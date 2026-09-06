"""Session lifecycle, bounded input buffering and traceable frame timing."""
import csv
import datetime as dt
import hashlib
import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from .capture import ForegroundGate, ScreenCapture, VideoEncoder
from .raw_input import RawMouseObserver

MOUSE_FIELDS = ["t_ns", "dx", "dy", "flags", "button_flags", "button_data", "device", "foreground"]


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


class RecordingSession:
    def __init__(self, output_root, monitor, settings=None, fps=30, max_width=1920,
                 title=None, ffmpeg=None, hardware=False, game=None, context=None,
                 setting_sources=None, settings_source=None):
        if fps not in (15, 30, 60):
            raise ValueError("Choose 15, 30 or 60 frames per second.")
        if max_width not in (1280, 1920, 2560, 3840):
            raise ValueError("Unsupported capture width.")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.folder = Path(output_root).expanduser() / (stamp + "-" + uuid.uuid4().hex[:6])
        self.monitor, self.fps = dict(monitor), fps
        self.settings = dict(dpi=None, look_sensitivity=None, scoped_multiplier=None,
                             normal_zoom_multiplier=None)
        self.settings.update(settings or {})
        self.game = game or title or "Unknown game"
        title = title or self.game
        self.context = dict(context or {})
        self.setting_sources = dict(setting_sources or {})
        self.settings_source = settings_source or (
            "Unknown unless supplied or discovered by the host agent; the runtime does not read game settings or mouse hardware")
        self.max_width, self.title, self.ffmpeg, self.hardware = max_width, title, ffmpeg, hardware
        self.stop_event = threading.Event()
        self.input_queue = queue.Queue(maxsize=65536)
        self.writer_done = threading.Event()
        self.capture_ready = threading.Event()
        self.capture_thread = self.writer_thread = self.mouse = None
        self.errors = []
        self.frame_count = self.input_count = self.dropped_input = self.focus_discarded = 0
        self.active = False
        self.origin = 0
        self.metadata = {}
        self._finished = False
        self._input_active = False
        self._encoder = None

    def _fail(self, error):
        self.errors.append(str(error))
        self.stop_event.set()

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=False)
        self.origin = time.perf_counter_ns()
        self.metadata = {
            "schema": "aim-observer/session-v1", "session_id": self.folder.name,
            "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "game": self.game, "status": "recording", "settings": self.settings,
            "settings_source": self.settings_source, "setting_sources": self.setting_sources,
            "context": self.context,
            "capture": {"fps": self.fps, "monitor": self.monitor, "max_width": self.max_width,
                        "foreground_title_contains": self.title, "backend": "Windows desktop GDI",
                        "codec": "h264_nvenc" if self.hardware else "libx264"},
            "clock": {"name": "time.perf_counter_ns", "origin_ns": self.origin,
                      "input_timestamp": "OS message receipt, not hardware polling time",
                      "frame_timestamp": "Start/end of GDI capture, not display presentation time",
                      "media_mapping": "Use frames.csv frame_index/fps; real-time gaps are preserved in CSV"},
            "interpretation": {"click": "Left-button-down candidate, not a confirmed shot",
                               "right_button": "Physical button state, not confirmed scoped mode",
                               "dx_dy": "Raw mouse counts, not screen pixels or camera degrees"}}
        write_json(self.folder / "session.json", self.metadata)
        try:
            if shutil.disk_usage(str(self.folder)).free < 2 * 1024 ** 3:
                raise RuntimeError("Less than 2 GB free at the recording destination.")
            self.gate = ForegroundGate(self.title)
            self.writer_thread = threading.Thread(target=self._write_mouse, name="mouse-csv", daemon=True)
            self.capture_thread = threading.Thread(target=self._capture, name="screen-video", daemon=True)
            self.writer_thread.start()
            self.capture_thread.start()
            if not self.capture_ready.wait(10):
                raise RuntimeError("Screen capture did not initialize in time.")
            if self.errors:
                raise RuntimeError(self.errors[0])
            self.mouse = RawMouseObserver(self._on_mouse)
            self.mouse.start()
        except Exception as exc:
            self._fail(exc)
            self.stop()
            raise
        return self.folder

    def _on_mouse(self, event):
        if self.stop_event.is_set():
            return
        active = bool(self.gate.active_handle())
        if not active:
            if not self._input_active:
                return
            # A boundary, not a background input sample. Clear stale button state in review.
            row = dict(t_ns=event["t_ns"]-self.origin, dx=0, dy=0, flags=0,
                       button_flags=0, button_data=0, device="focus", foreground=0)
        else:
            row = dict(event)
            row["t_ns"] -= self.origin
            row["foreground"] = 1
        self._input_active = active
        try:
            self.input_queue.put_nowait(row)
        except queue.Full:
            self.dropped_input += 1

    def _write_mouse(self):
        try:
            with (self.folder / "mouse.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, MOUSE_FIELDS, extrasaction="ignore")
                writer.writeheader()
                last_flush = time.perf_counter()
                while not self.writer_done.is_set() or not self.input_queue.empty():
                    try:
                        row = self.input_queue.get(timeout=0.1)
                    except queue.Empty:
                        row = None
                    if row is not None:
                        writer.writerow(row)
                        self.input_count += 1
                    if time.perf_counter() - last_flush > 1:
                        stream.flush()
                        last_flush = time.perf_counter()
        except Exception as exc:
            self._fail("Mouse log: " + str(exc))

    def _capture(self):
        capture = encoder = None
        try:
            capture = ScreenCapture(self.monitor, self.max_width)
            self.metadata["capture"].update(width=capture.width, height=capture.height)
            write_json(self.folder / "session.json", self.metadata)
            self.capture_ready.set()
            with (self.folder / "frames.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["frame_index", "t_start_ns", "t_end_ns"])
                due = time.perf_counter()
                checked_disk = flushed = due
                while not self.stop_event.is_set():
                    handle = self.gate.active_handle()
                    self.active = bool(handle)
                    if not handle:
                        self.stop_event.wait(0.05)
                        due = time.perf_counter()
                        continue
                    now = time.perf_counter()
                    if now < due:
                        self.stop_event.wait(min(due-now, 0.05))
                        continue
                    if now - checked_disk > 5:
                        if shutil.disk_usage(str(self.folder)).free < 1024 ** 3:
                            raise RuntimeError("Recording stopped with less than 1 GB free.")
                        checked_disk = now
                    pixels, start, end = capture.grab()
                    if self.gate.active_handle() != handle:
                        self.focus_discarded += 1
                        continue
                    if encoder is None:
                        encoder = VideoEncoder(self.folder, capture.width, capture.height, self.fps,
                                               self.ffmpeg, self.hardware)
                        self._encoder = encoder
                        self.metadata["capture"]["encoder_command"] = encoder.command
                    encoder.write(pixels)
                    writer.writerow([self.frame_count, start-self.origin, end-self.origin])
                    self.frame_count += 1
                    if now-flushed > 1:
                        stream.flush()
                        flushed = now
                    # Never invent frames to conceal a stall. The sidecar records each actual capture.
                    due = max(due + 1/self.fps, time.perf_counter())
        except Exception as exc:
            self._fail("Screen/video: " + str(exc))
        finally:
            self.capture_ready.set()
            self.active = False
            if capture:
                try:
                    capture.close()
                except Exception as exc:
                    self._fail("Capture cleanup: " + str(exc))
            if encoder:
                try:
                    encoder.close()
                except Exception as exc:
                    self._fail(exc)
            self._encoder = None

    def status(self):
        if self.mouse and self.mouse.error and not self.stop_event.is_set():
            self._fail("Raw input: " + str(self.mouse.error))
        return dict(elapsed_s=(time.perf_counter_ns()-self.origin)/1e9 if self.origin else 0,
                    frame_count=self.frame_count, input_events=self.input_count,
                    dropped_input_events=self.dropped_input, active=self.active, errors=list(self.errors))

    def stop(self):
        if self._finished:
            return self.folder
        self.stop_event.set()
        if self.mouse:
            try:
                self.mouse.stop()
            except Exception as exc:
                self._fail(exc)
        self.writer_done.set()
        if self.capture_thread and self.capture_thread.ident is not None:
            self.capture_thread.join(25)
            if self.capture_thread.is_alive():
                encoder = self._encoder
                if encoder:
                    encoder.abort()
                    self.capture_thread.join(5)
                self._fail("Capture worker did not stop; session may be incomplete.")
        if self.writer_thread and self.writer_thread.ident is not None:
            self.writer_thread.join(10)
            if self.writer_thread.is_alive():
                self._fail("Input writer did not stop; session may be incomplete.")
        unsettled = any(worker and worker.is_alive() for worker in (self.capture_thread, self.writer_thread))
        self.metadata.update(status="error" if self.errors else "complete",
                             ended_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                             duration_ns=time.perf_counter_ns()-self.origin,
                             frame_count=self.frame_count, input_events=self.input_count,
                             dropped_input_events=self.dropped_input, focus_discarded_frames=self.focus_discarded,
                             errors=self.errors, files_finalized=not unsettled)
        if not self.frame_count:
            if not self.errors:
                self.metadata["status"] = "empty"
            self.metadata["note"] = "No frames. Check the foreground window title and selected monitor."
        write_json(self.folder / "session.json", self.metadata)
        if unsettled:
            # Do not label still-changing files with final integrity hashes.
            return self.folder
        manifest = {}
        for name in ("session.json", "mouse.csv", "frames.csv"):
            path = self.folder / name
            if path.exists():
                digest = hashlib.sha256()
                with path.open("rb") as data:
                    for block in iter(lambda: data.read(1024*1024), b""):
                        digest.update(block)
                manifest[name] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
        video = self.folder / "video.mp4"
        if video.exists():
            manifest["video.mp4"] = {"bytes": video.stat().st_size}
        write_json(self.folder / "manifest.json", manifest)
        self._finished = True
        return self.folder
