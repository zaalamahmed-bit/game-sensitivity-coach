"""Windows desktop pixels only. No game hooks or process access."""
import ctypes
from ctypes import wintypes as w
import os
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path


def require_windows():
    if os.name != "nt":
        raise RuntimeError("Live gameplay capture requires Windows. Offline review and evidence export work on other platforms.")


def enable_dpi_awareness():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def monitors():
    require_windows()
    user = ctypes.WinDLL("user32", use_last_error=True)
    found = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)

    def visit(handle, dc, rect, data):
        r = rect.contents
        found.append(dict(left=r.left, top=r.top, width=r.right-r.left, height=r.bottom-r.top))
        return True

    callback = callback_type(visit)
    user.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
    if not user.EnumDisplayMonitors(None, None, callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return found


def normalize_window_title(title):
    # Some games insert invisible formatting characters into window captions.
    visible = "".join(char for char in title if unicodedata.category(char) != "Cf")
    return " ".join(visible.casefold().split())


class ForegroundGate:
    """Read the foreground window caption. Never open the game process."""
    def __init__(self, title):
        require_windows()
        self.title = normalize_window_title(title)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetForegroundWindow.restype = w.HWND
        self.user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]

    def active_handle(self):
        handle = self.user.GetForegroundWindow()
        if not self.title:
            return handle or 1
        text = ctypes.create_unicode_buffer(512)
        self.user.GetWindowTextW(handle, text, len(text))
        return handle if self.title in normalize_window_title(text.value) else 0


def find_game_monitor(displays, title):
    """Return the monitor most covered by a visible matching game window."""
    require_windows()
    needle = normalize_window_title(title)
    if not needle or not displays:
        return None
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, ctypes.c_ssize_t)
    signatures = {
        "GetForegroundWindow": ([], w.HWND),
        "GetWindowTextW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
        "IsWindowVisible": ([w.HWND], w.BOOL),
        "IsIconic": ([w.HWND], w.BOOL),
        "EnumWindows": ([callback_type, ctypes.c_ssize_t], w.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(user, name)
        function.argtypes, function.restype = arguments, result

    def matching_rectangle(handle):
        if not handle or not user.IsWindowVisible(handle) or user.IsIconic(handle):
            return None
        caption = ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(handle, caption, len(caption))
        if needle not in normalize_window_title(caption.value):
            return None
        rectangle = w.RECT()
        return rectangle if user.GetWindowRect(handle, ctypes.byref(rectangle)) else None

    foreground = matching_rectangle(user.GetForegroundWindow())
    rectangles = [foreground] if foreground is not None else []
    if not rectangles:
        def visit(handle, unused):
            rectangle = matching_rectangle(handle)
            if rectangle is not None:
                rectangles.append(rectangle)
            return True
        callback = callback_type(visit)
        if not user.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    best_index, best_area = None, 0
    for rectangle in rectangles:
        for index, monitor in enumerate(displays):
            width = max(0, min(rectangle.right, monitor["left"] + monitor["width"])
                        - max(rectangle.left, monitor["left"]))
            height = max(0, min(rectangle.bottom, monitor["top"] + monitor["height"])
                         - max(rectangle.top, monitor["top"]))
            if width * height > best_area:
                best_index, best_area = index, width * height
    return best_index


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]


class ScreenCapture:
    """Capture a selected desktop rectangle into a top-down BGR0 DIB."""
    def __init__(self, monitor, max_width=1920):
        require_windows()
        self.monitor = monitor
        scale = min(1.0, max_width / monitor["width"])
        self.width = max(2, int(monitor["width"] * scale) // 2 * 2)
        self.height = max(2, int(monitor["height"] * scale) // 2 * 2)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.GetDC.argtypes = [w.HWND]
        self.user.GetDC.restype = w.HDC
        self.user.ReleaseDC.argtypes = [w.HWND, w.HDC]
        self.gdi.CreateCompatibleDC.argtypes = [w.HDC]
        self.gdi.CreateCompatibleDC.restype = w.HDC
        self.gdi.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BITMAPINFOHEADER), w.UINT,
                                             ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
        self.gdi.CreateDIBSection.restype = w.HBITMAP
        self.gdi.SelectObject.argtypes = [w.HDC, w.HANDLE]
        self.gdi.SelectObject.restype = w.HANDLE
        self.gdi.DeleteObject.argtypes = [w.HANDLE]
        self.gdi.DeleteDC.argtypes = [w.HDC]
        self.gdi.StretchBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD]
        self.gdi.SetStretchBltMode.argtypes = [w.HDC, ctypes.c_int]
        self.screen = self.memory = self.bitmap = self.old = None
        try:
            self.screen = self.user.GetDC(None)
            self.memory = self.gdi.CreateCompatibleDC(self.screen)
            header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.width, -self.height, 1, 32)
            self.bits = ctypes.c_void_p()
            self.bitmap = self.gdi.CreateDIBSection(self.screen, ctypes.byref(header), 0,
                                                  ctypes.byref(self.bits), None, 0)
            if not self.screen or not self.memory or not self.bitmap or not self.bits.value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.old = self.gdi.SelectObject(self.memory, self.bitmap)
            self.gdi.SetStretchBltMode(self.memory, 3)  # COLORONCOLOR, low overhead
        except Exception:
            self.close()
            raise

    def grab(self):
        m = self.monitor
        start = time.perf_counter_ns()
        ok = self.gdi.StretchBlt(self.memory, 0, 0, self.width, self.height, self.screen,
                                 m["left"], m["top"], m["width"], m["height"], 0x40CC0020)
        self.gdi.GdiFlush()
        end = time.perf_counter_ns()
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(self.bits, self.width * self.height * 4), start, end

    def close(self):
        if self.old and self.memory:
            self.gdi.SelectObject(self.memory, self.old)
        if self.bitmap:
            self.gdi.DeleteObject(self.bitmap)
        if self.memory:
            self.gdi.DeleteDC(self.memory)
        if self.screen:
            self.user.ReleaseDC(None, self.screen)
        self.screen = self.memory = self.bitmap = self.old = None


