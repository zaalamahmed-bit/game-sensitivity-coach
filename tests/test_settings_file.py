"""Synthetic config fixtures only. Never inspect or change installed game files."""

import codecs
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "settings_file.py"
SPEC = importlib.util.spec_from_file_location("settings_file_under_test", str(SCRIPT))
settings = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(settings)


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def fixture(self, data, name="Input.ini"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def change(self, old="0.72", new="0.70", key="Scoped", section="Controls"):
        return {"selector": {"key": key, "section": section}, "expected_old": old, "new_value": new}

    def plan(self, path, changes=None, file_format="ini"):
        value = settings.create_plan(path, changes or [self.change()], file_format)
        output = self.root / ("plan-" + str(len(list(self.root.glob("plan-*.json")))) + ".json")
        output.write_text(json.dumps(value), encoding="utf-8")
        return output, value

    def apply(self, path):
        return settings.apply_plan(path, reviewed=True, game_closed=True)

    def test_utf8_bom_crlf_apply_and_rollback_preserve_exact_bytes(self):
        original = codecs.BOM_UTF8 + b"; retained\r\n[Controls]\r\nScoped \t= 0.72  ; comment\r\nOther=9\r\n"
        source = self.fixture(original)
        inspection = settings.inspect_file(source, [{"key": "Scoped", "section": "Controls"}])
        self.assertEqual(inspection["values"][0]["value"], "0.72")
        self.assertEqual(inspection["values"][0]["line"], 3)
        self.assertTrue(inspection["bom"])
        plan_path, plan = self.plan(source)
        self.assertEqual(source.read_bytes(), original, "Planning must not alter the source")
        self.assertTrue(plan["dry_run"])
        result = self.apply(plan_path)
        self.assertEqual(source.read_bytes(), original.replace(b"0.72", b"0.70"))
        backup = Path(result["backup_path"])
        self.assertEqual(backup.read_bytes(), original)
        receipt = Path(result["receipt_path"])
        before_receipt = receipt.read_bytes()
        restored = settings.rollback(receipt, game_closed=True)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(receipt.read_bytes(), before_receipt, "Rollback keeps the apply audit intact")
        self.assertEqual(json.loads(Path(restored["audit_path"]).read_text())["state"], "rolled_back")

    def test_utf16_endianness_and_mixed_newlines(self):
        for bom, encoding in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
            with self.subTest(encoding=encoding):
                original = bom + "[Controls]\r\nName=naïve 日本\nScoped=0.72\rOther=1".encode(encoding)
                source = self.fixture(original)
                plan_path, _ = self.plan(source)
                self.apply(plan_path)
                self.assertEqual(source.read_bytes(), original.replace("0.72".encode(encoding), "0.70".encode(encoding)))

    def test_frostbite_spacing_comments_and_no_final_newline(self):
        original = b"GstInput.MouseSensitivity\t  0.123\r\nGstInput.MouseZoomSensitivity 1.0  # keep\nGstRender.FullscreenEnabled 1"
        source = self.fixture(original, "PROFSAVE_profile")
        change = self.change("0.123", "0.09999", "GstInput.MouseSensitivity", None)
        plan_path, _ = self.plan(source, [change], "frostbite")
        self.apply(plan_path)
        self.assertEqual(source.read_bytes(), original.replace(b"0.123", b"0.09999"))

    def test_quoted_comment_characters_are_values(self):
        original = b'[Controls]\nScoped=0.72\nName="alpha ; beta # gamma" ; keep\n'
        source = self.fixture(original)
        item = settings.inspect_file(source, [{"key": "Name"}])["values"][0]
        self.assertEqual(item["value"], '"alpha ; beta # gamma"')

    def test_section_omission_requires_global_unique_key(self):
        source = self.fixture(b"[First]\nScoped=0.72\n[Second]\nScoped=0.90\n")
        with self.assertRaisesRegex(settings.SettingsError, "Ambiguous duplicate"):
            settings.inspect_file(source, [{"key": "Scoped"}])
        result = settings.inspect_file(source, [{"key": "Scoped", "section": "Second"}])
        self.assertEqual(result["values"][0]["value"], "0.90")

    def test_duplicate_in_same_section_rejected(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\nScoped=0.72\n")
        with self.assertRaisesRegex(settings.SettingsError, "Ambiguous duplicate"):
            self.plan(source)

    def test_missing_case_sensitive_key_rejected(self):
        source = self.fixture(b"[Controls]\nscoped=0.72\n")
        with self.assertRaisesRegex(settings.SettingsError, "Missing key"):
            self.plan(source)

    def test_expected_old_is_exact_lexical_value(self):
        source = self.fixture(b"[Controls]\nScoped=0.720\n")
        with self.assertRaisesRegex(settings.SettingsError, "Stale value"):
            self.plan(source)

    def test_stale_file_hash_prevents_apply_and_creates_no_backup(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\nOther=1\n")
        plan_path, _ = self.plan(source)
        changed = source.read_bytes().replace(b"Other=1", b"Other=2")
        source.write_bytes(changed)
        with self.assertRaisesRegex(settings.SettingsError, "Stale source hash"):
            self.apply(plan_path)
        self.assertEqual(source.read_bytes(), changed)
        self.assertFalse(list(self.root.glob("*.backup")))

    def test_source_changes_during_preparation_abort_without_overwriting(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        newer = b"[Controls]\nScoped=0.80\n"
        original_write_new = settings._write_new

        def competing_write(path, data):
            original_write_new(path, data)
            if str(path).endswith(".receipt.json"):
                source.write_bytes(newer)

        with mock.patch.object(settings, "_write_new", side_effect=competing_write):
            with self.assertRaisesRegex(settings.SettingsError, "changed during preparation"):
                self.apply(plan_path)
        self.assertEqual(source.read_bytes(), newer)
        receipt = next(self.root.glob("*.receipt.json"))
        self.assertEqual(json.loads(receipt.read_text())["state"], "aborted_source_changed")
        self.assertEqual(next(self.root.glob("*.backup")).read_bytes(), b"[Controls]\nScoped=0.72\n")

    def test_failed_atomic_replace_keeps_original_backup_and_prepared_receipt(self):
        original = b"[Controls]\nScoped=0.72\n"
        source = self.fixture(original)
        plan_path, _ = self.plan(source)
        with mock.patch.object(settings.os, "replace", side_effect=PermissionError("synthetic locked file")), mock.patch.object(settings.time, "sleep"):
            with self.assertRaises(PermissionError):
                self.apply(plan_path)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(next(self.root.glob("*.backup")).read_bytes(), original)
        receipt = next(self.root.glob("*.receipt.json"))
        self.assertEqual(json.loads(receipt.read_text())["state"], "prepared")
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_prepared_receipt_recovers_apply_when_final_receipt_write_failed(self):
        original = b"[Controls]\nScoped=0.72\n"
        source = self.fixture(original)
        plan_path, _ = self.plan(source)
        original_atomic_write = settings._atomic_write

        def receipt_failure(path, data, mode=None, check=None):
            if str(path).endswith(".receipt.json"):
                raise OSError("synthetic audit finalization failure")
            original_atomic_write(path, data, mode, check=check)

        with mock.patch.object(settings, "_atomic_write", side_effect=receipt_failure):
            partial = self.apply(plan_path)
        self.assertEqual(partial["state"], "applied_receipt_pending")
        self.assertTrue(partial["partial_success"])
        self.assertEqual(source.read_bytes(), original.replace(b"0.72", b"0.70"))
        receipt = next(self.root.glob("*.receipt.json"))
        self.assertEqual(json.loads(receipt.read_text())["state"], "prepared")
        settings.rollback(receipt, game_closed=True)
        self.assertEqual(source.read_bytes(), original)

    def receipt_replace_failure(self, limit=None):
        original_replace = settings.os.replace
        attempts = []

        def replace(source, destination):
            if str(destination).endswith(".receipt.json"):
                attempts.append(str(destination))
                if limit is None or len(attempts) <= limit:
                    raise PermissionError("synthetic receipt lock")
            return original_replace(source, destination)

        return replace, attempts

    def test_transient_receipt_lock_retries_and_finishes_once(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        replace, attempts = self.receipt_replace_failure(limit=2)
        with mock.patch.object(settings.os, "replace", side_effect=replace), mock.patch.object(settings.time, "sleep") as sleep:
            result = self.apply(plan_path)
        self.assertEqual(result["state"], "applied")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(list(self.root.glob("*.backup"))), 1)
        self.assertEqual(settings.status(result["receipt_path"])["source_state"], "applied")

    def test_persistent_receipt_lock_outputs_partial_json_and_exit_three(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        replace, attempts = self.receipt_replace_failure()
        stdout = io.StringIO()
        with mock.patch.object(settings.os, "replace", side_effect=replace), mock.patch.object(settings.time, "sleep"), contextlib.redirect_stdout(stdout):
            code = settings.main(["apply", str(plan_path), "--reviewed", "--game-closed"])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result["state"], "applied_receipt_pending")
        self.assertEqual(result["source_state"], "applied")
        self.assertEqual(len(attempts), 6)
        self.assertEqual(result["recovery_arguments"]["finalize"][0], "finalize")
        self.assertEqual(source.read_bytes(), b"[Controls]\nScoped=0.70\n")
        state = settings.status(result["receipt_path"])
        self.assertEqual(state["receipt_state"], "prepared")
        self.assertTrue(state["finalize_ready"])
        self.assertTrue(state["rollback_ready"])

    def test_finalize_preserves_prepared_receipt_and_never_rewrites_config(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        replace, _ = self.receipt_replace_failure()
        with mock.patch.object(settings.os, "replace", side_effect=replace), mock.patch.object(settings.time, "sleep"):
            partial = self.apply(plan_path)
        receipt_path = Path(partial["receipt_path"])
        prepared = receipt_path.read_bytes()
        current = source.read_bytes()
        original_replace = settings.os.replace
        with mock.patch.object(settings.os, "replace", wraps=original_replace) as writes:
            result = settings.finalize(receipt_path, game_closed=True)
        self.assertEqual(result["state"], "finalized")
        self.assertFalse(result["config_rewritten"])
        self.assertEqual(source.read_bytes(), current)
        self.assertEqual(Path(result["previous_receipt_path"]).read_bytes(), prepared)
        self.assertTrue(all(Path(call.args[1]) != source for call in writes.call_args_list))
        self.assertEqual(settings.finalize(receipt_path, game_closed=True)["state"], "already_finalized")
        settings.rollback(receipt_path, game_closed=True)
        self.assertEqual(source.read_bytes(), b"[Controls]\nScoped=0.72\n")
        self.assertEqual(settings.status(receipt_path)["source_state"], "original")

    def test_finalize_rejects_modified_source_and_backup_without_writes(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        result = self.apply(plan_path)
        applied = source.read_bytes()
        source.write_bytes(applied + b"Other=5\n")
        with self.assertRaisesRegex(settings.SettingsError, "applied hash"):
            settings.finalize(result["receipt_path"], game_closed=True)
        self.assertFalse(list(self.root.glob("*.pre-finalize-*.json")))
        self.assertEqual(settings.status(result["receipt_path"])["source_state"], "modified")
        source.write_bytes(applied)
        Path(result["backup_path"]).write_bytes(b"modified")
        with self.assertRaisesRegex(settings.SettingsError, "Backup hash"):
            settings.finalize(result["receipt_path"], game_closed=True)

    def test_persistent_finalize_lock_preserves_evidence_and_reports_partial(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        replace, _ = self.receipt_replace_failure()
        with mock.patch.object(settings.os, "replace", side_effect=replace), mock.patch.object(settings.time, "sleep"):
            partial = self.apply(plan_path)
            result = settings.finalize(partial["receipt_path"], game_closed=True)
        self.assertEqual(result["state"], "applied_receipt_pending")
        self.assertFalse(result["config_rewritten"])
        self.assertEqual(Path(result["previous_receipt_path"]).read_bytes(), Path(result["receipt_path"]).read_bytes())

    def test_retry_never_overwrites_competing_source_change(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        newer = b"[Controls]\nScoped=0.90\n"

        def competing_write(temporary, destination):
            source.write_bytes(newer)
            raise PermissionError("synthetic concurrent writer")

        with mock.patch.object(settings.os, "replace", side_effect=competing_write) as replace, mock.patch.object(settings.time, "sleep"):
            with self.assertRaisesRegex(settings.SettingsError, "changed before atomic"):
                self.apply(plan_path)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(source.read_bytes(), newer)

    def test_partial_result_reports_competing_write_after_application(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        newer = b"[Controls]\nScoped=0.80\n"
        original_replace = settings.os.replace

        def source_changes_during_receipt_lock(temporary, destination):
            if str(destination).endswith(".receipt.json"):
                source.write_bytes(newer)
                raise PermissionError("synthetic receipt lock and competing writer")
            return original_replace(temporary, destination)

        with mock.patch.object(settings.os, "replace", side_effect=source_changes_during_receipt_lock), mock.patch.object(settings.time, "sleep"):
            result = self.apply(plan_path)
        self.assertEqual(result["state"], "source_modified_audit_pending")
        self.assertEqual(result["source_state"], "modified")
        self.assertEqual(result["receipt_state"], "prepared")
        self.assertEqual(source.read_bytes(), newer)
        self.assertFalse(settings.status(result["receipt_path"])["finalize_ready"])

    def test_status_is_read_only_and_finalize_requires_closed_game(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        result = self.apply(plan_path)
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        state = settings.status(result["receipt_path"])
        self.assertTrue(state["rollback_ready"])
        with self.assertRaisesRegex(settings.SettingsError, "requires --game-closed"):
            settings.finalize(result["receipt_path"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_rollback_audit_failure_reports_restored_file_and_keeps_audit(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        applied = self.apply(plan_path)
        original_replace = settings.os.replace

        def fail_rollback_audit(temporary, destination):
            if ".rollback-" in str(destination):
                raise PermissionError("synthetic rollback audit lock")
            return original_replace(temporary, destination)

        with mock.patch.object(settings.os, "replace", side_effect=fail_rollback_audit), mock.patch.object(settings.time, "sleep"):
            result = settings.rollback(applied["receipt_path"], game_closed=True)
        self.assertEqual(result["state"], "rolled_back_audit_pending")
        self.assertEqual(result["source_state"], "original")
        self.assertEqual(source.read_bytes(), b"[Controls]\nScoped=0.72\n")
        self.assertEqual(json.loads(Path(result["audit_path"]).read_text())["state"], "prepared")

    def test_duplicate_json_artifact_fields_rejected(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        content = plan_path.read_text(encoding="utf-8")
        plan_path.write_text('{"schema": "settings-file/plan-v1", ' + content[1:], encoding="utf-8")
        with self.assertRaisesRegex(settings.SettingsError, "Duplicate JSON field"):
            self.apply(plan_path)

    def test_modified_plan_rejected(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, plan = self.plan(source)
        plan["changes"][0]["new_value"] = "0.50"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(settings.SettingsError, "Plan contents"):
            self.apply(plan_path)

    def test_duplicate_change_rejected(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        with self.assertRaisesRegex(settings.SettingsError, "more than once"):
            self.plan(source, [self.change(), self.change()])

    def test_injected_lines_or_comment_semantics_rejected(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        for new in ("0.70\nOther=99", "0.70 ; invisible", "NaN\x00"):
            with self.subTest(new=new), self.assertRaises(settings.SettingsError):
                self.plan(source, [self.change(new=new)])

    def test_rollback_rejects_modified_current_file(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        result = self.apply(plan_path)
        changed = source.read_bytes() + b"Other=99\n"
        source.write_bytes(changed)
        with self.assertRaisesRegex(settings.SettingsError, "later changes"):
            settings.rollback(result["receipt_path"], game_closed=True)
        self.assertEqual(source.read_bytes(), changed)

    def test_rollback_rejects_corrupted_backup(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        result = self.apply(plan_path)
        Path(result["backup_path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(settings.SettingsError, "Backup hash"):
            settings.rollback(result["receipt_path"], game_closed=True)
        self.assertIn(b"Scoped=0.70", source.read_bytes())

    def test_workflow_acknowledgments_required(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        for kwargs in ({}, {"reviewed": True}, {"game_closed": True}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(settings.SettingsError, "requires"):
                settings.apply_plan(plan_path, **kwargs)
        with self.assertRaisesRegex(settings.SettingsError, "requires"):
            settings.rollback("missing.json")

    def test_binary_json_invalid_encoding_and_unknown_syntax_rejected(self):
        for content in (b"GVAS\x00\x00", b'{"Scoped":0.72}', b"[Controls]\nScoped=\xff\n", b"arbitrary unsupported data"):
            with self.subTest(content=content):
                source = self.fixture(content)
                with self.assertRaises(settings.SettingsError):
                    self.plan(source)

    def test_backups_and_receipts_are_unique_across_operations(self):
        source = self.fixture(b"[Controls]\nScoped=0.72\n")
        plan_path, _ = self.plan(source)
        first = self.apply(plan_path)
        plan_path, _ = self.plan(source, [self.change("0.70", "0.68")])
        second = self.apply(plan_path)
        self.assertNotEqual(first["backup_path"], second["backup_path"])
        self.assertNotEqual(first["receipt_path"], second["receipt_path"])
        self.assertIn(b"0.72", Path(first["backup_path"]).read_bytes())
        self.assertIn(b"0.70", Path(second["backup_path"]).read_bytes())

    def test_cli_dry_run_apply_and_rollback(self):
        source = self.fixture(b"[Controls]\r\nScoped=0.72\r\n")
        plan_path = self.root / "review.json"
        planned = subprocess.run([sys.executable, str(SCRIPT), "plan", str(source), "--section", "Controls",
                                  "--change", "Scoped", "0.72", "0.70", "--output", str(plan_path)],
                                 capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertIn(b"0.72", source.read_bytes())
        refused = subprocess.run([sys.executable, str(SCRIPT), "apply", str(plan_path)], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        applied = subprocess.run([sys.executable, str(SCRIPT), "apply", str(plan_path), "--reviewed", "--game-closed"],
                                 capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        receipt = json.loads(applied.stdout)["receipt_path"]
        restored = subprocess.run([sys.executable, str(SCRIPT), "rollback", receipt, "--game-closed"],
                                  capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(source.read_bytes(), b"[Controls]\r\nScoped=0.72\r\n")

    def gvas(self, values=None):
        values = values or dict(zip(settings.FINALS_KEYS, ("48.0", "1.0", "0.79")))
        data = b"GVAS\x03\x00\x00\x00synthetic unrelated header\xff\x00"
        for key, value in values.items():
            key_bytes, value_bytes = key.encode("ascii") + b"\x00", value.encode("ascii") + b"\x00"
            data += struct.pack("<i", len(key_bytes)) + key_bytes + struct.pack("<i", len(value_bytes)) + value_bytes
            data += b"\xfe\x00unrelated trailer\x00"
        return data

    def test_finals_verified_layout_same_length_apply_and_rollback(self):
        original = self.gvas()
        source = self.fixture(original, "synthetic.sav")
        key = settings.FINALS_KEYS[2]
        inspection = settings.inspect_file(source, [{"key": key}], "finals-gvas")
        self.assertEqual(inspection["values"][0]["value"], "0.79")
        self.assertIsNone(inspection["values"][0]["line"])
        plan_path, _ = self.plan(source, [self.change("0.79", "0.72", key, None)], "finals-gvas")
        result = self.apply(plan_path)
        self.assertEqual(source.read_bytes(), original.replace(b"0.79\x00", b"0.72\x00"))
        self.assertEqual(len(source.read_bytes()), len(original))
        settings.rollback(result["receipt_path"], game_closed=True)
        self.assertEqual(source.read_bytes(), original)

    def test_finals_rejects_length_change_nonfinite_nonpositive_and_unknown_key(self):
        source = self.fixture(self.gvas(), "synthetic.sav")
        key = settings.FINALS_KEYS[2]
        for new in ("0.7", "0.720", "NaN", "-1.0", "0.00", "1e99" * 30):
            with self.subTest(new=new), self.assertRaises(settings.SettingsError):
                self.plan(source, [self.change("0.79", new, key, None)], "finals-gvas")
        with self.assertRaisesRegex(settings.SettingsError, "whitelisted"):
            settings.inspect_file(source, [{"key": "GameplayOption.Invented"}], "finals-gvas")

    def test_finals_rejects_duplicate_missing_and_different_fstring_layout(self):
        key = settings.FINALS_KEYS[2]
        good = self.gvas({key: "0.79"})
        key_bytes = key.encode("ascii")
        for bad in (good + key_bytes, good.replace(key_bytes, b"x" * len(key_bytes)),
                    good.replace(b"GVAS", b"FAIL", 1),
                    good.replace(struct.pack("<i", len(key_bytes) + 1) + key_bytes,
                                 struct.pack("<i", len(key_bytes)) + key_bytes),
                    good.replace(b"\x05\x00\x00\x000.79\x00", b"\x06\x00\x00\x000.79\x00"),
                    good.replace(b"0.79\x00", b"0.79X"), self.gvas({key: "NaN"})):
            with self.subTest(bad=bad[-80:]):
                source = self.fixture(bad, "synthetic.sav")
                with self.assertRaises(settings.SettingsError):
                    settings.inspect_file(source, [{"key": key}], "finals-gvas")


if __name__ == "__main__":
    unittest.main()
