# Agent-authored artifacts

Write these outside the installed skill directory, for example in a user-chosen `GameSensitivity` workspace. Avoid synced folders unless that is the user's intended storage. Never package captured sessions, real config backups, machine-specific paths or account data into a public release.

## Current profile

See `examples/profile.example.json`. The capture CLI accepts a flat `settings` object, per-key `setting_sources`, a general `settings_source`, and optional `context`/`evidence`. Known common keys are `dpi`, `look_sensitivity`, `normal_zoom_multiplier`, and `scoped_multiplier`; game-specific scalar keys can retain native values. Unknown values are null. Sources distinguish file reads, visual reads, user reports and history.

Keep native UI values and config strings together in context, including exact field selector, path and original hash when known. File paths in real profiles are resolved local paths. The bundled example intentionally has no real file path or settings. Create a new profile version after each discovered correction or applied change; a historical profile is not automatically current.

## Session and report

The recorder's `aim-observer/session-v1` metadata and raw CSV/video files are immutable capture evidence after finalization. `manifest.json` hashes metadata/CSV; video size is recorded. For stronger transport integrity, compute a video hash when moving the session. Do not claim a video content hash was checked when only its size was available.

`analysis.json` contains actual-frame mapping, quality and candidate windows. `report.html` reads the adjacent video and renders a coach assessment if present. A missing assessment means pending agent review, not a failure or invented recommendation.

Write `coach-assessment.json` after viewing evidence:

```json
{
  "schema": "aim-observer/coach-assessment-v1",
  "source": "assistant-visual-review",
  "status": "baseline_retained",
  "created_at_utc": "2026-09-06T12:00:00+00:00",
  "headline": "Retain the observed setting for now",
  "summary": "The reviewed sample does not establish a repeated directional error.",
  "settings": {"scoped_multiplier": null},
  "evidence": [],
  "limitations": ["No candidate setting was compared."],
  "next_step": "Record a comparable sample if a repeated error appears."
}
```

Replace the example's text and timestamp with evidence-supported current facts; keep null values when a setting remains unknown. Each evidence entry must contain a recorded `frame_index` and a nonempty `observation`. For click-linked evidence, `candidate_id` must match a candidate in this session. For an overview frame or another combat interval, omit `candidate_id` or set it to null; the report still validates the frame reference. An optional top-level `session_id` must match the recorded session. Setting values are native scalars or null, never guesses inserted to satisfy a schema. Use `scripts/review_session.py SESSION --assessment ASSESSMENT.json --verify-manifest` to validate and attach the assessment before rebuilding the report.

An overview evidence entry has this shape; replace the illustrative frame and observation with inspected evidence:

```json
{
  "candidate_id": null,
  "frame_index": 100,
  "observation": "Describe the visible evidence in this recorded overview frame."
}
```

## Visual annotations

An agent-created `assistant-shot-review.json` should contain the session ID, image evidence directory, review method, all screened candidate IDs or screening coverage, exclusions, and selected observations. A coordinate entry can contain:

```json
{
  "candidate_id": "click-00001",
  "frame_index": 100,
  "image": "evidence/frame-000000100.png",
  "coordinate_space": "original captured pixels, origin top-left",
  "crosshair_xy": [960, 540],
  "target_point_xy": [980, 550],
  "target_reference": "estimated upper torso",
  "annotation_uncertainty_px": 8,
  "classification": "unclear",
  "observation": "Synthetic shape example; not gameplay evidence."
}
```

Copy the image path and frame index from `evidence.json`. Only write coordinates actually measured/estimated from the stated source image. Target size, annotation uncertainty and capture timing should bound any conclusion. Preserve original files when drawing overlays or crops.

## Conversion and iteration

The converter preserves its full input, SHA-256 of canonical request JSON, computed gain, chosen metric and assumptions. Patch plans/receipts are separate artifacts produced by `settings_file.py`; recommendation does not mutate files.

Append an iteration journal entry with a unique experiment ID, game/profile version, session paths, baseline/candidate, intended metric, comparison conditions, authorization scope, patch receipt, readback and result (`keep`, `rollback`, `inconclusive`). Record warm-up or changed weapon/FOV/DPI rather than quietly combining incompatible trials. An applied config value can be verified while its performance benefit remains untested.

Late settings corrections belong in `settings-correction.json` using `aim-observer/settings-correction-v1`, `source: user-reported`, `reported_at_utc`, a `settings` subset and a `note`; this changes derived report context without rewriting original `session.json`. For newly discovered file evidence or other correction types, preserve a separate provenance record rather than falsely labeling it user-reported.
