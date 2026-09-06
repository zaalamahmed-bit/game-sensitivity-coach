# Apply only the reviewed sensitivity change

Discovery, conversion and recommendation are non-mutating. A user who asks to apply a specified proposal has authorized it; a user who asks only for advice has not. An explicitly authorized bounded iterative experiment can cover multiple stated changes. Preserve that authorization across turns rather than repeatedly asking. Prepare the exact diff and recovery artifacts before an approval question if one is needed.

## Text and supported save workflow

1. Confirm the active profile and game edition. Verify the game is closed through available process/window evidence. Do not kill a running game to edit its save. If a launcher or sync tool is still changing the file, wait and reread; do not disable sync or permanently mark the file read-only.
2. Inspect the desired fields. Keys/sections are exact and case-sensitive. The helper refuses ambiguous duplicates, missing values, unsupported encoding, and unrecognized layouts.
3. Generate a **new** plan with exact expected-old and new values. Review the path, source hash, units and only the intended changes. The plan itself does not edit the source or create a backup.
4. With authorization and the game closed, apply the plan. The helper revalidates the file hash and expected values, writes a unique byte-identical sibling backup, atomically replaces the edited file, verifies its hash, and writes a receipt. Preserve backup and receipt together.
5. Read the persisted fields back, then verify in-game display/behavior when the game is relaunched. Record any rounding or normalization; do not immediately fight the game with repeated writes. Continue observation under the updated profile version.

```text
python scripts/settings_file.py inspect "<config>" --format ini --section Controls --key Sensitivity
python scripts/settings_file.py plan "<config>" --format ini --section Controls --change Sensitivity 1.00 0.96 --output "<new-plan.json>"
python scripts/settings_file.py apply "<new-plan.json>" --reviewed --game-closed
python scripts/settings_file.py rollback "<receipt.json>" --game-closed
```

Repeat `--change KEY OLD NEW` for a small explicitly authorized set of changes. A plan uses one optional INI section; generate separate plans for separate files/sections and re-plan after each file mutation, because its source hash changes. A multigame transfer is not authorization to modify unrelated controller, graphics, network, keybinding, or accessibility settings.

The agent supplies `--reviewed` and `--game-closed` after checking these facts. They are not a request for the gamer to fill a form, and they do not override sandbox or OS permission requirements.

## Formats

- `ini`: sectioned or unsectioned `key=value` text; omitted section searches all and requires a unique match.
- `frostbite`: whitespace-separated profile entries such as `GstInput.MouseSensitivity 0.016508`.
- `finals-gvas`: only a verified GVAS layout containing THE FINALS' three named ASCII FString sensitivity entries; same-length numeric replacements only. See [game-examples.md](game-examples.md).

Supported text encodings include UTF-8, UTF-8 BOM, and BOM-marked UTF-16. Preserve original encoding, BOM, line endings and unrelated bytes. The helper intentionally does not rewrite arbitrary JSON, registry data, encrypted saves or general Unreal structures. For another format, an agent may add a format-aware adapter tested on copies before proposing its use. A same-length binary replacement is safe only for a verified layout without additional integrity requirements; the label “binary” alone does not establish that.

## Stale files, failures, and rollback

A stale hash means something changed since planning. Inspect the new state and prepare a new plan; never pass a force flag or edit the plan's hash to bypass it. If a write fails, check the reported file/receipt state before retrying. Preserve any backup even when final verification fails.

Atomic replacements retry transient permission errors at most six times, with 1.55 seconds of total delay. The source is rechecked before each source-replacement attempt, so a competing write aborts the patch instead of being overwritten during a retry. This is not an operating-system-wide writer lock; the game and other config-writing tools must remain inactive.

If the config change succeeds but receipt finalization fails, the CLI returns **exit code 3** and JSON with `partial_success: true`, `state: applied_receipt_pending`, observed source/receipt states and hashes, artifact paths, and `recovery_arguments`. A failed rollback-audit update similarly reports `rolled_back_audit_pending`. If another writer has subsequently changed the source, the state instead reports `source_modified_audit_pending` (or `source_unreadable_audit_pending` when readback fails). Do not rerun `apply` or `rollback` just because an audit write failed. Exit code 0 means the requested command completed; exit code 2 means validation or another operation failed.

Use the receipt to inspect the actual persisted state, then finalize the receipt or perform a guarded rollback as appropriate:

```text
python scripts/settings_file.py status "<receipt.json>"
python scripts/settings_file.py finalize "<receipt.json>" --game-closed
python scripts/settings_file.py rollback "<receipt.json>" --game-closed
```

`status` is read-only. It validates the original backup and patch recipe and reports `source_state` (`applied`, `original`, or `modified`), `receipt_state`, `finalize_ready`, and `rollback_ready`. `finalize` requires the source still to match the applied hash. It preserves a byte-identical `*.pre-finalize-<id>.json` snapshot, then updates **only the receipt**; it never reapplies the patch or rewrites the config. An already-finalized matching receipt is a harmless no-op. The original application-completion time is not invented during recovery. A `prepared` apply receipt also supports guarded rollback directly. After `rolled_back_audit_pending`, use `status` to verify `source_state: original`, retain the prepared rollback audit, and record independent verification in the iteration journal; do not restore the same file again.

Rollback restores the backup only if the current file still matches the applied result. If the gamer or game subsequently changed it, stop the full-file rollback and prepare a newly reviewed selective correction to avoid losing those changes. Keep the original patch receipt and a new rollback audit file. A byte-identical file readback proves persistence, not that the game used the value; verify that separately.

An iterative tuning journal should record profile version, capture session, proposed setting, reason, approval scope, plan/receipt paths, game readback, comparison result, and keep/rollback decision. Stop when the user stops, the budget ends, evidence is inconclusive, or the environment no longer supports reliable capture/patching.
