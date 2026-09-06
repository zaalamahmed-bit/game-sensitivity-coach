"""Synthetic files and mocked FFmpeg only: no desktop, mouse, or game access."""

import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_evidence.py"
SPEC = importlib.util.spec_from_file_location("portable_export_evidence", str(SCRIPT))
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class ExportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="evidence-test-")
        self.root = Path(self.temporary.name)
        self.session = self.root / "recorded session"
        self.session.mkdir()
        self.output = self.root / "evidence output"
        self.frames = [{"frame_index": i, "t_start_ns": i * 100_000_000 + (9_000_000_000 if i >= 5 else 0),
                        "t_end_ns": i * 100_000_000 + (9_000_000_000 if i >= 5 else 0) + 20_000_000}
                       for i in range(15)]
        self.metadata = {"schema": "aim-observer/session-v1", "session_id": "synthetic-session", "capture": {"fps": 10}}
        self.analysis = {"schema": "aim-observer/review-v1", "session_id": "synthetic-session", "fps": 10,
                         "candidates": [self.candidate(i) for i in range(15)]}
        self.write_sources()
        (self.session / "video.mp4").write_bytes(b"synthetic video placeholder")
        self.ffmpeg = mock.patch.object(exporter, "_find_ffmpeg", return_value="ffmpeg-placeholder")
        self.ffmpeg.start()
        self.runner = mock.patch.object(exporter.subprocess, "run", side_effect=self.fake_decode)
        self.run_mock = self.runner.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.ffmpeg.stop)
        self.addCleanup(self.runner.stop)

    def candidate(self, index):
        frame = dict(self.frames[index])
        frame["t_mid_ns"] = (frame["t_start_ns"] + frame["t_end_ns"]) // 2
        frame["media_time_s"] = index / 10
        return {"id": "click-{:05d}".format(index + 1), "t_ns": frame["t_mid_ns"] + 2_000_000,
                "mapped_frame": frame, "device": "synthetic-device", "right_held_at_click": True,
                "window_overlaps_capture_gap": index in (4, 5), "mouse_row_number": index * 10}

    def write_sources(self):
        (self.session / "session.json").write_text(json.dumps(self.metadata), encoding="utf-8")
        (self.session / "analysis.json").write_text(json.dumps(self.analysis), encoding="utf-8")
        with (self.session / "frames.csv").open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=["frame_index", "t_start_ns", "t_end_ns"])
            writer.writeheader()
            writer.writerows(self.frames)

    def fake_decode(self, command, **kwargs):
        self.assertIsInstance(command, list)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("-ss", command)
        script = Path(command[command.index("-filter_script:v") + 1]).read_text(encoding="utf-8")
        indices = [int(value) for value in re.findall(r"eq\(n\\,(\d+)\)", script)]
        self.assertEqual(indices, sorted(set(indices)))
        for position, index in enumerate(indices):
            Path(command[-1] % position).write_bytes("synthetic frame {}".format(index).encode("ascii"))
        return subprocess.CompletedProcess(command, 0, "", "")

    def export(self, **kwargs):
        path = exporter.export_evidence(self.session, output=self.output, **kwargs)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_gap_uses_mapped_index_and_csv_timing_with_consecutive_neighbors(self):
        data = self.export(click_ids=["click-00006"], overview_count=0, before_frames=2, after_frames=2)
        candidate = data["candidates"][0]
        self.assertEqual([row["frame_index"] for row in candidate["samples"]], [3, 4, 5, 6, 7])
        self.assertEqual([row["relative_frame_offset"] for row in candidate["samples"]], [-2, -1, 0, 1, 2])
        mapped = candidate["mapped_frame"]
        self.assertEqual(mapped["media_time_s"], 0.5)
        self.assertEqual(mapped["t_start_ns"], 9_500_000_000)
        self.assertEqual(mapped["t_end_ns"], 9_520_000_000)
        self.assertEqual(mapped["t_mid_ns"], 9_510_000_000)
        self.assertEqual(candidate["samples"][1]["actual_offset_ms"], -9102)
        self.assertEqual(candidate["context"]["right_held_at_click"], True)
        self.assertTrue(candidate["context"]["window_overlaps_capture_gap"])
        for row in data["frames"]:
            self.assertFalse(Path(row["image"]).is_absolute())
            self.assertEqual((self.output / row["image"]).read_text(), "synthetic frame {}".format(row["frame_index"]))

    def test_overview_and_default_clicks_cover_both_ends_not_first_n(self):
        data = self.export(overview_count=3, click_count=3, before_frames=0, after_frames=0)
        self.assertEqual([row["frame_index"] for row in data["overview"]], [0, 7, 14])
        self.assertEqual([row["candidate_id"] for row in data["candidates"]], ["click-00001", "click-00008", "click-00015"])
        self.assertEqual(len(data["frames"]), 3)
        self.assertEqual(self.run_mock.call_count, 1)

    def test_boundaries_clamp_and_shared_frames_are_only_extracted_once(self):
        data = self.export(click_ids=["click-00001", "click-00015", "click-00001"],
                           overview_count=30, before_frames=30, after_frames=30)
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual(len(data["overview"]), 15)
        self.assertEqual(len(data["frames"]), 15)
        self.assertEqual(data["candidates"][0]["before_frames_available"], 0)
        self.assertEqual(data["candidates"][1]["after_frames_available"], 0)
        self.assertEqual(len(list(self.output.glob("frame-*.png"))), 15)

    def test_overview_only_needs_no_analysis_and_default_output_is_session_evidence(self):
        (self.session / "analysis.json").unlink()
        path = exporter.export_evidence(self.session, overview_count=1, overview_only=True)
        self.assertEqual(path, (self.session / "evidence" / "evidence.json").resolve())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["overview"][0]["frame_index"], 7)
        self.assertIsNone(data["source_analysis"])
        self.assertEqual(data["candidates"], [])

    def test_zero_clicks_still_produces_overview(self):
        self.analysis["candidates"] = []
        self.write_sources()
        self.assertEqual(len(self.export()["overview"]), 12)

    def test_source_bytes_unchanged_and_existing_output_is_preserved(self):
        before = {path.name: path.read_bytes() for path in self.session.iterdir()}
        self.export()
        first = {path.name: path.read_bytes() for path in self.output.iterdir()}
        with self.assertRaisesRegex(exporter.EvidenceError, "not empty"):
            self.export()
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, first)
        self.assertEqual({path.name: path.read_bytes() for path in self.session.iterdir()}, before)
        self.assertEqual(self.run_mock.call_count, 1)

    def test_unknown_ids_and_invalid_options_fail_before_decode(self):
        for options in ({"click_ids": ["click-99999"]}, {"before_frames": -1},
                        {"after_frames": True}, {"overview_count": -1}, {"click_count": -1},
                        {"click_ids": "click-00001"}, {"click_ids": [""]},
                        {"click_ids": ["click-00001"], "overview_only": True},
                        {"overview_count": 0, "click_count": 0}):
            with self.subTest(options=options), self.assertRaises(exporter.EvidenceError):
                self.export(**options)
        self.run_mock.assert_not_called()

    def test_stale_analysis_and_malformed_mappings_are_rejected(self):
        for change in (lambda: self.analysis.update(session_id="another-session"),
                       lambda: self.analysis.update(fps=30),
                       lambda: self.analysis["candidates"][0].update(mapped_frame=None),
                       lambda: self.analysis["candidates"][0]["mapped_frame"].update(frame_index=99),
                       lambda: self.analysis["candidates"][0]["mapped_frame"].update(t_mid_ns=42),
                       lambda: self.analysis["candidates"].append(self.analysis["candidates"][0])):
            with self.subTest(change=change):
                original = json.dumps(self.analysis)
                change()
                self.write_sources()
                with self.assertRaises(exporter.EvidenceError):
                    self.export()
                self.analysis = json.loads(original)
        self.run_mock.assert_not_called()

    def test_corrupt_csv_is_rejected_instead_of_silently_reindexing(self):
        for text in ("frame_index,t_start_ns,t_end_ns\n0,0,10\n2,20,30\n",
                     "frame_index,t_start_ns,t_end_ns\n0,0,10\n0,20,30\n",
                     "frame_index,t_start_ns,t_end_ns\n0,20,10\n",
                     "frame_index,t_start_ns,t_end_ns\n0,20,30\n1,0,10\n",
                     "frame_index,t_start_ns,t_end_ns\n0,bad,10\n",
                     "frame_index,t_start_ns,t_end_ns\n", "bad,header\n"):
            with self.subTest(csv=text):
                (self.session / "frames.csv").write_text(text, encoding="utf-8")
                with self.assertRaises(exporter.EvidenceError):
                    self.export(overview_only=True)
        self.run_mock.assert_not_called()

    def test_encoder_failure_is_clear_and_leaves_no_partial_evidence(self):
        self.run_mock.return_value = subprocess.CompletedProcess([], 7, "", "synthetic encoder error")
        self.run_mock.side_effect = None
        with self.assertRaisesRegex(exporter.EvidenceError, "FFmpeg failed.*synthetic encoder error"):
            self.export()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_short_decode_is_rejected_and_no_index_published(self):
        def short_decode(command, **kwargs):
            Path(command[-1] % 0).write_bytes(b"only one frame")
            return subprocess.CompletedProcess(command, 0, "", "")
        self.run_mock.side_effect = short_decode
        with self.assertRaisesRegex(exporter.EvidenceError, "1 images for 15 selected frames"):
            self.export()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_or_unlaunchable_ffmpeg_gives_actionable_error(self):
        self.ffmpeg.stop()
        with mock.patch.object(exporter.shutil, "which", return_value=None):
            with self.assertRaisesRegex(exporter.EvidenceError, "--ffmpeg"):
                self.export(ffmpeg=self.root / "nonexistent ffmpeg")
        self.ffmpeg.start()
        self.run_mock.side_effect = OSError("synthetic executable error")
        with self.assertRaisesRegex(exporter.EvidenceError, "Could not start FFmpeg"):
            self.export()

    def test_cli_combines_repeated_and_comma_separated_ids(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(exporter.main([str(self.session), "--output", str(self.output),
                                           "--click", "click-00015", "--click", "click-00001",
                                           "--clicks", "click-00006, click-00008", "--overview-count", "0",
                                           "--before-frames", "0", "--after-frames", "0"]), 0)
        self.assertIn("evidence.json", stdout.getvalue())
        data = json.loads((self.output / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual([row["candidate_id"] for row in data["candidates"]],
                         ["click-00015", "click-00001", "click-00006", "click-00008"])

    def test_cli_help_and_errors_do_not_decode(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, self.assertRaises(SystemExit) as result:
            exporter.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--overview-only", stdout.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as result:
            exporter.main([str(self.session), "--before-frames", "-1"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("nonnegative integer", stderr.getvalue())
        self.run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
