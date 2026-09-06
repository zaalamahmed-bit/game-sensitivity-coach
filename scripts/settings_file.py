#!/usr/bin/env python3
"""Inspect and patch explicit text settings without rewriting unrelated bytes.

Python 3.8+, standard library only. This tool does not discover game schemas,
authorize changes, detect running games, or modify process memory. The caller
must review the plan, obtain authorization, and close the game before applying.
JSON, generic binary saves, and unmarked legacy encodings are unsupported.
The explicit finals-gvas format patches only a verified, same-length layout.
"""

import argparse
import codecs
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile
import time
import uuid


PLAN_SCHEMA = "settings-file/plan-v1"
RECEIPT_SCHEMA = "settings-file/receipt-v1"
MAX_FILE_BYTES = 16 * 1024 * 1024
FORMATS = ("ini", "frostbite", "finals-gvas")
FINALS_KEYS = ("GameplayOption.Controls.MouseSensitivity",
               "GameplayOption.Controls.MouseZoomSensitivity",
               "GameplayOption.Controls.MouseScopedZoomSensitivity")
GVAS_ENCODING = "ascii-gvas-verified-layout"
REPLACE_RETRY_DELAYS = (0.05, 0.10, 0.20, 0.40, 0.80)


class SettingsError(ValueError):
    """A file or requested operation did not pass validation."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _path(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise SettingsError("Symlink files are unsupported; select the real file explicitly.")
    path = path.resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise SettingsError("Selected path must be a regular file.")
    return path


def _read(path):
    path = _path(path)
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SettingsError("File exceeds the 16 MiB text-config limit; unsupported save format.")
    return path, data


def _decode(data):
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise SettingsError("UTF-32 is unsupported; use a supported text config.")
    if data.startswith(codecs.BOM_UTF8):
        bom, encoding = codecs.BOM_UTF8, "utf-8"
    elif data.startswith(codecs.BOM_UTF16_LE):
        bom, encoding = codecs.BOM_UTF16_LE, "utf-16-le"
    elif data.startswith(codecs.BOM_UTF16_BE):
        bom, encoding = codecs.BOM_UTF16_BE, "utf-16-be"
    else:
        bom, encoding = b"", "utf-8"
    try:
        text = data[len(bom):].decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise SettingsError("Unsupported or invalid text encoding; expected UTF-8 or BOM-marked UTF-16.") from exc
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        raise SettingsError("Binary/control characters detected; binary saves are unsupported.")
    if any(0x7f <= ord(char) <= 0x9f or char in "\u2028\u2029" for char in text):
        raise SettingsError("Unsupported control or line-separator characters.")
    if text.lstrip().startswith(("{", "[\"")):
        raise SettingsError("JSON files are unsupported; use an explicit text-config format.")
    return text, bom, encoding


def _selector(selector):
    if not isinstance(selector, dict) or set(selector) - {"key", "section"}:
        raise SettingsError("Each selector must contain key and optional section only.")
    key, section = selector.get("key"), selector.get("section")
    if not isinstance(key, str) or not key or key.strip() != key or any(c in key for c in "\r\n\x00= \t"):
        raise SettingsError("Selector key must be an explicit nonempty key without whitespace or '='.")
    if section is not None and (not isinstance(section, str) or not section or section.strip() != section or any(c in section for c in "\r\n[]\x00")):
        raise SettingsError("Selector section must be a nonempty section name without brackets.")
    return {"key": key, "section": section}


def _value(value, name):
    if not isinstance(value, str) or value.strip(" \t") != value:
        raise SettingsError("%s must be an exact string without surrounding whitespace." % name)
    if any(ord(c) < 32 or c in "\x7f\u2028\u2029" for c in value):
        raise SettingsError("%s must be a single-line value without control characters." % name)
    return value


def _value_end(line, start):
    """Recognize whitespace-delimited inline comments outside quoted strings."""
    quote = None
    escaped = False
    end = len(line)
    for index in range(start, len(line)):
        char = line[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in ";#" and (index == start or line[index - 1] in " \t"):
            end = index
            break
    if quote is not None:
        raise SettingsError("Unclosed quoted value; unsupported text config syntax.")
    while end > start and line[end - 1] in " \t":
        end -= 1
    return end


def _positive_number(value):
    if not re.fullmatch(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        raise SettingsError("finals-gvas values must be positive ASCII numeric strings.")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise SettingsError("finals-gvas values must be finite and strictly positive.")


def _gvas_entries(data):
    """Recognize only the three reviewed FString key/value byte layouts.

    This is deliberately not a GVAS decoder. No offsets are guessed, length
    fields rewritten, or other binary content interpreted. The workflow must
    verify this layout for the current game build and read back values in-game.
    """
    if not data.startswith(b"GVAS"):
        raise SettingsError("finals-gvas requires the GVAS magic header and the verified FString layout.")
    entries = []
    for key in FINALS_KEYS:
        key_bytes = key.encode("ascii")
        occurrences = data.count(key_bytes)
        if not occurrences:
            continue
        if occurrences != 1:
            raise SettingsError("Ambiguous duplicate finals-gvas key %r." % key)
        start = data.index(key_bytes)
        if start < 4 or data[start - 4:start] != struct.pack("<i", len(key_bytes) + 1):
            raise SettingsError("Unsupported finals-gvas key FString length for %r." % key)
        after_key = start + len(key_bytes)
        if data[after_key:after_key + 1] != b"\x00" or after_key + 5 > len(data):
            raise SettingsError("Unsupported finals-gvas key termination/layout for %r." % key)
        value_length = struct.unpack("<i", data[after_key + 1:after_key + 5])[0]
        value_start = after_key + 5
        value_end = value_start + value_length - 1
        if value_length < 2 or value_length > 128 or value_end >= len(data) or data[value_end:value_end + 1] != b"\x00":
            raise SettingsError("Unsupported finals-gvas value FString length/termination for %r." % key)
        try:
            value = data[value_start:value_end].decode("ascii")
        except UnicodeError as exc:
            raise SettingsError("Unsupported non-ASCII finals-gvas value for %r." % key) from exc
        _positive_number(value)
        entries.append({"key": key, "section": None, "value": value, "line": None,
                        "byte_start": value_start, "byte_end": value_end})
    return entries, GVAS_ENCODING, False


def _entries(data, file_format):
    if file_format not in FORMATS:
        raise SettingsError("Unsupported format; choose ini, frostbite, or verified finals-gvas. JSON is unsupported.")
    if file_format == "finals-gvas":
        return _gvas_entries(data)
    text, bom, encoding = _decode(data)
    entries = []
    offset = len(bom)
    section = None
    # splitlines with keepends preserves CRLF, LF, CR and the missing final newline.
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        line = raw.rstrip("\r\n")
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith((";", "#")):
            offset += len(raw.encode(encoding))
            continue
        match = re.fullmatch(r"\s*\[([^\[\]]+)\]\s*(?:[;#].*)?", line)
        if file_format == "ini" and match:
            section = match.group(1).strip()
            offset += len(raw.encode(encoding))
            continue
        pattern = r"[ \t]*([^=\s]+)[ \t]*=[ \t]*" if file_format == "ini" else r"[ \t]*([^\s=]+)[ \t]+"
        match = re.match(pattern, line)
        if not match:
            raise SettingsError("Unsupported %s syntax at line %d; refusing to patch this file." % (file_format, number))
        start = match.end()
        end = _value_end(line, start)
        entries.append({"key": match.group(1), "section": section, "value": line[start:end],
                        "line": number, "byte_start": offset + len(line[:start].encode(encoding)),
                        "byte_end": offset + len(line[:end].encode(encoding))})
        offset += len(raw.encode(encoding))
    return entries, encoding, bool(bom)


def _match(entries, selector, file_format):
    selector = _selector(selector)
    if file_format in ("frostbite", "finals-gvas") and selector["section"] is not None:
        raise SettingsError("%s selectors cannot have sections." % file_format)
    if file_format == "finals-gvas" and selector["key"] not in FINALS_KEYS:
        raise SettingsError("finals-gvas supports only the three explicitly whitelisted sensitivity keys.")
    matches = [entry for entry in entries if entry["key"] == selector["key"] and
               (selector["section"] is None or entry["section"] == selector["section"])]
    if not matches:
        raise SettingsError("Missing key %r in section %r." % (selector["key"], selector["section"]))
    if len(matches) != 1:
        raise SettingsError("Ambiguous duplicate key %r (%d matches); choose an explicit section or stop." % (selector["key"], len(matches)))
    return matches[0]


def inspect_file(path, selectors, file_format="ini"):
    """Return exact lexical values and SHA256; section/key matching is case-sensitive."""
    path, data = _read(path)
    if path.suffix.lower() == ".json":
        raise SettingsError("JSON files are unsupported.")
    if not selectors:
        raise SettingsError("Select at least one explicit key.")
    entries, encoding, bom = _entries(data, file_format)
    selected = [dict(_match(entries, selector, file_format)) for selector in selectors]
    return {"schema": "settings-file/inspection-v1", "source_path": str(path),
            "source_sha256": _hash(data), "source_size": len(data), "format": file_format,
            "encoding": encoding, "bom": bom, "values": selected}


def _patch(data, changes, file_format):
    if not isinstance(changes, list) or not changes:
        raise SettingsError("Plan must contain a nonempty changes list.")
    entries, encoding, bom = _entries(data, file_format)
    replacements, described, seen = [], [], set()
    for change in changes:
        if not isinstance(change, dict):
            raise SettingsError("Each change must be an object.")
        selector = _selector(change.get("selector"))
        old = _value(change.get("expected_old"), "expected_old")
        new = _value(change.get("new_value"), "new_value")
        entry = _match(entries, selector, file_format)
        if entry["byte_start"] in seen:
            raise SettingsError("The same setting was selected more than once.")
        seen.add(entry["byte_start"])
        if entry["value"] != old:
            raise SettingsError("Stale value for %r: expected %r, found %r." % (selector["key"], old, entry["value"]))
        if old == new:
            raise SettingsError("No-op change for %r; old and new values are identical." % selector["key"])
        if file_format == "finals-gvas":
            _positive_number(new)
            replacement = new.encode("ascii")
            if len(replacement) != entry["byte_end"] - entry["byte_start"]:
                raise SettingsError("finals-gvas replacements must have exactly the same encoded byte length.")
        else:
            replacement = new.encode(encoding)
        replacements.append((entry["byte_start"], entry["byte_end"], replacement))
        described.append({"selector": selector, "expected_old": old, "new_value": new,
                          "line": entry["line"], "matched_section": entry["section"]})
    result = data
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    # New syntax must still resolve to exactly the requested lexical value.
    new_entries, _, _ = _entries(result, file_format)
    for change in described:
        if _match(new_entries, change["selector"], file_format)["value"] != change["new_value"]:
            raise SettingsError("Replacement changes comment/quoting syntax; use a simple exact value.")
    return result, described, encoding, bom


def create_plan(path, changes, file_format="ini"):
    """Return a dry-run plan; this function never writes to the source file."""
    path, data = _read(path)
    if path.suffix.lower() == ".json":
        raise SettingsError("JSON files are unsupported.")
    result, described, encoding, bom = _patch(data, changes, file_format)
    return {"schema": PLAN_SCHEMA, "created_at_utc": _now(), "source_path": str(path),
            "source_sha256": _hash(data), "source_size": len(data), "format": file_format,
            "encoding": encoding, "bom": bom, "changes": described,
            "result_sha256": _hash(result), "dry_run": True,
            "workflow": "Review this exact plan, obtain authorization, and close the game before apply."}


def _json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _write_new(path, data):
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _unchanged(path, expected):
    if _read(path)[1] != expected:
        raise SettingsError("File changed before atomic replacement: %s" % path)


def _atomic_write(path, data, mode=None, check=None):
    """Replace via a temporary sibling, never truncate the destination in place."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, stat.S_IMODE(mode))
        for attempt in range(len(REPLACE_RETRY_DELAYS) + 1):
            try:
                if check is not None:
                    check()
                os.replace(temporary, str(path))
                break
            except PermissionError:
                if attempt == len(REPLACE_RETRY_DELAYS):
                    raise
                time.sleep(REPLACE_RETRY_DELAYS[attempt])
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_json(path, schema):
    path, data = _read(path)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SettingsError("Duplicate JSON field %r in artifact." % key)
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError("Artifact must be valid UTF-8 JSON.") from exc
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise SettingsError("Expected artifact schema %s." % schema)
    return path, data, value


