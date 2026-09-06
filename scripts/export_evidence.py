"""Export actual recorded frames for offline visual review (Python 3.8+).

Requires FFmpeg on PATH or --ffmpeg. This script never captures a desktop,
reads live input, accesses a game, or calls an AI service. Click candidates
are input events, not confirmed shots, and right-held state is not ADS.
"""

import argparse
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


class EvidenceError(RuntimeError):
    """An export could not safely produce a complete evidence index."""


def _read_json(path):
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            value = json.load(source)
    except (OSError, ValueError) as error:
        raise EvidenceError("{}: {}".format(path.name, error)) from error
    if not isinstance(value, dict):
        raise EvidenceError("{} must contain a JSON object".format(path.name))
    return value


def _nonnegative_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceError("{} must be a nonnegative integer".format(name))
    return value


def _read_frames(path):
    frames = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            required = {"frame_index", "t_start_ns", "t_end_ns"}
            if not required.issubset(reader.fieldnames or []):
                raise EvidenceError("frames.csv must contain {}".format(", ".join(sorted(required))))
            for line, row in enumerate(reader, 2):
                try:
                    frame = {key: int(row[key]) for key in required}
                except (TypeError, ValueError) as error:
                    raise EvidenceError("frames.csv line {} has invalid integers".format(line)) from error
                if frame["frame_index"] != len(frames):
                    raise EvidenceError("frames.csv line {}: frame indices must be consecutive from zero; "
                                        "cannot verify video correspondence".format(line))
                start, end = frame["t_start_ns"], frame["t_end_ns"]
                if start < 0 or end < start:
                    raise EvidenceError("frames.csv line {} has an invalid capture bracket".format(line))
                frame["t_mid_ns"] = (start + end) // 2
                if frames and frame["t_mid_ns"] < frames[-1]["t_mid_ns"]:
                    raise EvidenceError("frames.csv capture midpoints are out of order at line {}".format(line))
                frames.append(frame)
    except OSError as error:
        raise EvidenceError("frames.csv: {}".format(error)) from error
    if not frames:
        raise EvidenceError("frames.csv contains no recorded frames")
    return frames


