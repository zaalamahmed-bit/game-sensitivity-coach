#!/usr/bin/env python3
"""Bounded Windows gameplay recording. Imports no capture API for --help/control.

Examples:
  python capture_gameplay.py --game "Example Game" --seconds 480 --wait-for-game 60
  python capture_gameplay.py control SESSION/control.json --extend-seconds 480
  python capture_gameplay.py control SESSION/control.json --stop
"""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import sys
import time
import unicodedata
import uuid

from aim_observer.profiles import KNOWN_SETTINGS, load_profile, validate_settings


CONTROL_SCHEMA = "game-sensitivity/capture-control-v1"
HARD_LIMIT_SECONDS = 14400


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def bounded_number(value):
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= HARD_LIMIT_SECONDS:
        raise argparse.ArgumentTypeError("Duration must be between 1 and 14400 seconds")
    return value


class CaptureDeadline:
    """A monotonic deadline with replay-safe, explicitly bounded extensions."""
    def __init__(self, start, seconds, max_total_seconds):
        if not (1 <= seconds <= max_total_seconds <= HARD_LIMIT_SECONDS):
            raise ValueError("Require 1 <= seconds <= max-total-seconds <= 14400")
        self.start = start
        self.deadline = start + seconds
        self.max_total_seconds = max_total_seconds
        self.stopped = False
        self.seen = set()

    def apply(self, document):
        if not isinstance(document, dict) or document.get("schema") != CONTROL_SCHEMA:
            raise ValueError("Unsupported capture control document")
        request_id = document.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("Control request_id must be nonempty text")
        if request_id in self.seen:
            return None
        action = document.get("action")
        if action == "stop":
            self.stopped = True
        elif action in ("extend", "set_total"):
            seconds = document.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 1:
                raise ValueError("Control seconds must be finite and at least 1")
            deadline = self.deadline + seconds if action == "extend" else self.start + seconds
            if deadline > self.start + self.max_total_seconds:
                raise ValueError("Control request exceeds the recording's fixed max-total-seconds")
            self.deadline = deadline
        else:
            raise ValueError("Control action must be stop, extend, or set_total")
        self.seen.add(request_id)
        return action


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", help="Game name; may instead come from --profile")
    parser.add_argument("--window-title", help="Matching visible window title substring; defaults to game name")
    parser.add_argument("--seconds", required=True, type=bounded_number, help="Initial wall-clock recording duration (1-14400 seconds)")
    parser.add_argument("--max-total-seconds", type=bounded_number, default=3600,
                        help="Fixed total-duration cap for control-file extensions (default 3600)")
    parser.add_argument("--wait-for-game", type=float, default=0,
                        help="Wait up to this many seconds for the matching game to become foreground (0-3600)")
    parser.add_argument("--output-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--control-file", type=Path, help="Control JSON path; default SESSION/control.json")
    parser.add_argument("--profile", type=Path, help="Agent-discovered game-sensitivity/profile-v1 JSON")
    parser.add_argument("--dpi", type=int, default=None, help="Known DPI; otherwise preserve profile value or null")
    parser.add_argument("--look-sensitivity", type=float, default=None, help="Known game value; otherwise preserve profile value or null")
    parser.add_argument("--scoped-multiplier", type=float, default=None, help="Known multiplier; otherwise preserve profile value or null")
    parser.add_argument("--normal-zoom-multiplier", type=float, default=None, help="Known multiplier; otherwise preserve profile value or null")
    parser.add_argument("--monitor", type=int, help="Explicit zero-based monitor index; default detects matching game window")
    parser.add_argument("--fps", type=int, choices=(15, 30, 60), default=30)
    parser.add_argument("--max-width", type=int, choices=(1280, 1920, 2560, 3840), default=1920)
    parser.add_argument("--ffmpeg", help="FFmpeg executable; default searches PATH")
    parser.add_argument("--hardware", action="store_true", help="Use NVIDIA h264_nvenc instead of CPU libx264")
    parser.add_argument("--review-on-stop", action="store_true", help="Build the offline descriptive report after finalizing")
    return parser