def _sha(value, label):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise SettingsError("%s must be a lowercase SHA256 digest." % label)
    return value


def _source(artifact):
    value = artifact.get("source_path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise SettingsError("Artifact source_path must be an absolute path.")
    return _read(value)


def _pending(operation, source, expected, receipt_path, backup, error, audit_path=None):
    """Truthful partial result after a verified mutation and failed audit update."""
    try:
        current_hash = _hash(_read(source)[1])
        source_state = ("original" if operation == "rollback" else "applied") if current_hash == _hash(expected) else "modified"
        read_error = None
    except (OSError, SettingsError) as exc:
        current_hash, source_state, read_error = None, "unreadable", str(exc)
    try:
        receipt_state = _load_json(receipt_path, RECEIPT_SCHEMA)[2].get("state")
    except (OSError, SettingsError):
        receipt_state = "unreadable"
    expected_state = "original" if operation == "rollback" else "applied"
    state = ("rolled_back_audit_pending" if operation == "rollback" else "applied_receipt_pending")
    if source_state != expected_state:
        state = "source_%s_audit_pending" % source_state
    recovery = {"status": ["status", str(receipt_path)]}
    if operation != "rollback":
        recovery.update({"finalize": ["finalize", str(receipt_path), "--game-closed"],
                         "rollback": ["rollback", str(receipt_path), "--game-closed"]})
    return {"schema": "settings-file/operation-result-v1", "operation": operation,
            "state": state, "partial_success": True, "operation_completed": False,
            "config_mutation_verified_before_audit": True,
            "source_path": str(source), "source_state": source_state,
            "current_sha256": current_hash, "expected_sha256": _hash(expected),
            "receipt_path": str(receipt_path), "receipt_state": receipt_state, "backup_path": str(backup),
            "audit_path": str(audit_path) if audit_path else None,
            "error": {"type": type(error).__name__, "message": str(error)},
            "readback_error": read_error, "recovery_arguments": recovery,
            "guidance": "Do not repeat apply or rollback. Inspect status first. Preserve the backup and all audit files; finalize changes only the receipt when the source still matches the applied hash."}


def _receipt_material(receipt_path):
    receipt_path, receipt_bytes, receipt = _load_json(receipt_path, RECEIPT_SCHEMA)
    if receipt.get("state") not in ("prepared", "applied", "aborted_source_changed"):
        raise SettingsError("Unknown receipt state.")
    source, current = _source(receipt)
    backup_name = receipt.get("backup_path")
    if not isinstance(backup_name, str) or not Path(backup_name).is_absolute():
        raise SettingsError("Receipt backup_path must be an absolute path.")
    backup, original = _read(backup_name)
    if source == backup or source == receipt_path or backup.parent != source.parent:
        raise SettingsError("Receipt must reference a separate sibling backup.")
    if _hash(original) != _sha(receipt.get("original_sha256"), "original_sha256"):
        raise SettingsError("Backup hash does not match the original; refusing recovery.")
    reproduced, changes, encoding, bom = _patch(original, receipt.get("changes"), receipt.get("format"))
    if (_hash(reproduced) != _sha(receipt.get("applied_sha256"), "applied_sha256") or
            changes != receipt.get("changes") or encoding != receipt.get("encoding") or bom is not receipt.get("bom")):
        raise SettingsError("Receipt changes do not reproduce the recorded applied hash.")
    return receipt_path, receipt_bytes, receipt, source, current, backup, original, reproduced


def status(receipt_path):
    """Read-only recovery status verified against the backup and patch recipe."""
    receipt_path, _, receipt, source, current, backup, original, applied = _receipt_material(receipt_path)
    source_state = "applied" if current == applied else "original" if current == original else "modified"
    eligible = receipt["state"] in ("prepared", "applied")
    return {"schema": "settings-file/status-v1", "receipt_path": str(receipt_path),
            "receipt_state": receipt["state"], "source_path": str(source), "source_state": source_state,
            "current_sha256": _hash(current), "original_sha256": _hash(original),
            "applied_sha256": _hash(applied), "backup_path": str(backup), "backup_verified": True,
            "rollback_ready": eligible and source_state == "applied",
            "finalize_ready": receipt["state"] == "prepared" and source_state == "applied"}


def finalize(receipt_path, game_closed=False):
    """Finalize a prepared apply receipt without ever writing the game config."""
    if not game_closed:
        raise SettingsError("Finalize requires --game-closed; close the game first.")
    receipt_path, receipt_bytes, receipt, source, current, backup, original, applied = _receipt_material(receipt_path)
    if receipt["state"] not in ("prepared", "applied") or current != applied:
        raise SettingsError("Finalize requires the current file to match an applied or prepared receipt's applied hash.")
    if receipt["state"] == "applied":
        result = status(receipt_path)
        result.update({"state": "already_finalized", "config_rewritten": False})
        return result
    snapshot = receipt_path.with_name(receipt_path.name + ".pre-finalize-" + uuid.uuid4().hex + ".json")
    _write_new(snapshot, receipt_bytes)
    receipt["state"] = "applied"
    receipt["finalized_at_utc"] = _now()
    receipt["finalization_note"] = "Recovered prepared receipt after verifying the applied file; original application completion time is unknown."
    receipt["previous_receipt_path"] = str(snapshot)
    receipt["previous_receipt_sha256"] = _hash(receipt_bytes)

    def check():
        _unchanged(source, applied)
        _unchanged(backup, original)
        _unchanged(receipt_path, receipt_bytes)

    try:
        _atomic_write(receipt_path, _json_bytes(receipt), check=check)
    except (OSError, SettingsError) as exc:
        result = _pending("finalize", source, applied, receipt_path, backup, exc)
        result.update({"config_rewritten": False, "previous_receipt_path": str(snapshot)})
        return result
    result = status(receipt_path)
    result.update({"state": "finalized", "config_rewritten": False, "previous_receipt_path": str(snapshot)})
    return result


def apply_plan(plan_path, reviewed=False, game_closed=False):
    """Apply a reviewed plan. Flags are workflow acknowledgments, not detection."""
    if not reviewed or not game_closed:
        raise SettingsError("Apply requires --reviewed and --game-closed; obtain authorization before using them.")
    plan_path, plan_bytes, plan = _load_json(plan_path, PLAN_SCHEMA)
    source, original = _source(plan)
    if source == plan_path:
        raise SettingsError("Plan and source must be different files.")
    if source.suffix.lower() == ".json":
        raise SettingsError("JSON files are unsupported.")
    if _hash(original) != _sha(plan.get("source_sha256"), "source_sha256"):
        raise SettingsError("Stale source hash; inspect and create a fresh plan before applying.")
    result, changes, encoding, bom = _patch(original, plan.get("changes"), plan.get("format"))
    if (plan.get("source_size") != len(original) or plan.get("encoding") != encoding or
            plan.get("bom") is not bom or plan.get("changes") != changes or
            _hash(result) != _sha(plan.get("result_sha256"), "result_sha256")):
        raise SettingsError("Plan contents do not match the current source and exact proposed changes; regenerate and review.")
    token = uuid.uuid4().hex
    backup = source.with_name(source.name + ".sensitivity-" + token + ".backup")
    receipt_path = source.with_name(source.name + ".sensitivity-" + token + ".receipt.json")
    source_mode = source.stat().st_mode
    receipt = {"schema": RECEIPT_SCHEMA, "state": "prepared", "created_at_utc": _now(),
               "source_path": str(source), "original_sha256": _hash(original),
               "applied_sha256": _hash(result), "backup_path": str(backup),
               "plan_path": str(plan_path), "plan_sha256": _hash(plan_bytes),
               "format": plan["format"], "encoding": encoding, "bom": bom,
               "changes": changes, "source_mode": stat.S_IMODE(source_mode),
               "reviewed_acknowledged": True, "game_closed_acknowledged": True}
    _write_new(backup, original)
    prepared_bytes = _json_bytes(receipt)
    _write_new(receipt_path, prepared_bytes)
    # Recheck after writing backup/audit, immediately before replacing the source.
    if _read(source)[1] != original:
        receipt["state"] = "aborted_source_changed"
        _atomic_write(receipt_path, _json_bytes(receipt))
        raise SettingsError("Source changed during preparation; aborted. Backup and audit receipt were retained.")
    _atomic_write(source, result, source_mode, check=lambda: _unchanged(source, original))
    if _read(source)[1] != result:
        raise SettingsError("Source changed after replacement; prepared receipt and backup retained. Do not retry blindly.")
    receipt["state"] = "applied"
    receipt["applied_at_utc"] = _now()
    def check_receipt():
        _unchanged(source, result)
        _unchanged(receipt_path, prepared_bytes)

    try:
        _atomic_write(receipt_path, _json_bytes(receipt), check=check_receipt)
    except (OSError, SettingsError) as exc:
        return _pending("apply", source, result, receipt_path, backup, exc)
    return {"receipt_path": str(receipt_path), "backup_path": str(backup),
            "source_path": str(source), "applied_sha256": receipt["applied_sha256"], "state": "applied"}


def rollback(receipt_path, game_closed=False):
    """Restore original bytes only if both current file and backup match receipt."""
    if not game_closed:
        raise SettingsError("Rollback requires --game-closed; close the game first.")
    receipt_path, receipt_bytes, receipt, source, current, backup, original, reproduced = _receipt_material(receipt_path)
    if receipt.get("state") not in ("applied", "prepared"):
        raise SettingsError("Receipt is not an applied or recoverable prepared operation.")
    if _hash(current) != _sha(receipt.get("applied_sha256"), "applied_sha256"):
        raise SettingsError("Current file differs from the applied hash; rollback would overwrite later changes.")
    audit_path = receipt_path.with_name(receipt_path.name + ".rollback-" + uuid.uuid4().hex + ".json")
    audit = {"schema": "settings-file/rollback-v1", "state": "prepared", "created_at_utc": _now(),
             "source_path": str(source), "receipt_path": str(receipt_path), "receipt_sha256": _hash(receipt_bytes),
             "before_sha256": _hash(current), "restored_sha256": _hash(original),
             "backup_path": str(backup), "game_closed_acknowledged": True}
    _write_new(audit_path, _json_bytes(audit))
    if _read(source)[1] != current or _read(backup)[1] != original:
        audit["state"] = "aborted_files_changed"
        _atomic_write(audit_path, _json_bytes(audit))
        raise SettingsError("Source or backup changed during preparation; rollback aborted.")
    def check_rollback():
        _unchanged(source, current)
        _unchanged(backup, original)

    _atomic_write(source, original, source.stat().st_mode, check=check_rollback)
    if _read(source)[1] != original:
        raise SettingsError("Source changed after rollback; audit and backup retained.")
    audit["state"] = "rolled_back"
    audit["rolled_back_at_utc"] = _now()
    try:
        _atomic_write(audit_path, _json_bytes(audit), check=lambda: _unchanged(source, original))
    except (OSError, SettingsError) as exc:
        return _pending("rollback", source, original, receipt_path, backup, exc, audit_path)
    # Keep the original apply receipt and backup intact; rollback has its own audit.
    return {"source_path": str(source), "state": "rolled_back", "audit_path": str(audit_path),
            "backup_path": str(backup), "restored_sha256": _hash(original)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read exact selected values and file hash")
    plan = commands.add_parser("plan", help="Dry run: write a reviewable plan without changing the config")
    for command in (inspect, plan):
        command.add_argument("file")
        command.add_argument("--format", choices=FORMATS, default="ini")
        command.add_argument("--section", help="Exact INI section; omission searches all sections")
    inspect.add_argument("--key", action="append", required=True, help="Exact key, repeatable")
    plan.add_argument("--change", nargs=3, action="append", required=True, metavar=("KEY", "EXPECTED_OLD", "NEW"))
    plan.add_argument("--output", required=True, help="New plan JSON file; existing files are not overwritten")
    apply = commands.add_parser("apply", help="Apply an authorized, reviewed plan while the game is closed")
    apply.add_argument("plan")
    apply.add_argument("--reviewed", action="store_true", help="Acknowledge this exact plan was reviewed and authorized")
    apply.add_argument("--game-closed", action="store_true", help="Acknowledge the game is closed (not detected automatically)")
    restore = commands.add_parser("rollback", help="Restore from a receipt if there are no later file changes")
    restore.add_argument("receipt")
    restore.add_argument("--game-closed", action="store_true")
    inspect_status = commands.add_parser("status", help="Read verified source/backup state without modifying files")
    inspect_status.add_argument("receipt")
    recover = commands.add_parser("finalize", help="Recover a prepared apply receipt without rewriting the config")
    recover.add_argument("receipt")
    recover.add_argument("--game-closed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_file(args.file, [{"key": key, "section": args.section} for key in args.key], args.format)
        elif args.command == "plan":
            changes = [{"selector": {"key": key, "section": args.section}, "expected_old": old, "new_value": new}
                       for key, old, new in args.change]
            result = create_plan(args.file, changes, args.format)
            output = Path(args.output).expanduser().absolute()
            _write_new(output, _json_bytes(result))
            result = {"plan_path": str(output), "dry_run": True, "plan": result}
        elif args.command == "apply":
            result = apply_plan(args.plan, reviewed=args.reviewed, game_closed=args.game_closed)
        elif args.command == "status":
            result = status(args.receipt)
        elif args.command == "finalize":
            result = finalize(args.receipt, game_closed=args.game_closed)
        else:
            result = rollback(args.receipt, game_closed=args.game_closed)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 3 if result.get("partial_success") else 0
    except (SettingsError, OSError, UnicodeError) as exc:
        print("settings_file: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
