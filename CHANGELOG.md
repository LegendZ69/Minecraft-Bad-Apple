# Changelog

Mod versions below all target Minecraft Java Edition **1.21.1** unless stated
otherwise. Project code and synthetic test material are separate from the
original animation and recording, which are not distributed in these releases.

## 1.1.0 — original-media runtime verification pending

- Add distinct official, original, and user-reference download selections and
  source provenance.
- Add exhaustive archive-to-source verification and reusable synthetic fixtures.
- Acquire the original NicoVideo source in cloud
  [run 34176311242](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34176311242)
  and verify all 6,573 decoded 512 × 384 frames, relative timestamps,
  219.100-second duration, and normalized audio against the converted archive.
- Add isolated Minecraft cloud smoke testing and saved verification evidence.
- Correct stale audio-clock readings after backward seeks, restart, and loops;
  retain elapsed time when a stalled device falls back to silent playback.
- Replace the inherited native `Clip` path with a 20 ms `SourceDataLine` adapter
  and test Linux with `PULSE_LATENCY_MSEC=20`, following the completed four-case
  [buffer probe](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34177059058).
  Only that combination produced the intended nine-second stereo output; see the
  [source-backed diagnosis](docs/releases/v1.1.0.md#native-audio-buffering-diagnosis).
- Pass 96 Python tests, 37 Java tests, reproducible Fabric JAR checks, and actual
  Minecraft synthetic controls/strict stereo-audio verification in Fabric's
  development runtime in
  [integrated run 34177368921](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34177368921).
- Document primary reference sources, distribution limits, and versioned builds.

Both YouTube references remain sign-in/bot gated, so equivalence with them is not
verified. Original-source conversion and 219-second synthetic conversion stress
checks are complete; original-media Minecraft runtime verification remains
pending. See the
[v1.1.0 release record](docs/releases/v1.1.0.md) for the verification checklist.
The remapped release JAR has not been launched in an installed production
profile; its build/structure/reproducibility checks are separate evidence.

## 1.0.0 — baseline

Source: `2ec4c20d237a3f3f5d40bd02ffadf545229fc50e`.

- Add the Minecraft Java 1.21.1 Fabric client player, native-size lossless frame
  conversion, source-timestamp playback, synchronized audio, and playback controls.
- Add one-pixel-per-block screen sizing, bounded background decoding, and archive
  validation.
- Compile successfully and pass 24 tests in
  [Actions run 34174416502](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34174416502).

No baseline in-game verification or full-source-media comparison is claimed.
See the [v1.0.0 release record](docs/releases/v1.0.0.md).
