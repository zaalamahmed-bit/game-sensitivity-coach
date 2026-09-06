"""Recorder integration tests using synthetic input, pixels, clocks and devices.

These tests never register input devices, capture a desktop, or launch FFmpeg.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import csv
import hashlib
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aim_observer import recorder


MONITOR = {"left": 0, "top": 0, "width": 1920, "height": 1080}


def mouse_event(timestamp, **changes):
    event = dict(t_ns=timestamp, dx=-12, dy=8, flags=0,
                 button_flags=1, button_data=0, device="0x1234")
    event.update(changes)
    return event


class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aim-observer-tests-")
        self.addCleanup(self.temporary.cleanup)

    def session(self, create_folder=True):
        result = recorder.RecordingSession(self.temporary.name, MONITOR,
                                           {"dpi": None, "sensitivity": None})
        result.origin = 1000000000
        result.metadata = {"capture": {}}
        result.gate = mock.Mock()
        result.gate.active_handle.return_value = 42
        if create_folder:
            result.folder.mkdir()
        return result

    @staticmethod
    def read_csv(path):
        with Path(path).open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    def test_mouse_timestamp_is_session_relative_and_input_is_not_mutated(self):
        session = self.session()
        event = mouse_event(session.origin + 123456789)
        original = dict(event)
        session._on_mouse(event)
        row = session.input_queue.get_nowait()
        self.assertEqual(row["t_ns"], 123456789)
        self.assertEqual(row["foreground"], 1)
        self.assertEqual((row["dx"], row["dy"], row["button_flags"]), (-12, 8, 1))
        self.assertEqual(event, original)

    def test_mouse_is_filtered_when_foreground_does_not_match_or_session_stops(self):
        session = self.session()
        session.gate.active_handle.return_value = 0
        session._on_mouse(mouse_event(session.origin + 1))
        self.assertTrue(session.input_queue.empty())
        session.gate.active_handle.return_value = 42
        session.stop_event.set()
        session._on_mouse(mouse_event(session.origin + 2))
        self.assertTrue(session.input_queue.empty())
        self.assertEqual(session.dropped_input, 0)

    def test_queue_overflow_counts_losses_without_replacing_accepted_events(self):
        session = self.session()
        session.input_queue = queue.Queue(maxsize=1)
        for offset in (1, 2, 3):
            session._on_mouse(mouse_event(session.origin + offset))
        self.assertEqual(session.dropped_input, 2)
        self.assertEqual(session.input_queue.get_nowait()["t_ns"], 1)
        self.assertTrue(session.input_queue.empty())

    def test_lost_foreground_records_one_boundary_without_background_movement(self):
        session = self.session()
        session._on_mouse(mouse_event(session.origin + 10))
        session.input_queue.get_nowait()
        session.gate.active_handle.return_value = 0
        session._on_mouse(mouse_event(session.origin + 20, dx=900, button_flags=2))
        session._on_mouse(mouse_event(session.origin + 30, dx=700, button_flags=1))
        boundary = session.input_queue.get_nowait()
        self.assertEqual(boundary["t_ns"], 20)
        self.assertEqual(boundary["foreground"], 0)
        self.assertEqual(boundary["device"], "focus")
        self.assertEqual((boundary["dx"], boundary["dy"], boundary["button_flags"]), (0, 0, 0))
        self.assertTrue(session.input_queue.empty())
        session.gate.active_handle.return_value = 42
        session._on_mouse(mouse_event(session.origin + 40))
        self.assertEqual(session.input_queue.get_nowait()["foreground"], 1)

    def test_writer_drains_accepted_queue_after_stop_and_records_exact_rows(self):
        session = self.session()
        for offset in (10, 20, 30):
            session._on_mouse(mouse_event(session.origin + offset, button_data=-120))
        session.stop_event.set()
        session.writer_done.set()
        session._write_mouse()
        rows = self.read_csv(session.folder / "mouse.csv")
        self.assertEqual([int(row["t_ns"]) for row in rows], [10, 20, 30])
        self.assertEqual([int(row["button_data"]) for row in rows], [-120] * 3)
        self.assertEqual(session.input_count, 3)
        self.assertTrue(session.input_queue.empty())
        self.assertEqual(session.errors, [])

    def test_writer_failure_stops_session_and_is_reported(self):
        session = self.session()
        session._on_mouse(mouse_event(session.origin + 1))
        session.writer_done.set()
        with mock.patch.object(recorder.csv, "DictWriter") as writer:
            writer.return_value.writerow.side_effect = OSError("synthetic disk full")
            session._write_mouse()
        self.assertTrue(session.stop_event.is_set())
        self.assertEqual(session.input_count, 0)
        self.assertTrue(any("synthetic disk full" in value for value in session.errors))

    def test_frame_indices_stay_sequential_across_real_time_gap(self):
        session = self.session()
        capture = mock.Mock(width=1920, height=1080)
        capture.grab.side_effect = [
            (b"first synthetic pixels", session.origin + 1000000, session.origin + 2000000),
            (b"second synthetic pixels", session.origin + 10000000000,
             session.origin + 10001000000),
        ]
        encoder = mock.Mock(command=["synthetic-encoder"])
        writes = []

        def accept_pixels(pixels):
            writes.append(pixels)
            if len(writes) == 2:
                session.stop_event.set()

        encoder.write.side_effect = accept_pixels
        with mock.patch.object(recorder, "ScreenCapture", return_value=capture), \
                mock.patch.object(recorder, "VideoEncoder", return_value=encoder), \
                mock.patch.object(recorder.shutil, "disk_usage", return_value=mock.Mock(free=8*1024**3)), \
                mock.patch.object(recorder.time, "perf_counter", side_effect=[0, 0, .001, 10, 10.001]):
            session._capture()
        rows = self.read_csv(session.folder / "frames.csv")
        self.assertEqual([int(row["frame_index"]) for row in rows], [0, 1])
        self.assertEqual([int(row["t_start_ns"]) for row in rows], [1000000, 10000000000])
        self.assertEqual([int(row["t_end_ns"]) for row in rows], [2000000, 10001000000])
        self.assertEqual(session.frame_count, 2)
        self.assertEqual(len(writes), 2)
        self.assertEqual(session.errors, [])
        capture.close.assert_called_once()
        encoder.close.assert_called_once()

    def test_changed_foreground_discards_frame_before_encoding(self):
        session = self.session()
        capture = mock.Mock(width=1920, height=1080)

        def grab():
            session.stop_event.set()
            return b"discarded synthetic pixels", session.origin + 1, session.origin + 2

        capture.grab.side_effect = grab
        session.gate.active_handle.side_effect = [42, 43]
        with mock.patch.object(recorder, "ScreenCapture", return_value=capture), \
                mock.patch.object(recorder, "VideoEncoder") as encoder, \
                mock.patch.object(recorder.time, "perf_counter", return_value=0):
            session._capture()
        self.assertEqual(session.frame_count, 0)
        self.assertEqual(session.focus_discarded, 1)
        self.assertEqual(self.read_csv(session.folder / "frames.csv"), [])
        encoder.assert_not_called()
        capture.close.assert_called_once()

    def test_capture_close_failure_is_reported_and_encoder_still_closes(self):
        session = self.session()
        capture = mock.Mock(width=1920, height=1080)
        capture.grab.return_value = (b"synthetic pixels", session.origin + 1, session.origin + 2)
        capture.close.side_effect = OSError("synthetic capture cleanup failure")
        encoder = mock.Mock(command=["synthetic-encoder"])
        encoder.write.side_effect = lambda pixels: session.stop_event.set()
        with mock.patch.object(recorder, "ScreenCapture", return_value=capture), \
                mock.patch.object(recorder, "VideoEncoder", return_value=encoder), \
                mock.patch.object(recorder.time, "perf_counter", return_value=0):
            session._capture()
        encoder.close.assert_called_once()
        self.assertTrue(any("synthetic capture cleanup failure" in value for value in session.errors))

    def test_raw_callback_error_is_detected_and_persisted_on_finalization(self):
        session = self.session()
        session.frame_count = 1
        session.mouse = mock.Mock(error=ValueError("synthetic malformed input callback"))
        status = session.status()
        self.assertTrue(session.stop_event.is_set())
        self.assertTrue(any("synthetic malformed input callback" in value for value in status["errors"]))
        session.stop()
        session.mouse.stop.assert_called_once()
        metadata = json.loads((session.folder / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "error")
        self.assertTrue(any("synthetic malformed input callback" in value for value in metadata["errors"]))

    def test_finalization_saves_counts_and_integrity_manifest_and_is_idempotent(self):
        session = self.session()
        session.frame_count, session.input_count, session.dropped_input = 4, 3, 2
        session.focus_discarded = 1
        (session.folder / "mouse.csv").write_bytes(b"synthetic mouse csv\n")
        (session.folder / "frames.csv").write_bytes(b"synthetic frame csv\n")
        (session.folder / "video.mp4").write_bytes(b"synthetic video bytes")
        session.mouse = mock.Mock(error=None)
        with mock.patch.object(recorder.time, "perf_counter_ns", return_value=session.origin + 9000000000):
            self.assertEqual(session.stop(), session.folder)
        metadata_bytes = (session.folder / "session.json").read_bytes()
        metadata = json.loads(metadata_bytes)
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["duration_ns"], 9000000000)
        self.assertEqual((metadata["frame_count"], metadata["input_events"],
                          metadata["dropped_input_events"], metadata["focus_discarded_frames"]),
                         (4, 3, 2, 1))
        manifest = json.loads((session.folder / "manifest.json").read_text(encoding="utf-8"))
        for name in ("session.json", "mouse.csv", "frames.csv"):
            data = (session.folder / name).read_bytes()
            self.assertEqual(manifest[name]["bytes"], len(data))
            self.assertEqual(manifest[name]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(manifest["video.mp4"]["bytes"], len(b"synthetic video bytes"))
        self.assertEqual(session.stop(), session.folder)
        self.assertEqual((session.folder / "session.json").read_bytes(), metadata_bytes)
        session.mouse.stop.assert_called_once()

    def test_zero_frame_error_is_not_mislabeled_as_successfully_empty(self):
        session = self.session()
        session._fail("synthetic startup error")
        session.stop()
        metadata = json.loads((session.folder / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "error")
        self.assertIn("synthetic startup error", metadata["errors"])

    def test_capture_startup_failure_drains_writer_and_finalizes_error(self):
        session = self.session(create_folder=False)
        with mock.patch.object(recorder, "ForegroundGate", return_value=session.gate), \
                mock.patch.object(recorder, "ScreenCapture", side_effect=OSError("synthetic GDI startup failure")), \
                mock.patch.object(recorder, "RawMouseObserver") as observer, \
                mock.patch.object(recorder.shutil, "disk_usage", return_value=mock.Mock(free=8*1024**3)):
            with self.assertRaisesRegex(RuntimeError, "synthetic GDI startup failure"):
                session.start()
        observer.assert_not_called()
        self.assertFalse(session.capture_thread.is_alive())
        self.assertFalse(session.writer_thread.is_alive())
        self.assertTrue(session._finished)
        metadata = json.loads((session.folder / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "error")

    def test_foreground_gate_startup_failure_finalizes_session(self):
        session = self.session(create_folder=False)
        with mock.patch.object(recorder, "ForegroundGate", side_effect=OSError("synthetic gate failure")), \
                mock.patch.object(recorder, "ScreenCapture") as capture, \
                mock.patch.object(recorder, "RawMouseObserver") as observer, \
                mock.patch.object(recorder.shutil, "disk_usage", return_value=mock.Mock(free=8*1024**3)):
            with self.assertRaisesRegex(OSError, "synthetic gate failure"):
                session.start()
        capture.assert_not_called()
        observer.assert_not_called()
        self.assertTrue(session._finished)
        metadata = json.loads((session.folder / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "error")

    def test_partial_thread_start_failure_stops_started_writer(self):
        session = self.session(create_folder=False)
        original_start = threading.Thread.start

        def start_thread(thread):
            if thread.name == "screen-video":
                raise RuntimeError("synthetic capture thread startup failure")
            return original_start(thread)

        with mock.patch.object(recorder, "ForegroundGate", return_value=session.gate), \
                mock.patch.object(recorder, "ScreenCapture") as capture, \
                mock.patch.object(recorder, "RawMouseObserver") as observer, \
                mock.patch.object(threading.Thread, "start", new=start_thread), \
                mock.patch.object(recorder.shutil, "disk_usage", return_value=mock.Mock(free=8*1024**3)):
            with self.assertRaisesRegex(RuntimeError, "synthetic capture thread startup failure"):
                session.start()
        capture.assert_not_called()
        observer.assert_not_called()
        self.assertFalse(session.writer_thread.is_alive())
        self.assertIsNone(session.capture_thread.ident)
        self.assertTrue(session._finished)
        metadata = json.loads((session.folder / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "error")


if __name__ == "__main__":
    unittest.main()
