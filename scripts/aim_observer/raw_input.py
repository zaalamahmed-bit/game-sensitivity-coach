"""Observe Windows mouse Raw Input in a dedicated message-only window.

This module registers only generic-desktop mouse input (usage page 1, usage 2).
It does not install hooks, suppress input, or send input to any application.
Importing it is safe on other platforms; only ``start`` requires Windows.

The callback runs on the observer thread and should enqueue each event quickly.
Timestamps are perf_counter_ns() at WM_INPUT receipt, not hardware timestamps.
The raw ``flags`` field determines whether dx/dy are relative or absolute.
"""

import ctypes
import struct
import sys
import threading
import time
from typing import Callable, Dict, Optional


WM_INPUT = 0x00FF
WM_QUIT = 0x0012
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RIM_TYPEMOUSE = 0
MOUSE_MOVE_ABSOLUTE = 0x0001
UINT_ERROR = 0xFFFFFFFF

# Win32 LONG/ULONG are 32-bit even on 64-bit Windows. Explicit-width types
# also keep the binary parser and its tests correct on non-Windows systems.
UINT = ctypes.c_uint32
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
USHORT = ctypes.c_uint16
HANDLE = ctypes.c_void_p
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    LRESULT, HANDLE, UINT, WPARAM, LPARAM
)


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", DWORD), ("dwSize", DWORD),
        ("hDevice", HANDLE), ("wParam", WPARAM),
    ]


class _MouseButtonFields(ctypes.Structure):
    _fields_ = [("usButtonFlags", USHORT), ("usButtonData", USHORT)]


class _MouseButtonUnion(ctypes.Union):
    _anonymous_ = ("fields",)
    _fields_ = [("ulButtons", DWORD), ("fields", _MouseButtonFields)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("buttons",)
    _fields_ = [
        ("usFlags", USHORT), ("buttons", _MouseButtonUnion),
        ("ulRawButtons", DWORD), ("lLastX", LONG), ("lLastY", LONG),
        ("ulExtraInformation", DWORD),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", USHORT), ("usUsage", USHORT),
        ("dwFlags", DWORD), ("hwndTarget", HANDLE),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", LONG), ("cbWndExtra", LONG),
        ("hInstance", HANDLE), ("hIcon", HANDLE), ("hCursor", HANDLE),
        ("hbrBackground", HANDLE), ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HANDLE), ("message", UINT), ("wParam", WPARAM),
        ("lParam", LPARAM), ("time", DWORD), ("pt", _POINT),
        ("lPrivate", DWORD),
    ]


def parse_raw_mouse(data: bytes, t_ns: int, pointer_size: Optional[int] = None) -> Dict:
    """Decode one complete RID_INPUT packet; reject truncated/non-mouse data.

    ``pointer_size`` allows testing both Windows ABIs without a device or DLL.
    Device handles are session-local identifiers, not durable hardware IDs.
    ``button_data`` is interpreted as a signed SHORT to preserve wheel deltas.
    """
    if pointer_size is None:
        pointer_size = ctypes.sizeof(HANDLE)
    if pointer_size not in (4, 8):
        raise ValueError("Raw Input pointer size must be 4 or 8 bytes")
    header = struct.Struct("<II" + ("II" if pointer_size == 4 else "QQ"))
    if len(data) < header.size:
        raise ValueError("Truncated RAWINPUTHEADER")
    kind, size, device, _ = header.unpack_from(data)
    if kind != RIM_TYPEMOUSE:
        raise ValueError("Expected a mouse Raw Input packet")
    if size != len(data):
        raise ValueError("RAWINPUTHEADER size does not match received data")
    mouse = struct.Struct("<H2xHHIiiI")
    if len(data) < header.size + mouse.size:
        raise ValueError("Truncated RAWMOUSE payload")
    flags, buttons, button_data, _, dx, dy, _ = mouse.unpack_from(data, header.size)
    if button_data & 0x8000:
        button_data -= 0x10000
    return {
        "t_ns": t_ns, "dx": dx, "dy": dy, "flags": flags,
        "button_flags": buttons, "button_data": button_data,
        "device": "0x{:0{}x}".format(device, pointer_size * 2),
    }