def find_ffmpeg(explicit=None):
    choices = [explicit] if explicit else [shutil.which("ffmpeg")]
    for choice in choices:
        if choice and Path(choice).is_file():
            return str(Path(choice).resolve())
    raise RuntimeError("FFmpeg was not found. Put ffmpeg on PATH or supply --ffmpeg /path/to/ffmpeg.")


class VideoEncoder:
    def __init__(self, folder, width, height, fps, ffmpeg=None, hardware=False):
        executable = find_ffmpeg(ffmpeg)
        self.log = (Path(folder) / "encoder.log").open("wb")
        self.process = None
        codec = (["-c:v", "h264_nvenc", "-preset", "fast", "-b:v", "12M"] if hardware else
                 ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-threads", "2"])
        self.command = [executable, "-hide_banner", "-loglevel", "warning", "-nostdin",
                        "-f", "rawvideo", "-pixel_format", "bgr0", "-video_size", "%dx%d" % (width, height),
                        "-framerate", str(fps), "-i", "pipe:0", "-an"] + codec + [
                        "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                        "-g", str(fps), "-y", str(Path(folder) / "video.mp4")]
        try:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=self.log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            self.log.close()
            raise

    def write(self, data):
        if self.process.poll() is not None:
            raise RuntimeError("Video encoder stopped. See encoder.log in the session folder.")
        self.process.stdin.write(data)

    def abort(self):
        process = self.process
        if process and process.poll() is None:
            process.kill()

    def close(self):
        if self.process is None:
            return
        try:
            try:
                self.process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                code = self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise RuntimeError("Encoder did not finish in time; inspect encoder.log.")
            if code:
                raise RuntimeError("Video encoding failed (%s); inspect encoder.log." % code)
        finally:
            self.log.close()
            self.process = None
