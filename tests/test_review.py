"""Synthetic-only review checks; never capture a real desktop or read input."""

import contextlib
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from aim_observer.review import analyze_session, generate_review, map_timestamp_to_frame, validate_coach_assessment
import review_session


MOUSE_COLUMNS = ["t_ns", "dx", "dy", "flags", "button_flags", "button_data", "device", "foreground"]
FRAME_COLUMNS = ["frame_index", "t_start_ns", "t_end_ns"]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aim-review-test-")
        self.directory = Path(self.temporary.name)
        self.metadata = {
            "schema": "aim-observer/session-v1", "session_id": "synthetic-test",
            "started_at_utc": "2026-09-04T00:00:00Z", "game": "Example Arena",
            "settings": {"dpi": 1200, "look_sensitivity": 3.5,
                         "scoped_multiplier": 0.9, "normal_zoom_multiplier": 1.0},
            "capture": {"fps": 10, "width": 640, "height": 360,
                        "monitor": {"left": 0, "top": 0, "width": 640, "height": 360}},
            "status": "complete", "dropped_input_events": 0, "errors": [],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def write_session(self, mouse=None, frames=None):
        (self.directory / "session.json").write_text(json.dumps(self.metadata), encoding="utf-8")
        if frames is None:
            frames = [(0, 0, 20_000_000), (1, 100_000_000, 120_000_000),
                      (2, 2_000_000_000, 2_020_000_000)]
        with (self.directory / "frames.csv").open("w", newline="", encoding="utf-8") as destination:
            writer = csv.writer(destination)
            writer.writerow(FRAME_COLUMNS)
            writer.writerows(frames)
        with (self.directory / "mouse.csv").open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=MOUSE_COLUMNS)
            writer.writeheader()
            writer.writerows(mouse or [])

    @staticmethod
    def event(t, dx=0, dy=0, buttons=0, flags=0, device="mouse-a", foreground=1):
        return dict(t_ns=t, dx=dx, dy=dy, flags=flags, button_flags=buttons,
                    button_data=0, device=device, foreground=foreground)

    def assessment(self, **changes):
        value = {
            "schema": "aim-observer/coach-assessment-v1", "status": "baseline_retained",
            "settings": dict(self.metadata["settings"]),
            "headline": "Keep the recorded settings for now",
            "summary": "This synthetic observation does not establish a better setting.",
            "evidence": [{"candidate_id": "click-00001", "frame_index": 0,
                          "observation": "Synthetic evidence for report testing only."}],
            "limitations": ["This is synthetic test data."],
            "next_step": "The assistant will assess additional evidence when available.",
            "created_at_utc": "2026-09-04T00:00:00Z", "source": "assistant-visual-review",
        }
        value.update(changes)
        return value

    def generated_analysis(self):
        generate_review(self.directory)
        return json.loads((self.directory / "analysis.json").read_text(encoding="utf-8"))

    def write_assessment(self, value):
        (self.directory / "coach-assessment.json").write_text(json.dumps(value), encoding="utf-8")

    def correction(self, **changes):
        value = {
            "schema": "aim-observer/settings-correction-v1", "source": "user-reported",
            "reported_at_utc": "2026-09-05T00:00:00Z", "settings": {"scoped_multiplier": 0.85},
            "note": "User reported scoped multiplier 0.85; capture-time value not independently verified.",
        }
        value.update(changes)
        return value

    def write_correction(self, value):
        (self.directory / "settings-correction.json").write_text(json.dumps(value), encoding="utf-8")

    def test_capture_gap_uses_actual_midpoint_and_encoded_media_index(self):
        self.write_session([self.event(2_005_000_000, buttons=1)])
        result = analyze_session(self.directory)
        mapped = result["candidates"][0]["mapped_frame"]
        self.assertEqual(mapped["frame_index"], 2)
        self.assertAlmostEqual(mapped["media_time_s"], 0.2)
        self.assertAlmostEqual(mapped["distance_ms"], 5.0)
        self.assertEqual(result["quality"]["capture_gap_count"], 1)
        self.assertAlmostEqual(result["quality"]["capture_gap_excess_total_ms"], 1800)
        self.assertTrue(result["candidates"][0]["window_overlaps_capture_gap"])

    def test_nearest_midpoint_tie_chooses_earlier_capture(self):
        frames = [dict(frame_index=0, t_start_ns=0, t_end_ns=0, t_mid_ns=0),
                  dict(frame_index=1, t_start_ns=100, t_end_ns=100, t_mid_ns=100)]
        self.assertEqual(map_timestamp_to_frame(50, frames, 30)["frame_index"], 0)
        self.assertEqual(map_timestamp_to_frame(-10, frames, 30)["frame_index"], 0)
        self.assertEqual(map_timestamp_to_frame(1000, frames, 30)["frame_index"], 1)
        self.assertIsNone(map_timestamp_to_frame(0, [], 30))

    def test_observed_cadence_shows_slow_capture_even_without_gap_flags(self):
        self.metadata["capture"]["fps"] = 30
        self.write_session([], frames=[(i, i * 40_000_000, i * 40_000_000 + 2_000_000)
                                       for i in range(20)])
        quality = analyze_session(self.directory)["quality"]
        self.assertEqual(quality["capture_gap_count"], 0)
        self.assertEqual(quality["median_frame_midpoint_interval_ms"], 40)
        self.assertEqual(quality["p95_frame_midpoint_interval_ms"], 40)
        self.assertEqual(quality["observed_median_fps"], 25)

    def test_button_order_device_isolation_and_focus_loss(self):
        events = [self.event(10, buttons=4), self.event(10, buttons=1),
                  self.event(20, buttons=8), self.event(20, buttons=1),
                  self.event(30, buttons=1, device="mouse-b"),
                  self.event(40, buttons=4), self.event(50, device="focus", foreground=0),
                  self.event(60, buttons=1), self.event(70, buttons=5),
                  self.event(80, buttons=13)]
        self.write_session(events)
        result = analyze_session(self.directory)
        self.assertEqual([c["right_held_at_click"] for c in result["candidates"]],
                         [True, False, None, None, True, False])
        self.assertTrue(result["candidates"][-1]["right_transition_in_click_packet"])

    def test_background_clicks_excluded_and_background_state_not_trusted(self):
        self.write_session([self.event(10, buttons=4), self.event(20, buttons=5, foreground=0),
                            self.event(30, buttons=1)])
        result = analyze_session(self.directory)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["quality"]["background_left_down_events"], 1)
        self.assertIsNone(result["candidates"][0]["right_held_at_click"])

    def test_absolute_and_background_movement_excluded_per_device(self):
        self.write_session([
            self.event(0, dx=10), self.event(100, dy=4),
            self.event(150, dx=1000, dy=1000, flags=1),
            self.event(175, dx=-999, dy=-999, foreground=0),
            self.event(200, dx=-3, dy=-2, buttons=1),
            self.event(220, dx=-2, device="mouse-b"),
            self.event(230, dx=1, device="mouse-b"),
        ])
        result = analyze_session(self.directory)
        candidate = result["candidates"][0]
        a, b = candidate["motion_by_device"]
        self.assertEqual((a["net_dx"], a["net_dy"]), (7, 2))
        self.assertEqual((a["x_direction_reversals"], a["y_direction_reversals"]), (1, 1))
        self.assertEqual(a["absolute_dx_sum"], 13)
        self.assertEqual(a["relative_motion_events"], 3)
        self.assertEqual((b["net_dx"], b["net_dy"]), (-1, 0))
        self.assertEqual(b["x_direction_reversals"], 1)
        self.assertEqual(candidate["window_absolute_motion_events"], 1)
        self.assertEqual(candidate["window_background_events"], 1)

    def test_window_bounds_include_edges_and_exclude_outside(self):
        click = 1_000_000_000
        self.write_session([self.event(click - 750_000_001, dx=999),
                            self.event(click - 750_000_000, dx=1),
                            self.event(click, buttons=1),
                            self.event(click + 500_000_000, dx=2),
                            self.event(click + 500_000_001, dx=999)])
        result = analyze_session(self.directory)
        motion = result["candidates"][0]["motion_by_device"][0]
        self.assertEqual(motion["net_dx"], 3)
        self.assertEqual([point[0] for point in motion["trace"]], [-750, 500])

    def test_high_rate_overlapping_windows_keep_exact_metrics_and_bounded_traces(self):
        self.write_session(self.event(i * 125_000, dx=1 if i % 2 else -1,
                                      buttons=1 if i in (8000, 8800) else 0)
                           for i in range(16_001))
        result = analyze_session(self.directory)
        self.assertEqual(result["quality"]["valid_mouse_rows"], 16_001)
        self.assertEqual(len(result["candidates"]), 2)
        for candidate in result["candidates"]:
            motion = candidate["motion_by_device"][0]
            self.assertEqual(motion["relative_motion_events"], 10_001)
            self.assertEqual(motion["x_direction_reversals"], 10_000)
            self.assertEqual(motion["net_dx"], -1)
            self.assertTrue(motion["trace_downsampled"])
            self.assertLessEqual(len(motion["trace"]), 251)
            self.assertEqual(motion["trace"][0][0], -745.125)
            self.assertEqual(motion["trace"][-1][0], 500)

    def test_out_of_order_rows_are_counted_and_skipped_without_reordering_buttons(self):
        self.write_session([self.event(10, buttons=4), self.event(20, buttons=1),
                            self.event(15, buttons=8), self.event(30, buttons=1)])
        result = analyze_session(self.directory)
        self.assertEqual(result["quality"]["out_of_order_mouse_rows"], 1)
        self.assertEqual(result["quality"]["valid_mouse_rows"], 3)
        self.assertEqual([c["right_held_at_click"] for c in result["candidates"]], [True, True])

    def test_invalid_rows_duplicates_and_recorder_failures_are_visible(self):
        self.metadata.update(dropped_input_events=12, errors=["encoder stopped"], status="error")
        self.write_session([self.event(10, buttons=1), self.event(-1), self.event(20, foreground=9)],
                           frames=[(0, 0, 2), (0, 1, 3), (2, 10, 12), (3, 99, 1)])
        quality = analyze_session(self.directory)["quality"]
        self.assertEqual(quality["invalid_mouse_rows"], 2)
        self.assertEqual(quality["invalid_frame_rows"], 1)
        self.assertEqual(quality["duplicate_frame_indices"], 1)
        self.assertFalse(quality["frame_indices_contiguous_from_zero"])
        self.assertEqual(quality["dropped_input_events"], 12)
        self.assertEqual(quality["recording_errors"], ["encoder stopped"])
        self.assertEqual(quality["recording_status"], "error")

    def test_html_escapes_metadata_and_device_names_without_losing_data(self):
        dangerous = '</script><script id="injected">bad()</script><img src=x onerror=bad()>&\u2028'
        self.metadata["session_id"] = dangerous
        self.metadata["settings"]["note"] = dangerous
        self.write_session([self.event(10, dx=1, buttons=1, device=dangerous)])
        report = generate_review(self.directory)
        self.assertEqual(report, self.directory / "report.html")
        rendered = report.read_text(encoding="utf-8")
        self.assertNotIn('<script id="injected">', rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertEqual(rendered.count("</script>"), 2)
        embedded = re.search(r'<script id="reviewData" type="application/json">(.*?)</script>',
                             rendered, re.DOTALL).group(1)
        self.assertEqual(json.loads(embedded)["session_id"], dangerous)
        self.assertEqual(json.loads((self.directory / "analysis.json").read_text(encoding="utf-8"))
                         ["candidates"][0]["device"], dangerous)
        self.assertIn('src="video.mp4"', rendered)
        self.assertNotIn('src="https://', rendered)
        self.assertNotIn('src="http://', rendered)

    def test_empty_session_still_generates_quality_report(self):
        self.write_session([], frames=[])
        result = analyze_session(self.directory)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["quality"]["valid_frame_rows"], 0)
        self.assertTrue(generate_review(self.directory).exists())

    def test_missing_or_invalid_fps_fails_with_useful_error(self):
        for value in (0, -1, "invalid", None, float("nan")):
            self.metadata["capture"]["fps"] = value
            self.write_session()
            with self.assertRaisesRegex(ValueError, "capture.fps"):
                analyze_session(self.directory)

    def test_missing_coach_file_is_pending_without_inventing_recommendations(self):
        self.write_session([self.event(10, buttons=1)])
        result = self.generated_analysis()
        self.assertIsNone(result["coach_assessment"])
        self.assertIsNone(result["coach_assessment_error"])
        report = (self.directory / "report.html").read_text(encoding="utf-8")
        self.assertIn("Recording collected. Assistant analysis pending.", report)
        self.assertIn("No labels or forms required.", report)
        disclosure = re.search(r'<details id="evidenceTools"[^>]*>', report).group(0)
        self.assertNotIn("open", disclosure)
        self.assertIn("Detailed evidence tools", report)

    def test_coach_status_and_settings_are_preserved_without_inferred_changes(self):
        self.write_session([self.event(10, buttons=1)])
        for status in ("baseline_retained", "test_recommendation", "validated_recommendation", "awaiting_evidence"):
            assessment = self.assessment(status=status)
            self.write_assessment(assessment)
            result = self.generated_analysis()
            self.assertIsNone(result["coach_assessment_error"])
            self.assertEqual(result["coach_assessment"], assessment)

    def test_coach_schema_status_source_and_evidence_references_are_validated(self):
        self.write_session([self.event(10, buttons=1)])
        changes = [dict(schema=None), dict(status="optimal"), dict(source="algorithm"),
                   dict(created_at_utc="not a timestamp"),
                   dict(evidence=[dict(candidate_id="missing", frame_index=0, observation="Text")]),
                   dict(evidence=[dict(candidate_id="click-00001", frame_index=999, observation="Text")]),
                   dict(limitations="Not a list")]
        for change in changes:
            self.write_assessment(self.assessment(**change))
            result = self.generated_analysis()
            self.assertIsNone(result["coach_assessment"])
            self.assertIn("coach-assessment.json:", result["coach_assessment_error"])

    def test_coach_settings_reject_nonfinite_nonpositive_and_non_numeric_known_values(self):
        self.write_session([self.event(10, buttons=1)])
        bad_settings = [("dpi", 0), ("dpi", -1), ("dpi", 1200.0),
                        ("look_sensitivity", 0), ("look_sensitivity", -1),
                        ("scoped_multiplier", -1), ("normal_zoom_multiplier", 0),
                        ("look_sensitivity", float("nan")), ("scoped_multiplier", float("inf")),
                        ("dpi", True), ("look_sensitivity", "3.5"), ("custom_setting", []), ("custom_setting", float("inf"))]
        for field, value in bad_settings:
            assessment = self.assessment()
            assessment["settings"][field] = value
            self.write_assessment(assessment)
            result = self.generated_analysis()
            self.assertIsNone(result["coach_assessment"])
            self.assertIn(field, result["coach_assessment_error"])

    def test_malformed_coach_json_keeps_report_available_with_explicit_error(self):
        self.write_session([self.event(10, buttons=1)])
        (self.directory / "coach-assessment.json").write_text("{broken", encoding="utf-8")
        result = self.generated_analysis()
        self.assertIsNone(result["coach_assessment"])
        self.assertTrue(result["coach_assessment_error"])
        self.assertTrue((self.directory / "report.html").exists())

    def test_coach_text_is_escaped_before_embedding(self):
        self.write_session([self.event(10, buttons=1)])
        dangerous = '</script><script id="coach-injection">bad()</script>'
        self.write_assessment(self.assessment(headline=dangerous))
        result = self.generated_analysis()
        self.assertEqual(result["coach_assessment"]["headline"], dangerous)
        report = (self.directory / "report.html").read_text(encoding="utf-8")
        self.assertNotIn('<script id="coach-injection">', report)
        self.assertEqual(report.count("</script>"), 2)

    def test_settings_correction_merges_effective_context_and_preserves_raw_files(self):
        self.write_session([self.event(10, buttons=1)])
        (self.directory / "manifest.json").write_text('{"raw":"original"}', encoding="utf-8")
        original_files = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        correction = self.correction()
        self.write_correction(correction)
        session = self.generated_analysis()["session"]
        self.assertEqual(session["settings"]["scoped_multiplier"], 0.85)
        self.assertEqual(session["settings"]["dpi"], 1200)
        self.assertEqual(session["settings"]["look_sensitivity"], 3.5)
        self.assertEqual(session["settings"]["normal_zoom_multiplier"], 1.0)
        self.assertEqual(session["settings_as_recorded"], self.metadata["settings"])
        self.assertEqual(session["settings_as_recorded"]["scoped_multiplier"], 0.9)
        self.assertEqual(session["settings_correction"], correction)
        self.assertIn("post-capture user report", session["settings_source"])
        self.assertIn("recorded provenance", session["settings_source"])
        for name, contents in original_files.items():
            self.assertEqual((self.directory / name).read_bytes(), contents)

    def test_invalid_settings_correction_raises_useful_error(self):
        self.write_session([self.event(10, buttons=1)])
        for changes in (dict(schema="unknown"), dict(source="inferred"),
                        dict(reported_at_utc="invalid"), dict(settings={}),
                        dict(settings={"custom_setting": {"nested": 1}}), dict(settings={"scoped_multiplier": -1}),
                        dict(settings={"scoped_multiplier": float("nan")}),
                        dict(settings={"look_sensitivity": 0}), dict(settings={"dpi": True})):
            self.write_correction(self.correction(**changes))
            with self.assertRaisesRegex(ValueError, "settings-correction.json:"):
                analyze_session(self.directory)
        (self.directory / "settings-correction.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "settings-correction.json:"):
            analyze_session(self.directory)

    def test_settings_correction_does_not_rewrite_separately_authored_coach_settings(self):
        self.write_session([self.event(10, buttons=1)])
        assessment = self.assessment()
        self.write_assessment(assessment)
        self.write_correction(self.correction())
        result = self.generated_analysis()
        self.assertEqual(result["session"]["settings"]["scoped_multiplier"], 0.85)
        self.assertEqual(result["coach_assessment"], assessment)
        self.assertEqual(json.loads((self.directory / "coach-assessment.json").read_text(encoding="utf-8")),
                         assessment)

    def test_partial_unknown_and_extra_settings_are_preserved_without_defaults(self):
        cases = ({}, {"dpi": None}, {"look_sensitivity": 3.5, "scoped_multiplier": None,
                                    "invert_y": False, "aim_mode": "hold", "custom_curve": -2})
        for settings in cases:
            with self.subTest(settings=settings):
                self.metadata["settings"] = settings
                self.write_session([self.event(10, buttons=1)])
                assessment = self.assessment(settings=settings)
                self.write_assessment(assessment)
                data = self.generated_analysis()
                self.assertEqual(data["session"]["settings"], settings)
                self.assertEqual(data["coach_assessment"]["settings"], settings)
                self.assertIsNone(data["coach_assessment_error"])
        del self.metadata["settings"]
        self.write_session()
        self.assertEqual(analyze_session(self.directory)["session"]["settings"], {})

    def test_settings_are_not_limited_to_one_games_ranges(self):
        self.write_session([self.event(10, buttons=1)])
        settings = {"dpi": 49, "look_sensitivity": 175.5, "scoped_multiplier": 25,
                    "normal_zoom_multiplier": 30, "extra_setting": "game-native value"}
        self.write_assessment(self.assessment(settings=settings))
        data = self.generated_analysis()
        self.assertEqual(data["coach_assessment"]["settings"], settings)
        self.assertIsNone(data["coach_assessment_error"])

    def test_overview_evidence_can_omit_candidate_but_must_reference_a_recorded_frame(self):
        self.write_session()
        observations = [{"frame_index": 0, "observation": "Overview scene, no click."},
                        {"candidate_id": None, "frame_index": 2, "observation": "Later overview scene."}]
        self.write_assessment(self.assessment(settings={}, evidence=observations))
        data = self.generated_analysis()
        self.assertEqual(data["candidates"], [])
        self.assertIsNone(data["coach_assessment_error"])
        self.assertEqual([item["candidate_id"] for item in data["coach_assessment"]["evidence"]], [None, None])
        for frame_index in (True, None, 99):
            with self.assertRaisesRegex(ValueError, "frame_index"):
                validate_coach_assessment(self.assessment(evidence=[{"frame_index": frame_index,
                                                                    "observation": "Invalid frame."}]), data)

    def test_optional_assessment_session_id_is_checked_and_preserved(self):
        self.write_session([self.event(10, buttons=1)])
        analysis = analyze_session(self.directory)
        accepted = validate_coach_assessment(self.assessment(session_id="synthetic-test"), analysis)
        self.assertEqual(accepted["session_id"], "synthetic-test")
        with self.assertRaisesRegex(ValueError, "session_id"):
            validate_coach_assessment(self.assessment(session_id="different-session"), analysis)

    def test_settings_correction_can_clear_unknowns_and_add_game_specific_scalars(self):
        self.metadata["setting_sources"] = {"dpi": {"source": "user-reported"}}
        self.write_session()
        changes = {"dpi": None, "sensitivity_scale": "native slider", "invert_y": True}
        self.write_correction(self.correction(settings=changes))
        session = analyze_session(self.directory)["session"]
        self.assertIsNone(session["settings"]["dpi"])
        self.assertEqual(session["settings"]["sensitivity_scale"], "native slider")
        self.assertIs(session["settings"]["invert_y"], True)
        self.assertEqual(session["settings_as_recorded"], self.metadata["settings"])
        self.assertEqual(session["setting_sources"], self.metadata["setting_sources"])

    def write_manifest(self):
        manifest = {}
        for name in ("session.json", "mouse.csv", "frames.csv", "video.mp4"):
            path = self.directory / name
            if path.exists():
                entry = {"bytes": path.stat().st_size}
                if name != "video.mp4":
                    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest[name] = entry
        (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_review_cli_attaches_valid_assessment_and_verifies_without_changing_raw_files(self):
        self.write_session()
        (self.directory / "video.mp4").write_bytes(b"synthetic video placeholder")
        self.write_manifest()
        originals = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        assessment = self.assessment(settings={"dpi": None, "custom_setting": "value"},
                                     evidence=[{"frame_index": 2, "observation": "Overview frame."}])
        supplied = self.directory / "supplied-assessment.json"
        supplied.write_text(json.dumps(assessment), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(review_session.main([str(self.directory), "--assessment", str(supplied),
                                                  "--verify-manifest"]), 0)
        self.assertIn("Manifest verified", stdout.getvalue())
        for name, contents in originals.items():
            self.assertEqual((self.directory / name).read_bytes(), contents)
        data = json.loads((self.directory / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(data["coach_assessment"]["settings"], assessment["settings"])
        self.assertIsNone(data["coach_assessment"]["evidence"][0]["candidate_id"])
        verification = data["manifest_verification"]
        self.assertTrue(verification["verified"])
        self.assertEqual({entry["file"]: entry["sha256_verified"] for entry in verification["files"]},
                         {"frames.csv": True, "mouse.csv": True, "session.json": True, "video.mp4": False})

    def test_invalid_assessment_cli_preserves_prior_assessment_and_other_files(self):
        self.write_session([self.event(10, buttons=1)])
        self.write_assessment(self.assessment())
        supplied = self.directory / "invalid-assessment.json"
        supplied.write_text(json.dumps(self.assessment(evidence=[{"frame_index": 999, "observation": "Bad reference"}])),
                            encoding="utf-8")
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as result:
            review_session.main([str(self.directory), "--assessment", str(supplied)])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("frame_index", stderr.getvalue())
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_manifest_mismatch_fails_before_creating_report_or_assessment(self):
        self.write_session()
        self.write_manifest()
        path = self.directory / "mouse.csv"
        contents = path.read_bytes()
        path.write_bytes(b"X" + contents[1:])  # Equal length catches hash verification, not just size.
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch for mouse.csv"):
            review_session.review_session(self.directory, check_manifest=True)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_manifest_rejects_empty_missing_and_traversal_entries(self):
        self.write_session()
        valid = self.write_manifest()
        for manifest in ({}, dict(valid, **{"../outside": {"bytes": 1}}),
                         dict(valid, **{"mouse.csv": {"bytes": 0, "sha256": "invalid"}})):
            (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                review_session.verify_manifest(self.directory)

    def test_offline_help_imports_no_windows_or_live_capture_modules(self):
        script_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        command = "import sys; sys.path.insert(0, {!r}); import review_session; ".format(script_dir)
        command += "assert not any(name in sys.modules for name in ('aim_observer.capture', 'aim_observer.raw_input', 'tkinter', 'ctypes')); "
        command += "review_session.main(['--help'])"
        result = subprocess.run([sys.executable, "-c", command], cwd=str(self.directory),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--assessment", result.stdout)
        self.assertIn("--verify-manifest", result.stdout)


if __name__ == "__main__":
    unittest.main()
