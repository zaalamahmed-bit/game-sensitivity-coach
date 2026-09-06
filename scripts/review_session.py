"""Build an offline report from a recorded session, optionally attaching a reviewed assessment.

Python 3.8+ and the standard library are sufficient. This command works from
existing recordings and never imports screen-capture or live mouse-input modules.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from aim_observer.review import analyze_session, generate_review, validate_coach_assessment


def verify_manifest(session):
    """Verify the recorder's original sizes/hashes without rewriting its manifest."""
    session = Path(session)
    with (session / "manifest.json").open("r", encoding="utf-8-sig") as source:
        manifest = json.load(source)
    required = {"session.json", "mouse.csv", "frames.csv"}
    allowed = required | {"video.mp4"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise ValueError("manifest.json must include session.json, mouse.csv and frames.csv")
    if set(manifest) - allowed:
        raise ValueError("manifest.json contains unsupported paths; only original session files are allowed")
    if (session / "video.mp4").exists() and "video.mp4" not in manifest:
        raise ValueError("manifest.json omits the existing video.mp4")
    verified = []
    for name, expected in sorted(manifest.items()):
        if not isinstance(expected, dict):
            raise ValueError("manifest.json entry {} must be an object".format(name))
        size = expected.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("manifest.json entry {} must contain a nonnegative byte count".format(name))
        digest = expected.get("sha256")
        if name in required or digest is not None:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ValueError("manifest.json entry {} must contain a valid SHA-256 hash".format(name))
        path = session / name
        if path.stat().st_size != size:
            raise ValueError("Manifest size mismatch for {}".format(name))
        if digest is not None:
            actual = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    actual.update(block)
            if actual.hexdigest().lower() != digest.lower():
                raise ValueError("Manifest SHA-256 mismatch for {}".format(name))
        verified.append({"file": name, "bytes": size, "sha256_verified": digest is not None})
    return {"verified": True, "files": verified,
            "note": "Files without a recorded SHA-256 hash were checked by size only."}


def _write_assessment(path, assessment):
    serialized = json.dumps(assessment, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    # Atomic replacement targets the sidecar entry itself, never a symlink's
    # destination; a failed write leaves any earlier assessment intact.
    descriptor, temporary = tempfile.mkstemp(prefix=".assessment-", suffix=".json", dir=str(path.parent))
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(serialized)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def review_session(session, assessment_path=None, check_manifest=False):
    """Return report.html after all supplied evidence passes validation."""
    session = Path(session).expanduser().resolve()
    verification = verify_manifest(session) if check_manifest else None
    analysis = analyze_session(session)
    if verification is not None:
        analysis["manifest_verification"] = verification
    if assessment_path is not None:
        with Path(assessment_path).expanduser().open("r", encoding="utf-8-sig") as source:
            assessment = json.load(source)
        accepted = validate_coach_assessment(assessment, analysis)
        _write_assessment(session / "coach-assessment.json", accepted)
    return generate_review(session, analysis=analysis)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="Directory containing session.json, frames.csv and mouse.csv")
    parser.add_argument("--assessment", type=Path, help="Assistant-authored JSON to validate and save as coach-assessment.json")
    parser.add_argument("--verify-manifest", action="store_true", help="Verify original file sizes/hashes before creating report files")
    args = parser.parse_args(argv)
    try:
        report = review_session(args.session, assessment_path=args.assessment, check_manifest=args.verify_manifest)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        parser.exit(2, "error: {}\n".format(error))
    print(report)
    if args.verify_manifest:
        print("Manifest verified; files lacking a recorded SHA-256 hash were checked by size only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
