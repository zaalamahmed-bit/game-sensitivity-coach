"""Offline, descriptive review of a locally recorded aim-observer session.

No image recognition, game access, network access, or input injection is used.
Only reviewer-entered labels describe aim outcome, weapon, or scoped state.
"""

import bisect
import csv
import html
import json
import math
import re
import statistics
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .profiles import validate_setting_value


WINDOW_BEFORE_NS = 750_000_000
WINDOW_AFTER_NS = 500_000_000
LEFT_DOWN = 0x0001
RIGHT_DOWN = 0x0004
RIGHT_UP = 0x0008
ABSOLUTE_MOTION = 0x0001
TRACE_BIN_NS = 5_000_000
COACH_STATUSES = {"baseline_retained", "test_recommendation", "validated_recommendation",
                  "awaiting_evidence"}


def _apply_settings_correction(directory: Path, metadata: Dict[str, Any]) -> None:
    """Merge a user-reported sidecar into derived context, never the raw files."""
    path = directory / "settings-correction.json"
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            correction = json.load(source)
        if not isinstance(correction, dict) or correction.get("schema") != "aim-observer/settings-correction-v1":
            raise ValueError("Missing or unsupported settings-correction schema")
        if correction.get("source") != "user-reported":
            raise ValueError("Correction source must be user-reported")
        reported = correction.get("reported_at_utc")
        if not isinstance(reported, str):
            raise ValueError("reported_at_utc must be an ISO 8601 UTC timestamp")
        try:
            if datetime.fromisoformat(reported.replace("Z", "+00:00")).utcoffset() != timedelta(0):
                raise ValueError()
        except ValueError:
            raise ValueError("reported_at_utc must be an ISO 8601 UTC timestamp")
        if not isinstance(correction.get("note"), str) or not correction["note"].strip():
            raise ValueError("Correction note must be a nonempty string")
        settings = correction.get("settings")
        if not isinstance(settings, dict) or not settings:
            raise ValueError("Correction settings must be a nonempty object; unknown values may be null")
        for field, value in settings.items():
            validate_setting_value(field, value, "Correction settings.")
        if not isinstance(metadata.get("settings"), dict):
            raise ValueError("session.json settings must be an object before applying a correction")
    except (OSError, ValueError, TypeError, OverflowError) as error:
        raise ValueError("settings-correction.json: {}".format(error)) from error
    recorded = dict(metadata["settings"])
    metadata["settings_as_recorded"] = recorded
    metadata["settings"] = dict(recorded, **settings)
    metadata["settings_correction"] = correction
    metadata["settings_source"] = (
        "Effective context includes a post-capture user report for {} from settings-correction.json. "
        "Other values retain their recorded provenance; a null value means unknown."
        .format(", ".join(sorted(settings))))


def _integer(value: Any) -> int:
    value = str(value).strip()
    return int(value, 16) if value.lower().startswith("0x") else int(value)