def _evenly_spaced(items, count):
    """Sample ordered positions, including both ends when count is at least two."""
    count = min(count, len(items))
    if not count:
        return []
    if count == 1:
        return [items[(len(items) - 1) // 2]]
    # Integer nearest-position rounding avoids floating-point index drift.
    return [items[(i * (len(items) - 1) + (count - 1) // 2) // (count - 1)]
            for i in range(count)]


def _load_candidates(path, session_id, fps, frames):
    analysis = _read_json(path)
    if analysis.get("schema") != "aim-observer/review-v1":
        raise EvidenceError("analysis.json must use aim-observer/review-v1; run review_session.py first")
    if analysis.get("session_id") != session_id:
        raise EvidenceError("analysis.json belongs to a different session; rebuild the analysis")
    if isinstance(analysis.get("fps"), bool) or analysis.get("fps") != fps:
        raise EvidenceError("analysis.json FPS differs from session.json; rebuild the analysis")
    candidates = analysis.get("candidates")
    if not isinstance(candidates, list):
        raise EvidenceError("analysis.json candidates must be a list")
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise EvidenceError("Every analysis candidate must be an object")
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen:
            raise EvidenceError("Analysis candidate IDs must be nonempty and unique")
        seen.add(candidate_id)
        _nonnegative_integer(candidate.get("t_ns"), "{} t_ns".format(candidate_id))
        mapped = candidate.get("mapped_frame")
        if not isinstance(mapped, dict):
            raise EvidenceError("{} has no mapped_frame; rebuild the analysis".format(candidate_id))
        index = _nonnegative_integer(mapped.get("frame_index"), "{} mapped frame_index".format(candidate_id))
        if index >= len(frames):
            raise EvidenceError("{} maps to an unrecorded frame; rebuild the analysis".format(candidate_id))
        for key in ("t_start_ns", "t_end_ns", "t_mid_ns"):
            if key in mapped and mapped[key] != frames[index][key]:
                raise EvidenceError("{} mapped {} differs from frames.csv; rebuild the analysis".format(
                    candidate_id, key))
    return sorted(candidates, key=lambda candidate: (candidate["t_ns"], candidate["id"]))


def _find_ffmpeg(value):
    executable = str(Path(value).expanduser()) if value is not None else "ffmpeg"
    located = shutil.which(executable)
    if located:
        return located
    if value is not None and Path(executable).is_file():
        return str(Path(executable).resolve())
    raise EvidenceError("FFmpeg was not found. Install FFmpeg on PATH or pass --ffmpeg /path/to/ffmpeg")


def _extract_frames(executable, video, indices, staging):
    # A script keeps large selections below Windows' command-line length limit.
    filter_path = staging / "select-frames.txt"
    filter_path.write_text("select=" + "+".join("eq(n\\,{})".format(index) for index in indices),
                           encoding="utf-8")
    pattern = staging / "decoded-%09d.png"
    command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
               "-i", str(video), "-map", "0:v:0", "-an", "-sn", "-dn",
               "-filter_script:v", str(filter_path), "-vsync", "0", "-threads", "1",
               "-frames:v", str(len(indices)), "-start_number", "0", str(pattern)]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as error:
        raise EvidenceError("Could not start FFmpeg: {}".format(error)) from error
    if result.returncode:
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()[-4000:]
        raise EvidenceError("FFmpeg failed (exit {}): {}".format(result.returncode, detail))
    files = sorted(staging.glob("decoded-*.png"))
    expected = [staging / "decoded-{:09d}.png".format(position) for position in range(len(indices))]
    if files != expected or any(path.stat().st_size == 0 for path in files):
        raise EvidenceError("FFmpeg produced {} images for {} selected frames. The video may be truncated "
                            "or inconsistent with frames.csv; no evidence index was written".format(
                                len(files), len(indices)))
    for position, index in enumerate(indices):
        expected[position].rename(staging / "frame-{:09d}.png".format(index))


def _source_path(path, output):
    try:
        return Path(os.path.relpath(str(path), str(output))).as_posix()
    except ValueError:  # Different drives on Windows cannot have a relative path.
        return path.as_posix()


def export_evidence(session, output=None, click_ids=None, overview_count=12,
                    before_frames=6, after_frames=6, click_count=6,
                    overview_only=False, ffmpeg=None):
    """Export unique PNGs and return evidence.json, refusing nonempty output dirs.

    Selected clicks use analysis.json's mapped frame index. Neighbors are
    consecutive recorded frames, even across capture gaps; their actual timing
    is retained. Overview and default click samples span the ordered recording,
    not the first N items. No source recording or analysis file is modified.
    """
    for name, value in (("overview_count", overview_count), ("before_frames", before_frames),
                        ("after_frames", after_frames), ("click_count", click_count)):
        _nonnegative_integer(value, name)
    if isinstance(click_ids, str):
        raise EvidenceError("click_ids must be a sequence of IDs, not one string")
    requested_ids = list(dict.fromkeys(click_ids or []))
    if any(not isinstance(value, str) or not value for value in requested_ids):
        raise EvidenceError("Click IDs must be nonempty strings")
    if overview_only and requested_ids:
        raise EvidenceError("--overview-only cannot be combined with selected click IDs")
    session = Path(session).expanduser().resolve()
    output = Path(output).expanduser().resolve() if output is not None else session / "evidence"
    metadata = _read_json(session / "session.json")
    if metadata.get("schema") != "aim-observer/session-v1":
        raise EvidenceError("session.json must use aim-observer/session-v1")
    try:
        raw_fps = metadata["capture"]["fps"]
        fps = float(raw_fps)
        if isinstance(raw_fps, bool) or not math.isfinite(fps) or not 0 < fps <= 1000:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise EvidenceError("session.json must contain capture.fps between 0 and 1000")
    frames = _read_frames(session / "frames.csv")
    video = session / "video.mp4"
    if not video.is_file():
        raise EvidenceError("video.mp4 was not found in the session directory")
    session_id = str(metadata.get("session_id", session.name))
    selected = []
    candidates = []
    load_clicks = not overview_only and (requested_ids or click_count)
    if load_clicks:
        candidates = _load_candidates(session / "analysis.json", session_id, fps, frames)
        by_id = {candidate["id"]: candidate for candidate in candidates}
        unknown = [value for value in requested_ids if value not in by_id]
        if unknown:
            raise EvidenceError("Unknown click ID(s): {}".format(", ".join(unknown)))
        selected = [by_id[value] for value in requested_ids] if requested_ids else _evenly_spaced(candidates, click_count)

    def sample(frame):
        value = dict(frame)
        value["media_time_s"] = frame["frame_index"] / fps
        value["image"] = "frame-{:09d}.png".format(frame["frame_index"])
        return value

    overview = [sample(frame) for frame in _evenly_spaced(frames, overview_count)]
    requested = {frame["frame_index"] for frame in overview}
    evidence = []
    for candidate in selected:
        center = candidate["mapped_frame"]["frame_index"]
        begin, end = max(0, center - before_frames), min(len(frames), center + after_frames + 1)
        samples = []
        for frame in frames[begin:end]:
            item = sample(frame)
            item["relative_frame_offset"] = frame["frame_index"] - center
            item["actual_offset_ms"] = (frame["t_mid_ns"] - candidate["t_ns"]) / 1_000_000
            samples.append(item)
            requested.add(frame["frame_index"])
        context_keys = ("device", "mouse_row_number", "right_held_at_click", "right_transition_in_click_packet",
                        "window_start_ns", "window_end_ns", "window_overlaps_capture_gap",
                        "window_background_events", "window_absolute_motion_events")
        evidence.append({"candidate_id": candidate["id"], "candidate_t_ns": candidate["t_ns"],
                         "context": {key: candidate[key] for key in context_keys if key in candidate},
                         "mapped_frame": sample(frames[center]),
                         "before_frames_available": center - begin, "after_frames_available": end - center - 1,
                         "samples": samples})
    indices = sorted(requested)
    if not indices:
        raise EvidenceError("No frames selected; increase --overview-count or select click candidates")
    executable = _find_ffmpeg(ffmpeg)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise EvidenceError("Output directory is not empty: {}. Choose a new --output directory".format(output))
    output.mkdir(parents=True, exist_ok=True)
    index = {"schema": "aim-observer/evidence-v1", "session_id": session_id, "fps": fps,
             "source_session": _source_path(session, output),
             "source_video": _source_path(video, output),
             "source_frames": _source_path(session / "frames.csv", output),
             "source_analysis": _source_path(session / "analysis.json", output) if load_clicks else None,
             "selection": {"overview_method": "evenly_spaced_recorded_frame_positions",
                           "overview_count_requested": overview_count,
                           "click_method": "explicit_ids" if requested_ids else "evenly_spaced_candidate_positions"
                           if load_clicks else "none",
                           "click_count_requested": len(requested_ids) if requested_ids else click_count if load_clicks else 0,
                           "available_click_candidates": len(candidates) if load_clicks else None,
                           "before_frames_requested": before_frames, "after_frames_requested": after_frames},
             "notes": ["Click candidates are foreground left-button-down events, not confirmed shots or hits.",
                       "Right-held state is recorded button context, not visually verified ADS or scoped state.",
                       "Images were selected by encoded frame index. Media time is frame_index / capture.fps.",
                       "Capture timestamps come from frames.csv; brackets are acquisition times, not guaranteed presentation times.",
                       "Consecutive recorded frames may span a capture gap; inspect actual timestamps and offsets.",
                       "Evenly spaced samples cover the ordered recording but do not establish representative gameplay outcomes."],
             "overview": overview, "candidates": evidence,
             "frames": [sample(frames[position]) for position in indices]}
    # Keep incomplete decodes out of the visible evidence files and publish the
    # index last. The temporary directory contains only this export's own files.
    with tempfile.TemporaryDirectory(prefix=".exporting-", dir=str(output)) as temporary:
        staging = Path(temporary)
        _extract_frames(executable, video, indices, staging)
        if any(path != staging for path in output.iterdir()):
            raise EvidenceError("Output changed during export; choose a new --output directory")
        (staging / "evidence.json").write_text(json.dumps(index, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                                               encoding="utf-8")
        for frame_index in indices:
            name = "frame-{:09d}.png".format(frame_index)
            (staging / name).rename(output / name)
        (staging / "evidence.json").rename(output / "evidence.json")
    return output / "evidence.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="Recorded session directory")
    parser.add_argument("--output", "--output-dir", type=Path, help="Empty output directory (default: SESSION/evidence)")
    parser.add_argument("--click", action="append", default=[], metavar="ID", help="Click ID to export; repeat for multiple IDs")
    parser.add_argument("--clicks", action="append", default=[], metavar="ID,ID", help="Comma-separated click IDs")
    parser.add_argument("--overview-count", type=int, default=12, help="Overview samples across all recorded frames (default: 12; 0 disables)")
    parser.add_argument("--before-frames", type=int, default=6, help="Consecutive frames before each mapped frame (default: 6)")
    parser.add_argument("--after-frames", type=int, default=6, help="Consecutive frames after each mapped frame (default: 6)")
    parser.add_argument("--click-count", type=int, default=6, help="Evenly spaced click candidates when IDs are omitted (default: 6)")
    parser.add_argument("--overview-only", action="store_true", help="Export overview only; analysis.json is not required")
    parser.add_argument("--ffmpeg", help="FFmpeg executable path (default: search PATH)")
    args = parser.parse_args(argv)
    click_ids = args.click + [item.strip() for group in args.clicks for item in group.split(",")]
    try:
        result = export_evidence(args.session, output=args.output, click_ids=click_ids,
                                 overview_count=args.overview_count, before_frames=args.before_frames,
                                 after_frames=args.after_frames, click_count=args.click_count,
                                 overview_only=args.overview_only, ffmpeg=args.ffmpeg)
    except (EvidenceError, OSError) as error:
        parser.exit(2, "error: {}\n".format(error))
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
