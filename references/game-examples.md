# Examples from the development work

These are historical observations from one Windows installation, not presets for another gamer or a promise that a later build uses the same format. The case study has been summarized without the user's paths, account data, or full game saves. The supplied README screenshot is the sole included real gameplay image.

## THE FINALS: discover separate settings

The install directory did not contain the active sensitivity. The observed path was:

```text
%LOCALAPPDATA%/Discovery/Saved/SaveGames/EmbarkOptionSaveGame.sav
```

Other settings were in `Discovery/Saved/Config/WindowsClient/GameUserSettings.ini`. The sensitivity save started with `GVAS`. A read-only inspection of the original backup confirmed separate named ASCII FString entries:

| Key | Original stored string |
|---|---|
| `GameplayOption.Controls.MouseSensitivity` | `48.0` |
| `GameplayOption.Controls.MouseZoomSensitivity` | `1.0` |
| `GameplayOption.Controls.MouseScopedZoomSensitivity` | `0.79` |

In that layout, each ASCII key had an int32 little-endian length including its NUL, followed by an equivalent length-prefixed numeric string value. The original operation backed up the closed game's file and replaced `48.0` with `55.2` and `0.79` with `0.76`, retaining normal zoom and total byte length. These strings demonstrate representation; do not infer that every stored mouse field is a normalized 0–1 fraction.

The bundled helper recognizes only those three exact entries, validates their lengths and terminators, and permits only same-length positive numeric replacements. It is not a full GVAS parser and cannot verify arbitrary checksums or future layouts. Reconfirm the active build before applying; use a format-aware adapter or supported settings UI for other layouts/lengths.

```text
python scripts/settings_file.py inspect "<EmbarkOptionSaveGame.sav>" --format finals-gvas --key GameplayOption.Controls.MouseSensitivity --key GameplayOption.Controls.MouseZoomSensitivity --key GameplayOption.Controls.MouseScopedZoomSensitivity
```

Later, the gamer reported **72% scoped**, correcting the historical assumption of 0.76. The recording reports retained their original metadata and added a user-reported correction to **0.72**. This is why the reusable runtime has no personal DPI or sensitivity defaults.

## THE FINALS to Battlefield 6: convert, then patch the active profile

The observed Battlefield profile was under the user's Documents folder:

```text
<Documents>/Battlefield 6/settings/steam/PROFSAVE_profile
```

Other installations may use `settings/PROFSAVE_profile` or a different account subfolder. Resolve redirected Documents and verify which file the installed game uses.

Relevant observed fields included `GstInput.MouseSensitivity`, `GstInput.SoldierZoomSensitivityAll`, per-optic `GstInput.SoldierZoomSensitivity*`, raw-input settings, `GstInput.UniformSoldierAiming`, and `GstInput.UniformSoldierAimingCoefficient`.

The historical conversion mapped a THE FINALS look value of 55.2 to Battlefield file value `0.016508`, displayed at approximately slider 22.01, under an **800 DPI assumption** and a reported physical baseline near **20.71 cm/360**. The file/UI factor of `0.000750` was documented in a tester's [Battlefield 6 sensitivity guide](https://gist.github.com/Jotunn/a574f25d18376f53bd1fb0ff8e4b9e04). That factor relates two representations inside Battlefield; it alone does not derive a cross-game angular conversion. Reverify calibration for the installed build before reproducing the numbers.

The old operation also copied a 0.76 global zoom slowdown and preserved coefficient 1.78 and individual optics at 1.00. **Treat that as a historical setting choice, not proof of equivalent scoped feel.** The reusable workflow requires separate optical/calibration context for ADS transfer and does not automatically carry the old 0.76 into the corrected 0.72 baseline.

A later restoration exposed the value of exact byte preservation and comparing the correct old/new direction. Do not copy a whole historic profile merely to change sensitivity: it may contain unrelated keybindings, graphics, vehicle and account state. The skill's plan includes explicit expected-old and new values, source hash, backup and guarded rollback.

## Holdfast: another format, the same discovery process

The observed file was:

```text
<user>/AppData/LocalLow/Anvil Game Studio/Holdfast NaW/HoldfastOptions.ini
```

It held `mouseSensitivity`, `mouseSensitivityFirearmAiming`, and `mouseSensitivityMeleeCombat`. The historical patch used `2.509091`, `0.76`, and `1.00`. Those values depended on a community conversion assumption and were not established by our passive recorder; they are deliberately absent from runtime defaults and conversion constants. Discover current scaling rather than reproducing them blindly.

## Passive sniper review: retaining a setting is a valid result

At the user-reported 72% scoped baseline, the agent screened 63 click candidates across two segments, excluding gadgets, object shots, obscured views and spectator/death frames. Reviewed SR-84 sequences included three clear hits showing 118 damage, one clear downward overtravel, and repeated scope-entry or unfinished tracking sequences. The supported decision was to **retain 72%**, not invent a lower value or call it an optimum.

The implementation lesson is to store the source frame and actual input/capture timing for each finding. Raw direction reversals, a few hits, or a single dramatic miss are insufficient alone. A future candidate should be compared against baseline under similar conditions, with the agent doing the annotation work.
