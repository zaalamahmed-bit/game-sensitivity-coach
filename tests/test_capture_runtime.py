"""Portable CLI/control tests; all recording and window APIs are synthetic."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import capture_gameplay as cli
from aim_observer.profiles import load_profile


class Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DeadlineTests(unittest.TestCase):
    def control(self, action, seconds=None, request_id="first"):
        result = dict(schema=cli.CONTROL_SCHEMA, request_id=request_id, action=action)
        if seconds is not None:
            result["seconds"] = seconds
        return result

    def test_extension_keeps_same_start_and_repeated_request_is_not_applied_twice(self):
        deadline = cli.CaptureDeadline(100, 60, 600)
        document = self.control("extend", 480)
        self.assertEqual(deadline.apply(document), "extend")
        self.assertEqual(deadline.deadline, 640)
        self.assertIsNone(deadline.apply(document))
        self.assertEqual(deadline.deadline, 640)
        self.assertEqual(deadline.start, 100)

    def test_set_total_and_stop(self):
        deadline = cli.CaptureDeadline(100, 60, 600)
        deadline.apply(self.control("set_total", 480))
        self.assertEqual(deadline.deadline, 580)
        deadline.apply(self.control("stop", request_id="second"))
        self.assertTrue(deadline.stopped)

    def test_bad_controls_do_not_change_deadline(self):
        for document in ({}, self.control("extend", 600), self.control("extend", -1),
                         self.control("extend", float("nan")), self.control("extend", True),
                         self.control("unknown", 1), self.control("stop", request_id="")):
            deadline = cli.CaptureDeadline(100, 60, 600)
            with self.subTest(document=document), self.assertRaises(ValueError):
                deadline.apply(document)
            self.assertEqual(deadline.deadline, 160)
            self.assertFalse(deadline.stopped)

    def test_wait_for_game_is_bounded_and_does_not_capture(self):
        clock = Clock()
        gate = mock.Mock()
        gate.active_handle.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "did not become foreground"):
            cli.wait_for_game(gate, .5, clock.time, clock.sleep)
        self.assertAlmostEqual(clock.now, 100.5)
        gate.active_handle.return_value = 123
        cli.wait_for_game(gate, 20, clock.time, clock.sleep)
        self.assertAlmostEqual(clock.now, 100.5)


class ProfileTests(unittest.TestCase):
    def test_recorded_metadata_preserves_unknowns_and_game_context_on_startup_error(self):
        from aim_observer import recorder
        with tempfile.TemporaryDirectory() as directory:
            session = recorder.RecordingSession(directory,
                dict(left=0, top=0, width=640, height=480), game="Other Arena",
                title="Other Arena client", context={"weapon": "unknown"},
                setting_sources={"dpi": {"status": "unknown", "source": None}})
            with mock.patch.object(recorder.shutil, "disk_usage", return_value=SimpleNamespace(free=0)), \
                    mock.patch.object(recorder, "ForegroundGate") as gate:
                with self.assertRaisesRegex(RuntimeError, "Less than 2 GB"):
                    session.start()
                gate.assert_not_called()
            metadata = json.loads((session.folder / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "error")
            self.assertEqual(metadata["game"], "Other Arena")
            self.assertTrue(all(value is None for value in metadata["settings"].values()))
            self.assertEqual(metadata["context"], {"weapon": "unknown"})
            self.assertEqual(metadata["setting_sources"]["dpi"]["status"], "unknown")

    def test_settings_are_unknown_without_profile_or_explicit_flags(self):
        args = cli.build_parser().parse_args(["--game", "Example Arena", "--seconds", "30"])
        context = cli.resolve_context(args)
        self.assertEqual(context["game"], "Example Arena")
        self.assertEqual(context["title"], "Example Arena")
        self.assertTrue(all(value is None for value in context["settings"].values()))
        self.assertTrue(all(value["status"] == "unknown" for value in context["setting_sources"].values()))

    def test_invisible_only_title_cannot_turn_game_filter_into_all_windows(self):
        args = cli.build_parser().parse_args(["--game", "Example Arena", "--seconds", "30",
                                             "--window-title", "\u200b\ufeff"])
        with self.assertRaisesRegex(ValueError, "nonempty game window"):
            cli.resolve_context(args)

    def test_profile_sources_and_extra_game_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = dict(schema="game-sensitivity/profile-v1", game="Other Arena", window_title="Arena client",
                           settings={"look_sensitivity": 250, "scoped_multiplier": None, "invert_y": False},
                           setting_sources={"look_sensitivity": {"status": "observed", "source": "config-file",
                                                                 "observed_at_utc": "2026-01-01T00:00:00Z"}},
                           context={"weapon": "unknown"}, evidence=[{"path": "config-file"}])
            path.write_text(json.dumps(profile), encoding="utf-8")
            args = cli.build_parser().parse_args(["--profile", str(path), "--seconds", "30", "--dpi", "1200"])
            context = cli.resolve_context(args)
            self.assertEqual(context["game"], "Other Arena")
            self.assertEqual(context["title"], "Arena client")
            self.assertEqual(context["settings"]["look_sensitivity"], 250)
            self.assertEqual(context["settings"]["dpi"], 1200)
            self.assertIsNone(context["settings"]["scoped_multiplier"])
            self.assertFalse(context["settings"]["invert_y"])
            self.assertEqual(context["setting_sources"]["look_sensitivity"], profile["setting_sources"]["look_sensitivity"])
            self.assertEqual(context["context"]["profile_evidence"], profile["evidence"])
            self.assertEqual(context["setting_sources"]["dpi"]["source"], "command-line")

    def test_profile_rejects_invalid_schema_or_nonfinite_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            for profile in ({"schema": "wrong"}, {"settings": {"dpi": -1}},
                            {"settings": {"look_sensitivity": float("nan")}},
                            {"settings": {"other": {"nested": 1}}}):
                path.write_text(json.dumps(profile), encoding="utf-8")
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    load_profile(path)

    def test_non_windows_capture_reports_platform_without_loading_capture_modules(self):
        args = cli.build_parser().parse_args(["--game", "Example Arena", "--seconds", "30"])
        with mock.patch.object(cli, "os", SimpleNamespace(name="posix")):
            with self.assertRaisesRegex(RuntimeError, "requires Windows"):
                cli.run_capture(args)


class CaptureIntegrationTests(unittest.TestCase):
    def test_control_cli_publishes_unique_requests_without_overwriting_other_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.json"
            original = {"schema": cli.CONTROL_SCHEMA, "request_id": "initial",
                        "action": "set_total", "seconds": 60}
            cli.atomic_json(path, original)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.send_control([str(path), "--extend-seconds", "480"]), 0)
            request = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "extend")
            self.assertEqual(request["seconds"], 480)
            self.assertNotEqual(request["request_id"], "initial")
            path.write_text('{"schema":"unrelated-document"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a gameplay recording"):
                cli.send_control([str(path), "--stop"])
            self.assertEqual(path.read_text(encoding="utf-8"), '{"schema":"unrelated-document"}')

    def test_control_extension_keeps_one_session_and_finalizes_after_new_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = Clock()
            control = root / "control.json"
            event = SimpleNamespace(wait=clock.sleep, is_set=lambda: False)
            session = SimpleNamespace(folder=root / "synthetic-session", metadata={}, errors=[],
                                      frame_count=12, input_count=15, stop_event=event)
            calls = {"start": 0, "stop": 0, "status": 0}

            def start():
                calls["start"] += 1
                session.folder.mkdir()
                return session.folder

            def stop():
                calls["stop"] += 1
                session.metadata["files_finalized"] = True
                return session.folder

            def status():
                calls["status"] += 1
                if calls["status"] == 1:
                    cli.atomic_json(control, {"schema": cli.CONTROL_SCHEMA, "request_id": "extension",
                                              "action": "extend", "seconds": 3})
                return dict(errors=[], active=True, elapsed_s=clock.now-100,
                            frame_count=12, input_events=15, dropped_input_events=0)

            session.start, session.stop, session.status = start, stop, status
            factory = mock.Mock(return_value=session)
            fake_capture = SimpleNamespace(ForegroundGate=mock.Mock(), enable_dpi_awareness=mock.Mock(),
                                           find_ffmpeg=mock.Mock(return_value="synthetic-ffmpeg"),
                                           find_game_monitor=mock.Mock(return_value=0),
                                           monitors=lambda: [dict(left=0, top=0, width=640, height=480)])
            args = cli.build_parser().parse_args(["--game", "Example Arena", "--seconds", "2",
                                                "--max-total-seconds", "10", "--output-root", str(root),
                                                "--control-file", str(control)])
            with mock.patch.object(cli, "os", SimpleNamespace(name="nt")), \
                    mock.patch.object(cli.time, "monotonic", clock.time), \
                    mock.patch.dict(sys.modules, {"aim_observer.capture": fake_capture,
                                                   "aim_observer.recorder": SimpleNamespace(RecordingSession=factory)}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.run_capture(args), 0)
            factory.assert_called_once()
            self.assertEqual(calls["start"], 1)
            self.assertEqual(calls["stop"], 1)
            self.assertAlmostEqual(clock.now, 105)
            self.assertEqual([row["action"] for row in session.metadata["runtime"]["control_history"]],
                             ["set_total", "extend"])
            status_data = json.loads((session.folder / "capture-status.json").read_text(encoding="utf-8"))
            self.assertTrue(status_data["files_finalized"])
            self.assertEqual(status_data["stop_reason"], "deadline")


if __name__ == "__main__":
    unittest.main()
