# Transfer a familiar setup

Discover both games first. Match a deliberate metric rather than copying an in-game slider or an ADS percentage. The source profile must have provenance, and the target's native config/UI scale must be known.

## Angular turn distance

For a calibrated affine camera model, define:

```text
g(s) = slope * native_setting + offset       # degrees per raw mouse count
cm_per_360 = 914.4 / (DPI * g(s))
target_gain = source_gain * source_DPI / target_DPI
target_setting = (target_gain - target_offset) / target_slope
```

The constant 914.4 is 360 degrees times 2.54 cm/inch. A game's UI or config may have a nonzero offset or nonlinear curve. Do not infer a slope from two unrelated games' raw numbers, assume all sliders are linear, or treat eDPI as comparable across engines.

Obtain calibration from a current documented mapping or passive measured turns with recorded raw counts and verifiable heading change. A full turn must be genuinely observed and unwrapped; moving scenery and wraparound can invalidate it. No input injection is needed. Record build, source, range and uncertainty. If the source is only an unverified prior assumption, mark it `assumed` and give a conditional estimate instead of silently promoting it to fact.

Both DPI values are needed for absolute physical distance. If the same mouse/profile is used without a DPI change, an explicitly stated same-DPI assumption permits a ratio conversion while cm/360 stays unknown.

## Scoped and ADS conversion

Each optic needs its own effective angular mapping at the chosen base sensitivity and view. Equal multipliers do not guarantee equal aiming feel. Preserve the target game's existing uniform-aim/coefficient/FOV choices unless a change to them is part of the request.

For rectilinear views, `monitor_distance` matches the movement needed to reach one fraction `r` of the half-screen on a shared horizontal or vertical axis:

```text
target_gain / source_gain = source_DPI / target_DPI
  * atan(r * tan(target_FOV/2)) / atan(r * tan(source_FOV/2))
```

For `r=0`, use the limit `tan(target_FOV/2)/tan(source_FOV/2)`. The helper does this. Supply the actual rendered FOV on the same axis, not mismatched horizontal/vertical menu labels. This matches a radius in one optical view, not all screen positions and scopes. Unsupported projection, acceleration, FOV-dependent curves, or changing base settings require a different calibrated model.

## Run the helper

```text
python scripts/convert_sensitivity.py examples/conversion.synthetic.json
python scripts/convert_sensitivity.py <agent-authored-request.json> --output <new-result.json>
```

The synthetic example is runnable and deliberately contains no real-game constants. Request structure:

```json
{
  "schema": "game-sensitivity/conversion-request-v1",
  "mode": "turn_distance",
  "source": {
    "game": "Synthetic A", "value": 10, "dpi": 800,
    "calibration": {"model": "affine", "slope": 0.001, "offset": 0,
      "min": 0, "max": 100, "source": "Synthetic fixture", "evidence": "measured"}
  },
  "target": {
    "game": "Synthetic B", "dpi": 800,
    "calibration": {"model": "affine", "slope": 0.002, "offset": 0,
      "min": 0, "max": 100, "source": "Synthetic fixture", "evidence": "measured"}
  },
  "target_decimals": 6
}
```

Use `dpi: null` plus `assume_same_dpi: true` only when explicitly justifiable. For monitor-distance mode add `projection` with `axis`, `source_fov_degrees`, `target_fov_degrees`, `fraction_of_half_screen`, and a nonempty `source` for the FOV evidence.

Output preserves the request, its hash, gain/distance calculations, rounding error and assumptions. A result is always `converted_estimate`, never a measured optimum. It refuses unsupported models and values outside calibrated bounds rather than silently clamping. Before applying, map the output to the exact target key/units, account for supported menu increments, and produce the patch plan described in [apply-and-rollback.md](apply-and-rollback.md).
