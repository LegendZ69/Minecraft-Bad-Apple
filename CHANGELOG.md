# Changelog

Mod versions below all target Minecraft Java Edition **1.21.1** unless stated
otherwise. Project code and synthetic test material are separate from the
original animation and recording, which are not distributed in these releases.

## 1.1.0 — provenance, playback fixes, and release verification

- Add distinct official, original, and user-reference download selections and
  source provenance.
- Add exhaustive archive-to-source verification and reusable synthetic fixtures.
- Acquire the original NicoVideo source in cloud
  [run 34176311242](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34176311242)
  and verify all 6,573 decoded 512 × 384 frames, relative timestamps,
  219.100-second duration, and normalized audio against the converted archive.
- Add isolated Minecraft cloud smoke testing and saved verification evidence.
- Add a separate exact-JAR, SHA-256-bound production-namespace smoke check using
  Fabric's official [production run tasks](https://docs.fabricmc.net/develop/loom/production-run-tasks);
  require both synthetic and full-original exact-JAR verification for publication.
- Correct stale audio-clock readings after backward seeks, restart, and loops;
  retain elapsed time when a stalled device falls back to silent playback and
  timestamp native queries after they return to avoid false stall detection.
- Fix inherited PNG native-stack exhaustion with expandable-buffer decoding;
  surface decoder termination and reject stale/frozen textures in full-run QA.
- Replace the inherited native `Clip` path with a 20 ms `SourceDataLine` adapter
  and test Linux with `PULSE_LATENCY_MSEC=20`, following the completed four-case
  [buffer probe](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34177059058).
  Only that combination produced the intended nine-second stereo output; see the
  [source-backed diagnosis](docs/releases/v1.1.0.md#native-audio-buffering-diagnosis).
- Pass 101 Python tests, 38 Java tests, reproducible Fabric JAR checks, and actual
  Minecraft synthetic controls/strict stereo-audio verification in Fabric's
  development runtime in
  [integrated run 34178311059](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34178311059).
- Complete the original 219.1-second animation in that development-runtime run
  without audio fallback: 59 assertions and 45 exact GPU readbacks passed.
  The run displayed 2,295 of 6,573 source frames (34.9%), skipping 4,278; this is
  not a claim of displaying every frame or achieving 30 FPS.
- Pass 106 Python and 38 Java tests plus the exact-JAR synthetic production
  controls/GPU/strict stereo stage in
  [run 34178900645](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34178900645).
  These are completed-step results, not a claim that the whole run completed.
  The final gate extensions pass 110 Python tests locally and are rerun by CI.
- Document primary reference sources, distribution limits, and versioned builds.

Both YouTube references remain sign-in/bot gated, so equivalence with them is not
verified. Original-source conversion and 219-second synthetic conversion stress
checks and full original-media development-runtime playback are complete.
Full-original production playback is an enforced final publication gate, not an
already-observed result in this historical record. See the
[v1.1.0 release record](docs/releases/v1.1.0.md) and packaged `build-info.json`
for the final build identity, runtime bindings, and evidence.
The production-test launcher is separate from an authenticated Minecraft
Launcher installation; account/profile setup is not exercised.

## 1.0.0 — baseline

Source: `2ec4c20d237a3f3f5d40bd02ffadf545229fc50e`.

- Add the Minecraft Java 1.21.1 Fabric client player, native-size lossless frame
  conversion, source-timestamp playback, synchronized audio, and playback controls.
- Add one-pixel-per-block screen sizing, bounded background decoding, and archive
  validation.
- Compile successfully and pass 24 tests in
  [Actions run 34174416502](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34174416502).

No baseline in-game verification or full-source-media comparison is claimed.
Later full-source testing identified inherited PNG native-stack exhaustion and
native `Clip` buffering/clock issues; the baseline does not contain the v1.1.0
fixes.
See the [v1.0.0 release record](docs/releases/v1.0.0.md).
