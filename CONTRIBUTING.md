# Contributing

Contributions are welcome for settings discovery, format adapters, capture reliability, evidence review, conversion, and documentation. For a substantial change or a new game adapter, open an issue describing the problem and proposed approach first.

## Make a change

1. Fork the repository and create a branch for one focused change.
2. Read [SKILL.md](SKILL.md) and the relevant [references](references/). Keep the agent workflow and user-facing documentation consistent with the implementation.
3. Use synthetic fixtures or minimal, redacted reproductions. Do not commit gameplay recordings, real saves, account profiles, configuration backups, credentials, or machine-specific paths.
4. For behavior changes, add or update a regression test that demonstrates the intended behavior. Document any new command, supported format, or limitation.
5. Run the existing suite from the repository root:

   ```sh
   python -m unittest discover -s tests -v
   ```

Use Python 3.8 or newer. The scripts use the standard library. The synthetic encoder test uses FFmpeg when available and otherwise reports a skip; include skipped checks and platform limitations in your pull request.

## Preserve the evidence and recovery model

- Keep discovery and recommendations separate from authorized configuration changes.
- Preserve source-hash checks, exact field selection, unrelated bytes, backups, receipts, and guarded rollback when changing settings helpers.
- Establish a game's conversion scaling with a source or measurement. Label assumptions and synthetic examples; do not turn personal settings into universal presets.
- Keep raw input counts, pixel measurements, and the host agent's visual assessment distinct. A passing test suite does not establish better aim, live capture compatibility, or anti-cheat approval.

## Submit a pull request

Explain the problem, resulting behavior, and validation. For game-specific work, name the game/version and distinguish synthetic checks from anything verified on a target computer. You do not need to publish private gameplay evidence to contribute.

Report ordinary bugs through the issue templates. Report vulnerabilities privately using [SECURITY.md](SECURITY.md).