def _windows_error(operation: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, "{} failed: {}".format(operation, ctypes.FormatError(code)))


class _Win32:
    """Explicit signatures prevent pointer truncation on 64-bit Windows."""

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "RegisterClassW": ([ctypes.POINTER(_WNDCLASSW)], USHORT),
            "UnregisterClassW": ([ctypes.c_wchar_p, HANDLE], LONG),
            "CreateWindowExW": ([DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                 DWORD, LONG, LONG, LONG, LONG,
                                 HANDLE, HANDLE, HANDLE, HANDLE], HANDLE),
            "DestroyWindow": ([HANDLE], LONG),
            "DefWindowProcW": ([HANDLE, UINT, WPARAM, LPARAM], LRESULT),
            "RegisterRawInputDevices": ([ctypes.POINTER(RAWINPUTDEVICE), UINT, UINT], LONG),
            "GetRawInputData": ([HANDLE, UINT, HANDLE, ctypes.POINTER(UINT), UINT], UINT),
            "GetMessageW": ([ctypes.POINTER(_MSG), HANDLE, UINT, UINT], LONG),
            "TranslateMessage": ([ctypes.POINTER(_MSG)], LONG),
            "DispatchMessageW": ([ctypes.POINTER(_MSG)], LRESULT),
            "PostThreadMessageW": ([DWORD, UINT, WPARAM, LPARAM], LONG),
            "PostQuitMessage": ([LONG], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes = arguments
            function.restype = result
        self.kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.kernel32.GetModuleHandleW.restype = HANDLE
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = DWORD


# Windows permits only one receiving window per raw-device class per process.
# Reject a second observer rather than silently redirecting an active session.
_process_registration = threading.Lock()


class RawMouseObserver:
    """One stoppable, mouse-only Raw Input receiver.

    ``start`` and ``stop`` belong on the owning/UI thread. Callback or Win32
    failures end capture and appear in ``error``; ``stop`` also raises them.
    Calls to start while running fail. A stopped instance can be restarted.
    """

    _TIMEOUT_SECONDS = 5.0

    def __init__(self, on_event: Callable[[Dict], None]):
        if not callable(on_event):
            raise TypeError("on_event must be callable")
        self._on_event = on_event
        self._thread = None
        self._thread_id = None
        self._api = None
        self._error = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._lifecycle_lock = threading.Lock()

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()
                    and self._ready.is_set() and not self._stop_requested.is_set())

    def start(self) -> None:
        if sys.platform != "win32":
            raise OSError("Mouse Raw Input recording requires Windows")
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Raw mouse observer is already running")
            if not _process_registration.acquire(blocking=False):
                raise RuntimeError("A raw mouse observer is already active in this process")
            self._error = None
            self._api = None
            self._thread_id = None
            self._ready.clear()
            self._stop_requested.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name="mouse-raw-input", daemon=True
            )
            try:
                self._thread.start()
            except BaseException:
                _process_registration.release()
                raise
            if not self._ready.wait(self._TIMEOUT_SECONDS):
                self._stop_requested.set()
                self._post_quit()
                self._thread.join(self._TIMEOUT_SECONDS)
                raise RuntimeError("Timed out initializing the mouse Raw Input observer")
            if self._error is not None:
                self._thread.join(self._TIMEOUT_SECONDS)
                raise RuntimeError("Could not start mouse Raw Input recording") from self._error

    def stop(self) -> None:
        if self._thread is threading.current_thread():
            raise RuntimeError("Stop the observer from its owning thread, not its callback")
        with self._lifecycle_lock:
            self._stop_requested.set()
            post_error = self._post_quit()
            if self._thread is not None:
                self._thread.join(self._TIMEOUT_SECONDS)
                if self._thread.is_alive():
                    raise RuntimeError("Mouse Raw Input thread did not stop") from post_error
            if self._error is not None:
                raise RuntimeError("Mouse Raw Input recording failed") from self._error

    def _post_quit(self) -> Optional[OSError]:
        if (self._thread is not None and self._thread.is_alive()
                and self._api is not None and self._thread_id is not None):
            if not self._api.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0):
                # The thread can have exited between is_alive() and posting.
                # Report this only if joining shows that it is still alive.
                return _windows_error("PostThreadMessageW")
        return None

    def _fail(self, error: BaseException) -> None:
        if self._error is None:
            self._error = error
        self._stop_requested.set()

    def _read_input(self, raw_handle: int, timestamp_ns: int) -> None:
        user32 = self._api.user32
        size = UINT(0)
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        result = user32.GetRawInputData(raw_handle, RID_INPUT, None,
                                        ctypes.byref(size), header_size)
        if result == UINT_ERROR:
            raise _windows_error("GetRawInputData size query")
        if result != 0 or size.value < header_size or size.value > 1024 * 1024:
            raise ValueError("Invalid Raw Input buffer size")
        expected = size.value
        # A DWORD array provides the alignment required by GetRawInputData.
        buffer = (DWORD * ((expected + 3) // 4))()
        copied = user32.GetRawInputData(raw_handle, RID_INPUT, buffer,
                                        ctypes.byref(size), header_size)
        if copied == UINT_ERROR:
            raise _windows_error("GetRawInputData")
        if copied != expected or size.value != expected:
            raise ValueError("Raw Input read returned an incomplete packet")
        self._on_event(parse_raw_mouse(bytes(buffer)[:copied], timestamp_ns))

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_INPUT:
            timestamp_ns = time.perf_counter_ns()
            try:
                if not self._stop_requested.is_set():
                    self._read_input(lparam, timestamp_ns)
            except BaseException as error:
                # Exceptions must never escape a ctypes callback: ctypes would
                # print and discard them, leaving a silently incomplete log.
                self._fail(error)
                self._api.user32.PostQuitMessage(1)
            finally:
                # Required for foreground RIM_INPUT cleanup, also harmless
                # for background RIM_INPUTSINK. A handled WM_INPUT returns 0.
                self._api.user32.DefWindowProcW(hwnd, message, wparam, lparam)
            return 0
        return self._api.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _thread_main(self) -> None:
        hwnd = None
        instance = None
        class_name = None
        registered_class = False
        registered_mouse = False
        callback = None  # Strong reference until after DestroyWindow.
        try:
            self._api = _Win32()
            user32 = self._api.user32
            self._thread_id = self._api.kernel32.GetCurrentThreadId()
            instance = self._api.kernel32.GetModuleHandleW(None)
            if not instance:
                raise _windows_error("GetModuleHandleW")
            class_name = "AimObserverRawMouse_{:x}_{}".format(id(self), self._thread_id)
            callback = WNDPROC(self._window_proc)
            window_class = _WNDCLASSW()
            window_class.lpfnWndProc = callback
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise _windows_error("RegisterClassW")
            registered_class = True
            hwnd = user32.CreateWindowExW(
                0, class_name, "Mouse Raw Input Observer", 0,
                0, 0, 0, 0, HANDLE(-3), None, instance, None,
            )
            if not hwnd:
                raise _windows_error("CreateWindowExW")
            if self._stop_requested.is_set():
                return
            device = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(ctypes.byref(device), 1,
                                                  ctypes.sizeof(device)):
                raise _windows_error("RegisterRawInputDevices")
            registered_mouse = True
            self._ready.set()
            message = _MSG()
            while not self._stop_requested.is_set():
                status = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status == -1:
                    raise _windows_error("GetMessageW")
                if status == 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as error:
            self._fail(error)
        finally:
            if self._api is not None:
                user32 = self._api.user32
                cleanup = []
                if registered_mouse:
                    device = RAWINPUTDEVICE(1, 2, RIDEV_REMOVE, None)
                    cleanup.append(("Unregister mouse Raw Input", lambda:
                        user32.RegisterRawInputDevices(ctypes.byref(device), 1,
                                                       ctypes.sizeof(device))))
                if hwnd:
                    cleanup.append(("DestroyWindow", lambda: user32.DestroyWindow(hwnd)))
                if registered_class:
                    cleanup.append(("UnregisterClassW", lambda:
                        user32.UnregisterClassW(class_name, instance)))
                for name, operation in cleanup:
                    try:
                        if not operation():
                            self._fail(_windows_error(name))
                    except BaseException as error:
                        self._fail(error)
            self._stop_requested.set()
            _process_registration.release()
            self._ready.set()