def resolve_context(args):
    profile = load_profile(args.profile)
    game = args.game or profile.get("game")
    if not isinstance(game, str) or not game.strip():
        raise ValueError("Supply --game or a profile containing game")
    title = args.window_title if args.window_title is not None else profile.get("window_title", game)
    if (not isinstance(title, str)
            or not "".join(char for char in title if unicodedata.category(char) != "Cf").strip()):
        raise ValueError("A nonempty game window title is required")
    settings = {field: None for field in KNOWN_SETTINGS}
    settings.update(profile.get("settings", {}))
    sources = dict(profile.get("setting_sources", {}))
    for field in KNOWN_SETTINGS:
        explicit = getattr(args, field)
        if explicit is not None:
            settings[field] = explicit
            sources[field] = {"status": "provided", "source": "command-line",
                              "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        elif field not in sources:
            sources[field] = {"status": "unknown" if settings[field] is None else "provided",
                              "source": None if settings[field] is None else "profile"}
    validate_settings(settings)
    context = dict(profile.get("context", {}))
    if args.profile:
        context.update(profile_schema=profile.get("schema"), profile_evidence=profile.get("evidence", []))
    return dict(game=game.strip(), title=title.strip(), settings=settings,
                setting_sources=sources, context=context,
                settings_source=profile.get("settings_source", profile.get("source")))


def wait_for_game(gate, seconds, clock=time.monotonic, sleep=time.sleep):
    """Wait only within a fixed bound; never capture while waiting."""
    deadline = clock() + seconds
    while True:
        if gate.active_handle():
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError("The matching game did not become foreground within --wait-for-game")
        sleep(min(.1, remaining))


def run_capture(args):
    context = resolve_context(args)
    if args.seconds > args.max_total_seconds:
        raise ValueError("--seconds cannot exceed --max-total-seconds")
    if not math.isfinite(args.wait_for_game) or not 0 <= args.wait_for_game <= 3600:
        raise ValueError("--wait-for-game must be between 0 and 3600 seconds")
    if os.name != "nt":
        raise RuntimeError("Live gameplay capture requires Windows. Offline review and evidence export work on other platforms.")
    from aim_observer.capture import (ForegroundGate, enable_dpi_awareness, find_ffmpeg,
                                      find_game_monitor, monitors)
    from aim_observer.recorder import RecordingSession
    ffmpeg = find_ffmpeg(args.ffmpeg)
    enable_dpi_awareness()
    if args.wait_for_game:
        print(json.dumps({"status": "waiting_for_game", "game": context["game"],
                          "window_title": context["title"], "timeout_s": args.wait_for_game}), flush=True)
        wait_for_game(ForegroundGate(context["title"]), args.wait_for_game)
    displays = monitors()
    if not displays:
        raise RuntimeError("No display is available")
    selected = args.monitor if args.monitor is not None else find_game_monitor(displays, context["title"])
    if selected is None:
        raise RuntimeError("No visible matching game window was found. Open the game, use --wait-for-game, or supply --monitor.")
    if not 0 <= selected < len(displays):
        raise ValueError("--monitor is outside the available monitor indices")
    session = RecordingSession(args.output_root, displays[selected], fps=args.fps,
                               max_width=args.max_width, ffmpeg=ffmpeg,
                               hardware=args.hardware, **context)
    control = args.control_file or session.folder / "control.json"
    if control.exists():
        raise ValueError("Control file already exists; choose a fresh --control-file")
    control.parent.mkdir(parents=True, exist_ok=True) if args.control_file else None
    # The cap is fixed at launch. Extensions change the deadline in this same
    # process and session; they never restart capture or invent gap-filling frames.
    started = time.monotonic()
    deadline = CaptureDeadline(started, args.seconds, args.max_total_seconds)
    last_content, next_status, control_error = None, 0, None
    stop_reason = "deadline"
    try:
        folder = session.start()
        # Initialization belongs to the bounded run but may take some seconds.
        initial = {"schema": CONTROL_SCHEMA, "request_id": uuid.uuid4().hex,
                   "action": "set_total", "seconds": args.seconds}
        atomic_json(control, initial)
        session.metadata["runtime"] = {
            "initial_seconds": args.seconds, "max_total_seconds": args.max_total_seconds,
            "duration_clock": "monotonic elapsed time including initialization and focus pauses",
            "control_file": str(control.resolve()), "control_history": [],
        }
        print(json.dumps({"status": "recording", "session": str(folder.resolve()),
                          "control_file": str(control.resolve()), "max_total_seconds": args.max_total_seconds}), flush=True)
        while not deadline.stopped and time.monotonic() < deadline.deadline:
            now = time.monotonic()
            changed = False
            try:
                content = control.read_text(encoding="utf-8-sig")
                if content != last_content:
                    changed = True
                    last_content = content
                    document = json.loads(content)
                    action = deadline.apply(document)
                    control_error = None
                    if action:
                        session.metadata["runtime"]["control_history"].append(document)
                        print(json.dumps({"status": "control_applied", "request_id": document["request_id"],
                                          "action": action, "planned_total_seconds": deadline.deadline-deadline.start}), flush=True)
            except (OSError, ValueError, TypeError, OverflowError) as error:
                control_error = str(error)
                if changed or now >= next_status:
                    print(json.dumps({"status": "control_warning", "error": str(error)}), flush=True)
            info = session.status()
            if session.stop_event.is_set() or info["errors"]:
                stop_reason = "recording_error"
                break
            if now >= next_status:
                snapshot = dict(info, status="recording", session=str(folder.resolve()),
                                control_file=str(control.resolve()),
                                remaining_s=max(0, deadline.deadline-now),
                                planned_total_seconds=deadline.deadline-deadline.start,
                                max_total_seconds=deadline.max_total_seconds,
                                control_error=control_error)
                atomic_json(folder / "capture-status.json", snapshot)
                next_status = now + 1
            session.stop_event.wait(min(.1, max(0, deadline.deadline-time.monotonic())))
        if deadline.stopped:
            stop_reason = "control_stop"
    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"
    except Exception as error:
        stop_reason = "startup_or_runtime_error"
        session._fail("Capture runtime: " + str(error))
        raise
    finally:
        if session.folder.exists():
            session.metadata.setdefault("runtime", {})["stop_reason"] = stop_reason
            session.stop()
            atomic_json(session.folder / "capture-status.json", {
                "status": "stopped", "stop_reason": stop_reason,
                "session": str(session.folder.resolve()), "errors": list(session.errors),
                "files_finalized": session.metadata.get("files_finalized", False),
                "frames": session.frame_count,
            })
    if args.review_on_stop and session.metadata.get("files_finalized"):
        from aim_observer.review import generate_review
        generate_review(session.folder)
    print(json.dumps({"status": "stopped", "session": str(session.folder.resolve()),
                      "frames": session.frame_count, "input_events": session.input_count,
                      "errors": session.errors, "stop_reason": stop_reason}), flush=True)
    return 0 if session.frame_count and not session.errors and session.metadata.get("files_finalized") else 2


def send_control(argv):
    parser = argparse.ArgumentParser(description="Update one existing recording's bounded local control file")
    parser.add_argument("control_file", type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stop", action="store_true")
    action.add_argument("--extend-seconds", type=bounded_number)
    action.add_argument("--total-seconds", type=bounded_number)
    args = parser.parse_args(argv)
    if not args.control_file.is_file():
        raise ValueError("The recording control file does not exist")
    existing = json.loads(args.control_file.read_text(encoding="utf-8-sig"))
    if not isinstance(existing, dict) or existing.get("schema") != CONTROL_SCHEMA:
        raise ValueError("The target is not a gameplay recording control file")
    document = {"schema": CONTROL_SCHEMA, "request_id": uuid.uuid4().hex,
                "action": "stop" if args.stop else ("extend" if args.extend_seconds else "set_total")}
    if not args.stop:
        document["seconds"] = args.extend_seconds or args.total_seconds
    atomic_json(args.control_file, document)
    print(json.dumps({"status": "control_requested", "control_file": str(args.control_file.resolve()),
                      "request": document}), flush=True)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        return send_control(argv[1:]) if argv and argv[0] == "control" else run_capture(build_parser().parse_args(argv))
    except (OSError, ValueError, RuntimeError, OverflowError) as error:
        print("capture_gameplay: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
