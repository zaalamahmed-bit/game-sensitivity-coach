# Discover the actual settings

The agent performs this work. A profile JSON is an artifact it writes, not a form for the gamer.

1. Identify the game and edition, platform, account/profile, install root, and relevant weapon/aim modes from context. Inspect visible window titles or launcher manifests if available. Do not manipulate gameplay controls to identify the game.
2. Locate candidate user settings. Start with documented locations and the provided install path, then AppData Local/Roaming/LocalLow, redirected Documents, Saved Games, launcher account folders, Steam userdata, or Linux/Proton equivalents. Use `rg --files` and bounded searches. A registry-backed game needs a separate known preference adapter.
3. Read only relevant files/keys. Capture file hash, format/encoding and selector. Distinguish saved defaults, an inactive account, synced replicas, and the active local profile. A visible settings menu is useful corroboration when available. A recent modification time is a clue, not proof.
4. Discover meaning as well as numbers: a UI percentage may differ from a file decimal; a scoped multiplier may multiply global gain, normal ADS gain, an optical factor, or a per-weapon setting. Record raw input/acceleration/FOV/uniform aiming because these can change the mapping. Do not alter them just to simplify conversion.
5. Write a versioned profile, using null for unknown facts and separate provenance per setting. If UI and file disagree, explain and resolve which applies before editing. Do not silently average or overwrite the conflict.

## Helpers

```text
python scripts/discover_settings.py --game "THE FINALS"
python scripts/discover_settings.py --game "Another Game" --root "<resolved candidate directory>"
python scripts/discover_settings.py --game "Another Game" --alias "PublisherOrEngineProject"
python scripts/settings_file.py inspect "<file>" --format ini --section Controls --key Sensitivity
python scripts/settings_file.py inspect "<file>" --format frostbite --key GstInput.MouseSensitivity
```

Discovery lists candidates and errors without reading their contents. It bounds traversal and skips symlinks/reparse points. A supplied root replaces default hints. Resolve a redirected directory or unavailable cloud placeholder explicitly instead of repeatedly broadening the scan.

For THE FINALS, `--format finals-gvas` reads only the three recognized sensitivity fields described in [game-examples.md](game-examples.md). Other binary layouts require a proper parser or visual settings read; a matching string somewhere in a save is not sufficient to interpret arbitrary adjacent bytes.

## Units and confidence

- Store the literal file value and menu representation separately. Preserve trailing precision for patching.
- DPI comes from mouse configuration or an explicit user report, not Windows pointer speed or packet frequency. Read a vendor profile only through an available documented source. Do not scan unrelated credentials/configuration.
- Use labels such as `file-read`, `visual-read`, `user-reported`, `historical`, `measured`, and `unknown`. Include a time and evidence reference. Never relabel a user's report as a hardware measurement.
- Physical cm/360 needs a known DPI and angular calibration. Raw deltas still support relative movement analysis without DPI.
- If a game controls a 2D cursor rather than a rotating camera, choose a calibrated cursor-distance metric. The bundled angular converter cannot convert that model; do not report cm/360 for it.

Historical paths and keys are discovery hints. Recheck the installed game's format and version before use.
