#!/usr/bin/env python3
"""Build a reproducible, allowlisted skill ZIP without private runtime outputs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import zipfile


NAME = "game-sensitivity-coach"
ROOT_FILES = {"SKILL.md", "README.md", ".gitignore", "LICENSE", "CHANGELOG.md",
              "CONTRIBUTING.md", "SECURITY.md"}
EXACT_FILES = {
    "agents/openai.yaml", "assets/gameplay-review.png",
    "examples/profile.example.json", "examples/conversion.synthetic.json",
    "scripts/aim_observer/review_template.html", ".github/workflows/tests.yml",
}
REQUIRED = {
    "SKILL.md", "README.md", "assets/gameplay-review.png", "agents/openai.yaml",
    "scripts/capture_gameplay.py", "scripts/review_session.py",
    "scripts/export_evidence.py", "scripts/settings_file.py",
    "scripts/convert_sensitivity.py", "scripts/discover_settings.py",
}


def allowed(relative):
    parts = relative.parts
    if any(part.startswith(".") for part in parts) and relative.as_posix() not in ROOT_FILES | EXACT_FILES:
        return False
    if "__pycache__" in parts:
        return False
    if relative.as_posix() in ROOT_FILES | EXACT_FILES:
        return True
    return ((parts[0] in ("scripts", "tests") and relative.suffix == ".py")
            or (parts[0] == "references" and relative.suffix == ".md"))


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def collect(root):
    root = Path(root).resolve()
    entries = {}
    # Enumerate only source trees. Do not even descend into sessions or profiles.
    candidates = [root / name for name in ROOT_FILES if (root / name).exists()]
    for directory in ("agents", "assets", "examples", "references", "scripts", "tests", ".github"):
        start = root / directory
        if not start.exists():
            continue
        pending = [start]
        while pending:
            current = pending.pop()
            if is_link(current):
                raise ValueError("Refusing linked source path: " + str(current.relative_to(root)))
            for child in sorted(current.iterdir()):
                if child.name == "__pycache__":
                    continue
                if is_link(child):
                    raise ValueError("Refusing linked source path: " + str(child.relative_to(root)))
                if child.is_dir():
                    pending.append(child)
                else:
                    candidates.append(child)
    for path in candidates:
        relative = path.relative_to(root)
        if not allowed(relative):
            continue
        if is_link(path) or not path.is_file():
            raise ValueError("Expected ordinary source file: " + relative.as_posix())
        entries[relative.as_posix()] = path.read_bytes()
    missing = REQUIRED - set(entries)
    if missing:
        raise ValueError("Missing required release files: " + ", ".join(sorted(missing)))
    skill = entries["SKILL.md"].decode("utf-8-sig")
    if not re.search(r"^name:\s*game-sensitivity-coach\s*$", skill, re.M):
        raise ValueError("SKILL.md must declare the packaged skill name")
    version = re.search(r'^\s+version:\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', skill, re.M)
    manifest = {
        "schema": "game-sensitivity/release-manifest-v1",
        "name": NAME,
        "version": version.group(1) if version else "unspecified",
        "files": [{"path": name, "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(entries.items())],
        "note": "This manifest lists every archive file except itself. No runtime sessions or real game configurations are included.",
    }
    entries["RELEASE-MANIFEST.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return entries


def build(root, output, replace=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root in output.parents:
        raise ValueError("Write the release outside the skill source folder")
    checksum = output.with_suffix(output.suffix + ".sha256")
    if not replace and (output.exists() or checksum.exists()):
        raise ValueError("Release already exists; choose a new output or use --replace")
    entries = collect(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(output), "w" if replace else "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(NAME + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    with checksum.open("w" if replace else "x", encoding="ascii", newline="\n") as stream:
        stream.write(digest + "  " + output.name + "\n")
    return {"zip": str(output), "sha256_file": str(checksum), "sha256": digest,
            "archive_files": len(entries), "bytes": output.stat().st_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "dist" / (NAME + ".zip"))
    parser.add_argument("--replace", action="store_true", help="Explicitly replace a previously built ZIP and checksum")
    args = parser.parse_args(argv)
    try:
        result = build(Path(__file__).resolve().parents[1], args.output, args.replace)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