def _read_frames(path: Path) -> tuple:
    frames = []
    invalid = 0
    duplicates = 0
    seen = set()
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            try:
                index = _integer(row["frame_index"])
                start = _integer(row["t_start_ns"])
                end = _integer(row["t_end_ns"])
                if index < 0 or start < 0 or end < start:
                    raise ValueError("Invalid frame capture bracket")
            except (ValueError, TypeError, KeyError):
                invalid += 1
                continue
            if index in seen:
                duplicates += 1
                continue
            seen.add(index)
            frames.append({"frame_index": index, "t_start_ns": start,
                           "t_end_ns": end, "t_mid_ns": (start + end) // 2})
    frames.sort(key=lambda frame: (frame["t_mid_ns"], frame["frame_index"]))
    return frames, invalid, duplicates


def _iter_mouse(path: Path, counts: Dict[str, Any]):
    """Stream the recorder's monotonic log without retaining a whole match."""
    previous_time = -1
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for row_number, row in enumerate(csv.DictReader(source)):
            try:
                event = {key: _integer(row[key]) for key in
                         ("t_ns", "dx", "dy", "flags", "button_flags",
                          "button_data", "foreground")}
                if event["t_ns"] < 0 or event["foreground"] not in (0, 1):
                    raise ValueError("Invalid mouse timestamp or focus state")
            except (ValueError, TypeError, KeyError):
                counts["invalid_mouse_rows"] += 1
                continue
            if event["t_ns"] < previous_time:
                counts["out_of_order_mouse_rows"] += 1
                continue
            previous_time = event["t_ns"]
            event["device"] = str(row.get("device", "unknown"))
            event["row_number"] = row_number
            counts["valid_mouse_rows"] += 1
            counts["foreground_events"] += event["foreground"]
            counts["background_events"] += not event["foreground"]
            counts["absolute_motion_events"] += bool(event["flags"] & ABSOLUTE_MOTION)
            counts["foreground_relative_motion_events"] += bool(
                event["foreground"] and not event["flags"] & ABSOLUTE_MOTION
                and (event["dx"] or event["dy"]))
            counts["background_left_down_events"] += bool(
                not event["foreground"] and event["button_flags"] & LEFT_DOWN)
            if event["foreground"]:
                counts["_devices"].add(event["device"])
            yield event


def map_timestamp_to_frame(t_ns: int, frames: List[Dict[str, Any]],
                           fps: float,
                           midpoints: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
    """Find the nearest actual capture midpoint, then its encoded media time.

    ``frames`` must be sorted by midpoint. A tie chooses the earlier capture.
    Wall time divided by FPS is deliberately never used as a frame index.
    """
    if not frames:
        return None
    if fps <= 0:
        raise ValueError("Capture FPS must be positive")
    times = midpoints if midpoints is not None else [f["t_mid_ns"] for f in frames]
    insertion = bisect.bisect_left(times, t_ns)
    possibilities = [i for i in (insertion - 1, insertion) if 0 <= i < len(frames)]
    chosen = min(possibilities, key=lambda i: (abs(times[i] - t_ns), times[i]))
    frame = dict(frames[chosen])
    frame["media_time_s"] = frame["frame_index"] / fps
    frame["distance_ms"] = abs(frame["t_mid_ns"] - t_ns) / 1_000_000
    return frame


class _WindowAccumulator:
    """Exact packet metrics and a bounded display trace for one click window."""

    def __init__(self, click_ns: int):
        self.click_ns = click_ns
        self.devices = {}
        self.background_events = 0
        self.absolute_events = 0

    def add(self, event: Dict[str, Any]) -> None:
        self.background_events += not event["foreground"]
        self.absolute_events += bool(event["flags"] & ABSOLUTE_MOTION)
        if not event["foreground"] or event["flags"] & ABSOLUTE_MOTION:
            return
        if not event["dx"] and not event["dy"]:
            return
        device = event["device"]
        if device not in self.devices:
            self.devices[device] = {"device": device, "relative_motion_events": 0,
                                    "net_dx": 0, "net_dy": 0, "absolute_dx_sum": 0,
                                    "absolute_dy_sum": 0, "x_direction_reversals": 0,
                                    "y_direction_reversals": 0, "trace": [],
                                    "_x_sign": 0, "_y_sign": 0, "_last_bin": None}
        result = self.devices[device]
        result["relative_motion_events"] += 1
        for axis in ("x", "y"):
            delta = event["d" + axis]
            result["net_d" + axis] += delta
            result["absolute_d" + axis + "_sum"] += abs(delta)
            sign = (delta > 0) - (delta < 0)
            previous = result["_" + axis + "_sign"]
            if sign and previous and sign != previous:
                result[axis + "_direction_reversals"] += 1
            if sign:
                result["_" + axis + "_sign"] = sign
        point = [
            (event["t_ns"] - self.click_ns) / 1_000_000,
            result["net_dx"], result["net_dy"],
        ]
        time_bin = (event["t_ns"] - self.click_ns) // TRACE_BIN_NS
        if result["_last_bin"] == time_bin:
            result["trace"][-1] = point
        else:
            result["trace"].append(point)
            result["_last_bin"] = time_bin

    def finish(self, candidate: Dict[str, Any]) -> None:
        for result in self.devices.values():
            del result["_x_sign"]
            del result["_y_sign"]
            del result["_last_bin"]
            result["trace_original_points"] = result["relative_motion_events"]
            result["trace_downsampled"] = len(result["trace"]) < result["trace_original_points"]
            result["trace_bin_ms"] = TRACE_BIN_NS / 1_000_000
        candidate["motion_by_device"] = list(self.devices.values())
        candidate["window_background_events"] = self.background_events
        candidate["window_absolute_motion_events"] = self.absolute_events


def analyze_session(session_dir: Any) -> Dict[str, Any]:
    """Read session files and return measurements with no inferred aim labels."""
    directory = Path(session_dir)
    with (directory / "session.json").open("r", encoding="utf-8-sig") as source:
        metadata = json.load(source)
    if not isinstance(metadata, dict):
        raise ValueError("session.json must contain a JSON object")
    settings = metadata.setdefault("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("session.json settings must be an object; unknown values may be null")
    for field, value in settings.items():
        validate_setting_value(field, value, "session.json settings.")
    _apply_settings_correction(directory, metadata)
    try:
        raw_fps = metadata["capture"]["fps"]
        fps = float(raw_fps)
        if isinstance(raw_fps, bool) or not 0 < fps <= 1000:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError("session.json must contain capture.fps between 0 and 1000")
    frames, invalid_frames, duplicate_frames = _read_frames(directory / "frames.csv")
    midpoints = [frame["t_mid_ns"] for frame in frames]
    period_ns = 1_000_000_000 / fps
    capture_gaps = []
    for previous, frame in zip(frames, frames[1:]):
        delta = frame["t_mid_ns"] - previous["t_mid_ns"]
        if delta > period_ns * 1.5:
            capture_gaps.append({
                "after_frame_index": previous["frame_index"],
                "before_frame_index": frame["frame_index"],
                "from_t_ns": previous["t_mid_ns"], "to_t_ns": frame["t_mid_ns"],
                "midpoint_interval_ms": delta / 1_000_000,
                "excess_over_nominal_ms": (delta - period_ns) / 1_000_000,
            })
    right_held = {}
    candidates = []
    mouse_counts = {"valid_mouse_rows": 0, "invalid_mouse_rows": 0,
                    "out_of_order_mouse_rows": 0, "foreground_events": 0,
                    "background_events": 0, "absolute_motion_events": 0,
                    "foreground_relative_motion_events": 0,
                    "background_left_down_events": 0, "_devices": set()}
    history = deque()
    pending = deque()
    for event in _iter_mouse(directory / "mouse.csv", mouse_counts):
        while pending and pending[0][0]["window_end_ns"] < event["t_ns"]:
            completed, accumulator = pending.popleft()
            accumulator.finish(completed)
        for unused_candidate, accumulator in pending:
            accumulator.add(event)
        cutoff = event["t_ns"] - WINDOW_BEFORE_NS
        while history and history[0]["t_ns"] < cutoff:
            history.popleft()
        history.append(event)
        if not event["foreground"]:
            # Recorders may emit only a focus-loss sentinel, then omit all
            # background input. Never carry an old pressed state across that gap.
            right_held.clear()
            continue
        device = event["device"]
        buttons = event["button_flags"]
        if buttons & RIGHT_DOWN:
            right_held[device] = True
        if buttons & RIGHT_UP:
            right_held[device] = False
        if not buttons & LEFT_DOWN:
            continue
        click_ns = event["t_ns"]
        begin_ns = max(0, click_ns - WINDOW_BEFORE_NS)
        end_ns = click_ns + WINDOW_AFTER_NS
        mapped = map_timestamp_to_frame(click_ns, frames, fps, midpoints)
        candidate = {
            "id": "click-{:05d}".format(len(candidates) + 1),
            "t_ns": click_ns, "mouse_row_number": event["row_number"],
            "device": device, "right_held_at_click": right_held.get(device),
            "right_transition_in_click_packet": bool(buttons & (RIGHT_DOWN | RIGHT_UP)),
            "mapped_frame": mapped,
            "window_start_ns": begin_ns, "window_end_ns": end_ns,
            "window_overlaps_capture_gap": any(
                gap["from_t_ns"] <= end_ns and gap["to_t_ns"] >= begin_ns
                for gap in capture_gaps),
        }
        candidates.append(candidate)
        accumulator = _WindowAccumulator(click_ns)
        for previous_event in history:
            accumulator.add(previous_event)
        pending.append((candidate, accumulator))
    while pending:
        completed, accumulator = pending.popleft()
        accumulator.finish(completed)
    encoded_order = sorted(frames, key=lambda frame: frame["frame_index"])
    frame_indices = [frame["frame_index"] for frame in encoded_order]
    intervals_ms = sorted((b["t_mid_ns"] - a["t_mid_ns"]) / 1_000_000
                          for a, b in zip(frames, frames[1:]))
    median_interval_ms = statistics.median(intervals_ms) if intervals_ms else None
    quality = {
        "left_down_candidates": len(candidates),
        "valid_frame_rows": len(frames), "invalid_frame_rows": invalid_frames,
        "duplicate_frame_indices": duplicate_frames,
        "frame_indices_contiguous_from_zero": frame_indices == list(range(len(frame_indices))),
        "capture_gap_count": len(capture_gaps),
        "capture_gap_threshold_ms": period_ns * 1.5 / 1_000_000,
        "capture_gap_excess_total_ms": sum(gap["excess_over_nominal_ms"] for gap in capture_gaps),
        "max_capture_bracket_ms": max(
            ((f["t_end_ns"] - f["t_start_ns"]) / 1_000_000 for f in frames), default=0),
        "max_frame_midpoint_interval_ms": max(
            ((b["t_mid_ns"] - a["t_mid_ns"]) / 1_000_000 for a, b in zip(frames, frames[1:])),
            default=0),
        "median_frame_midpoint_interval_ms": median_interval_ms,
        "p95_frame_midpoint_interval_ms": intervals_ms[math.ceil(len(intervals_ms) * 0.95) - 1]
        if intervals_ms else None,
        "observed_median_fps": 1000 / median_interval_ms if median_interval_ms else None,
        "first_frame_midpoint_ns": frames[0]["t_mid_ns"] if frames else None,
        "last_frame_midpoint_ns": frames[-1]["t_mid_ns"] if frames else None,
        "mouse_device_count": len(mouse_counts.pop("_devices")),
        "dropped_input_events": metadata.get("dropped_input_events", 0),
        "recording_status": metadata.get("status", "unknown"),
        "recording_errors": metadata.get("errors", []),
    }
    quality.update(mouse_counts)
    return {
        "schema": "aim-observer/review-v1", "session_id": str(metadata.get("session_id", directory.name)),
        "session": metadata, "fps": fps, "quality": quality,
        "frames": encoded_order, "capture_gaps": capture_gaps, "candidates": candidates,
        "interpretation": {
            "candidates": "Foreground left-button-down packets; not confirmed shots or hits.",
            "right_held_at_click": "Observed right-button state for the clicking device, not ADS detection. "
                                   "Null means unknown at session start or after focus loss, until a right "
                                   "transition is observed. Background packets clear state. Same-packet right "
                                   "transitions are applied before the click; right-up wins if both flags occur.",
            "movement": "Foreground relative raw mouse counts, accumulated independently per device. "
                        "Absolute-motion packets are excluded. Counts are not screen pixels or aim angles. "
                        "Display traces keep the last cumulative value in each 5 ms bin; metrics use all packets.",
            "input_order": "Mouse CSV is streamed in recorded order, preserving equal-timestamp ordering. "
                           "Rows earlier than the last accepted timestamp are skipped and counted in quality. "
                           "Only a 750 ms input history and pending 500 ms windows are retained during reading.",
            "reversals": "Nonzero sign changes per axis, ignoring zero deltas; descriptive movement only. "
                         "A reversal does not establish overshoot, target position, or aim quality.",
            "sync": "Nearest actual capture midpoint maps wall time to encoded frame_index / requested FPS. "
                    "The capture bracket is acquisition timing, not a guaranteed presentation timestamp.",
            "gaps": "Midpoint intervals over 1.5 nominal frame periods flag capture gaps; playback uses CFR. "
                    "The video may therefore compress real elapsed time during capture gaps.",
            "manual_review": "Scoped state, weapon, aim outcome, intended target and crosshair positions "
                             "are reviewer labels only. No sensitivity recommendation is inferred.",
        },
    }


def validate_coach_assessment(assessment: Any, analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Validate supplied observations without inventing settings or conclusions.

    Settings may be partial or unknown, and extra game-specific scalar settings
    are retained. An overview observation can omit candidate_id (or use null),
    but every observation must reference an actual recorded frame.
    """
    if not isinstance(assessment, dict):
        raise ValueError("Assessment must be a JSON object")
    if assessment.get("schema") != "aim-observer/coach-assessment-v1":
        raise ValueError("Missing or unsupported assessment schema")
    if not isinstance(assessment.get("status"), str) or assessment["status"] not in COACH_STATUSES:
        raise ValueError("Unknown assessment status")
    if assessment.get("source") != "assistant-visual-review":
        raise ValueError("Assessment source must be assistant-visual-review")
    if "session_id" in assessment and assessment["session_id"] != analysis["session_id"]:
        raise ValueError("Assessment session_id does not match this session")
    for field in ("headline", "summary", "next_step", "created_at_utc"):
        if not isinstance(assessment.get(field), str) or not assessment[field].strip():
            raise ValueError("Assessment {} must be a nonempty string".format(field))
    try:
        created = datetime.fromisoformat(assessment["created_at_utc"].replace("Z", "+00:00"))
        if created.utcoffset() != timedelta(0):
            raise ValueError()
    except ValueError:
        raise ValueError("Assessment created_at_utc must be an ISO 8601 UTC timestamp")
    settings = assessment.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("Assessment settings must be an object; unknown values may be null")
    for field, value in settings.items():
        validate_setting_value(field, value, "Assessment settings.")
    evidence = assessment.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("Assessment evidence must be a list")
    candidate_ids = {candidate["id"] for candidate in analysis["candidates"]}
    frame_indices = {frame["frame_index"] for frame in analysis["frames"]}
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("Every evidence item must be an object")
        candidate_id = item.get("candidate_id")
        if candidate_id is not None and (not isinstance(candidate_id, str) or candidate_id not in candidate_ids):
            raise ValueError("Evidence candidate_id must reference a candidate in this session or be null")
        frame_index = item.get("frame_index")
        if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index not in frame_indices:
            raise ValueError("Evidence frame_index must reference a recorded frame in this session")
        if not isinstance(item.get("observation"), str) or not item["observation"].strip():
            raise ValueError("Evidence observation must be a nonempty string")
    limitations = assessment.get("limitations")
    if not isinstance(limitations, list) or any(not isinstance(item, str) or not item.strip() for item in limitations):
        raise ValueError("Assessment limitations must be a list of nonempty strings")
    # Keep the supported contract. Status and individual setting values are
    # supplied by the reviewer; omitted settings are never filled with defaults.
    accepted = {key: assessment[key] for key in (
        "schema", "status", "headline", "summary", "limitations", "next_step", "created_at_utc", "source")}
    if "session_id" in assessment:
        accepted["session_id"] = assessment["session_id"]
    accepted["settings"] = dict(settings)
    accepted["evidence"] = [{"candidate_id": item.get("candidate_id"), "frame_index": item["frame_index"],
                             "observation": item["observation"]} for item in evidence]
    return accepted


def _load_coach_assessment(directory: Path, analysis: Dict[str, Any]) -> tuple:
    """Load an assistant-authored assessment; never derive settings from counts."""
    path = directory / "coach-assessment.json"
    if not path.exists():
        return None, None
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            assessment = json.load(source)
        return validate_coach_assessment(assessment, analysis), None
    except (OSError, ValueError, TypeError, OverflowError) as error:
        return None, "coach-assessment.json: {}".format(error)


def generate_review(session_dir: Any, analysis: Optional[Dict[str, Any]] = None) -> Path:
    """Write analysis.json and a self-contained report.html beside video.mp4."""
    directory = Path(session_dir)
    if analysis is None:
        analysis = analyze_session(directory)
    assessment, assessment_error = _load_coach_assessment(directory, analysis)
    analysis["coach_assessment"] = assessment
    analysis["coach_assessment_error"] = assessment_error
    serialized = json.dumps(analysis, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # JSON lives in a non-executable script element, but must not close that element.
    safe_data = (serialized.replace("&", "\\u0026").replace("<", "\\u003c")
                 .replace(">", "\\u003e").replace("\u2028", "\\u2028")
                 .replace("\u2029", "\\u2029"))
    template = Path(__file__).with_name("review_template.html").read_text(encoding="utf-8")
    replacements = {"__TITLE__": html.escape(analysis["session_id"], quote=True),
                    "__DATA__": safe_data}
    rendered = re.sub(r"__TITLE__|__DATA__", lambda match: replacements[match.group(0)], template)
    (directory / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    report_path = directory / "report.html"
    report_path.write_text(rendered, encoding="utf-8")
    return report_path
