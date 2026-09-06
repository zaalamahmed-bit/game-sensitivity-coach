# Observe ordinary play

## Capture and evidence

Use native Windows Python for bundled live capture, including when the coordinating agent runs through WSL or a remote connection. A Linux/macOS/cloud environment can analyze existing sessions but cannot acquire this computer's screen without a local capture backend. Do not claim a recording started until the recorder confirms it.

The capture script's `--help` is the CLI authority. Supply `--game` or a profile containing its name, an `--output-root` outside the repository, bounded `--seconds`, and the discovered `--profile` where available. `--window-title` is optional: it overrides the profile's title, which otherwise defaults to the game name. FFmpeg must be on PATH or passed with `--ffmpeg`. Inspect status/errors, actual frame count, focus pauses, free space, and dropped packets. Use software encoding first; a detected NVIDIA GPU does not prove an installed FFmpeg/NVENC combination works. On the original machine, software encoding worked and an old NVENC preset failed.

No ordinary recording requires entering sensitivity metadata or labeling shots. `--wait-for-game` waits for a matching foreground window before starting the session timer; it is bounded separately to 0–3,600 seconds. Without this wait, a visible background game can be selected, but the timer runs while foreground gating keeps capture paused. Monitor selection happens once at launch, using the matching visible window or an explicit zero-based `--monitor`; it does not follow later window moves. A full-monitor capture may include visible game overlays or notifications. Never bypass OS capture permission or a game's protection to obtain images.

Generate the offline report with `scripts/review_session.py SESSION --verify-manifest`. The default `scripts/export_evidence.py SESSION` exports 12 overview frames and six click candidates, with six actual frames on each side. These are evenly spread samples, not exhaustive screening. `--overview-only` omits click sequences entirely and does not require `analysis.json`. To screen all candidates, read their count/IDs from `analysis.json` and set `--click-count` to that count or pass every ID with `--click`; use zero before/after frames for an initial single-frame screen if appropriate. Then export longer sequences around selected acquisitions, and inspect overview frames and other intervals beyond clicks. Supply a fresh `--output` directory for each export, since existing nonempty outputs are preserved. Retain originals, source hashes, selected frame indices and actual capture timestamps. Do not only choose the earliest or most dramatic misses.

Initial recording duration is wall-clock time, including initialization and focus pauses: `--seconds` must be 1–14,400 and cannot exceed the launch-time `--max-total-seconds` cap, which defaults to 3,600. The local control command's `--extend-seconds` adds to the current deadline, while `--total-seconds` replaces the total from the original start. Submit an extension before expiry and confirm its acknowledgment; it cannot resume an already stopped recorder.

## Inspect images as the agent

For a promising shot, inspect consecutive actual frames approximately 400 ms before through 180 ms after the click; expand the interval if acquisition began earlier or discharge occurs later. Choose `--before-frames` and `--after-frames` using the actual CSV timestamps: the default six frames per side does not guarantee those time spans, and capture gaps can lengthen them. The interval is a useful start, not a calibration rule. Use actual capture brackets and midpoint offsets. Identify:

- The player-controlled view, weapon, optic and whether zoom is established. Exclude spectator/replay, menus, interactions and obvious gadgets from opponent aim trials.
- The likely target and visible target region. Record ambiguity if several enemies overlap. Choose a consistent body/head reference without claiming to know the player's intent.
- Whether crosshair motion passes the target region and corrects back, stays short, follows late, or loses visibility. Compare background motion to distinguish camera motion from a moving/jumping target.
- A supported shot event: ammo reduction, firing animation, tracer or hit feedback. Automatic weapons need discharge sampling beyond left-down candidates; click count is not a shot-count denominator.
- Scope entry, recoil, motion blur, damage effects and occlusion. If aim is still entering the scope at the click, that sequence cannot isolate established scoped sensitivity.

Store observations in `assistant-shot-review.json`; the host agent may draw derived overlays or contact sheets but must preserve the original images and record transformations. Coordinates use original captured pixels, top-left origin, x right/y down. Include frame index, crosshair point, target point/bounds, observed/estimated status, and approximate uncertainty. Pixel coordinates are not raw mouse counts or world coordinates.

Inspect only the relevant device's raw input for pre-discharge motion. A reversal statistic across a window including 500 ms after the click can mostly describe recoil recovery. A right-held flag cannot detect toggle ADS. Receipt timestamps and GDI acquisition timestamps have uncalibrated latency; reject judgments that reverse when alignment shifts by one captured frame.

## Decide and compare

Separate descriptive findings from the proposed intervention. Repeated overtravel under comparable optics and target motion can justify a small lower-gain candidate. Late tracking, occlusion, or premature firing may not. A practical starting test can be a few percent change, rounded to a supported setting, but derive direction and magnitude from evidence rather than always applying a fixed decrement. Do not manufacture a setting change to make a session look productive.

Change one factor: for example scoped multiplier while holding look, DPI, FOV and normal zoom stable. Compare baseline A and candidate B under similar weapons/ranges; an A/B/A sequence helps expose warm-up, fatigue and match difficulty. The gamer plays normally; the agent selects comparable segments afterward. Record sample sizes, exclusions and relevant endpoint/acquisition observations. Hits alone do not establish optimality, especially with different targets or network conditions.

Use `baseline_retained`, `test_recommendation`, `validated_recommendation`, or `awaiting_evidence` in the coach assessment. Validation requires a meaningful comparison and is conditional on the observed context. State when there is no justified change. If supported evidence is scarce, give the best current baseline and a focused next observation, not a false confidence percentage.

## Timing quality

`frames.csv` relates encoded frame indices to capture timestamps. FFmpeg constant-frame-rate playback compresses stalls/focus pauses: never seek by elapsed session seconds alone. Use the candidate's mapped frame index divided by configured FPS or the exported frame mapping.

Report meaningful coverage and gaps. In the development run, typical capture intervals were about 35–36 ms and some exceeded 100 ms. An eight-minute requested window contained approximately 7:21 of foreground coverage, despite longer combined segment duration including pre-start footage. These are examples of what to check, not expected performance on another machine.
