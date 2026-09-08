# Bad Apple in Minecraft

A **Minecraft Java 1.21.1 / Fabric client mod** that plays a source video on a screen inside your world, with native-resolution frames, source timestamps, and synchronized audio.

Requested reference: [YouTube video](https://www.youtube.com/watch?v=FtutLA63Cp8).
Primary references: [original animation by あにら](https://www.nicovideo.jp/watch/sm8628149) and [Alstroemeria Records' upload](https://www.youtube.com/watch?v=i41KoE0iMYU). These are explicit source choices, not automatically interchangeable files. See [reference research and verification](docs/REFERENCE_SOURCES.md).

## What “1:1” means here

- Every decoded video frame is kept at its original dimensions as a lossless RGB PNG. No resizing, black/white thresholding, interpolation, or conversion to a fixed frame rate.
- Original frame timestamps drive playback, including variable frame rate sources. Audio comes from the same file and drives the playback clock when an audio device is available.
- The texture uses nearest-neighbor filtering and retains the source aspect ratio and grayscale detail. Lighting does not darken the screen.
- `/badapple place native` makes **one source pixel span one world block**. The initial screen is 48 blocks wide for convenient viewing.

This is a client-rendered screen, not a wall made from changing Minecraft blocks or a vanilla datapack. It does not change world blocks or require cheats. Other players do not see or hear your local playback. The original video/audio is **not bundled**: prepare your copy with the converter below.

Preserving decoded frames does not make the whole viewing pipeline mathematically lossless: perspective, monitor resolution, audio resampling, and GPU/display settings still apply. A slow client may skip presentation of overdue frames to stay in sync; no player can guarantee 30 distinct displayed frames per second when the game renders below 30 FPS.

## Install

1. Install **Minecraft Java 1.21.1**, [Fabric Loader](https://fabricmc.net/use/installer/) 0.16.5 or newer, and the **Fabric API for 1.21.1**. Minecraft 1.21.1 uses Java 21.
2. Download the installable JAR and optional portable tools bundle from [v1.1.0](https://github.com/LegendZ69/Minecraft-Bad-Apple/releases/tag/v1.1.0). `SHA256SUMS` covers every asset; `build-info.json` records the exact source commit. The evidence ZIP contains measured verification results, not the full original media.
3. Put `minecraft-bad-apple-1.1.0.jar` in your Minecraft instance’s `mods` folder alongside Fabric API. Do not install a sources JAR or smoke-test code.
4. Create a `badapple` folder in the same instance directory (next to `mods`). Put the prepared `bad_apple.bapple` file there.
5. Start the Fabric instance, join a world, face an open area, and run:

```mcfunction
/badapple play
```

Loading runs in the background. The screen rises ahead of you, the camera aims at its center, and playback starts. For the large one-pixel-per-block view, use a clear area or spectator view with sufficient render distance, then run `/badapple place native`.

## Prepare the exact video

Requires **Python 3.10+**, **FFmpeg**, and **ffprobe** on your `PATH`. No Python packages are required for local files.

```sh
python tools/prepare_video.py "Bad Apple.mp4" --output bad_apple.bapple
python tools/verify_archive.py bad_apple.bapple --source "Bad Apple.mp4" --report verification.json
```

The converter probes the actual source instead of assuming its resolution, frame rate, or duration. It extracts all decoded frames and matching audio, then packages them into a seekable `.bapple` archive. The audio is aligned to the first video frame, including offset tracks, converted to PCM 16 stereo 48 kHz, and padded/trimmed to the video duration. Conversion uses temporary files and only replaces the destination on success. Existing output is preserved unless you pass `--force`.

To obtain a specific public source, install `yt-dlp` and choose exactly one optional download mode:

```sh
python -m pip install -U yt-dlp
python tools/prepare_video.py --download-reference --output bad_apple.bapple
# Original creator's NicoVideo upload:
python tools/prepare_video.py --download-original --output original.bapple
# Official label's YouTube upload:
python tools/prepare_video.py --download-official --output official.bapple
```

YouTube refused cloud access with a sign-in/bot check during verification. No authentication gates were bypassed. If a source is unavailable, supply an authorized local copy of that exact reference. Downloading/conversion requires network access only for the optional download; playback itself is offline. No invented or substitute animation is included, and the release does not redistribute the full animation or recording.

## Controls

All commands are local client commands:

| Command | Action |
| --- | --- |
| `/badapple play` | Load `bad_apple.bapple` if needed and play |
| `/badapple load` | Reload the default archive and start |
| `/badapple load "my video.bapple"` | Load another archive from the instance’s `badapple` folder |
| `/badapple pause` / `/badapple resume` | Pause or resume video and audio together |
| `/badapple seek 60` | Seek to 60 seconds |
| `/badapple restart` | Play from the beginning |
| `/badapple loop true` | Enable looping; use `false` to disable |
| `/badapple place` | Place a 48-block-wide screen ahead of your current position |
| `/badapple place 96` | Place a 96-block-wide screen |
| `/badapple place native` | One source pixel per block of screen width/height |
| `/badapple stop` | Reset playback while retaining the screen |
| `/badapple unload` | Remove the screen, cancel loading, and release the movie |
| `/badapple status` | Show time, frame, resolution, and audio warnings |

In a paused single-player game, playback automatically pauses and resumes with the game. Disconnecting or changing dimension unloads the screen. Screen placement is session-local; run the load command again after rejoining.

## Audio and performance

Audio uses the system’s Java Sound output, outside Minecraft’s volume sliders and positional sound system. Use system/application volume to adjust it. If no compatible audio device is available, the mod explicitly reports silent playback and uses a monotonic clock. Pause, seeking, and looping remain available.

On Linux with the ALSA PulseAudio plugin, launch Minecraft with
`PULSE_LATENCY_MSEC=20` in its environment. This is the configuration exercised by
the cloud audio tests; the default PulseAudio buffering can delay starts/seeks.
The mod uses a 20 ms Java Sound streaming buffer rather than the standard
one-second native clip buffer. It does not modify your system audio settings.

PNG decoding happens on a background worker with a small prefetch buffer; textures are uploaded on the render thread. Encoded PNGs use an expandable native buffer, including frames larger than LWJGL's fixed scratch stack. This avoids loading all frames into memory. The PCM audio clip is loaded in memory, capped at 256 MiB (about 23 minutes at the chosen format). Archives also enforce dimension, timestamp, frame-count, and entry-size limits.

For fidelity, cap Minecraft at least as high as the source frame rate, face the screen squarely, use enough screen area on your monitor, and disable shaders/postprocessing that alter the picture. Shader packs and third-party renderers require separate compatibility testing.

## Build and test

Install a **JDK 21** and **Gradle 8.8**, then:

```sh
gradle build
python -m unittest discover -s tests -v
```

The installable mod is `build/libs/minecraft-bad-apple-1.1.0.jar`.

The GitHub Actions workflow installs the pinned toolchain, runs Python and Java tests, compiles/remaps the Fabric mod, checks reproducible JAR bytes, exhaustively compares decoded frames/timestamps/normalized PCM, and launches an actual Minecraft client under Xvfb/Mesa. The isolated smoke harness measures GPU pixels, screen orientation, controls, and a virtual stereo audio sink. A separate Gradle 8.12 / Loom 1.10.5 verification project runs Fabric's official production-test launcher without changing the main Gradle 8.8 / Loom 1.7.4 build. Both its synthetic controls test and full original playthrough must prove that the loaded mod is the exact release JAR by SHA-256, with development mode disabled and the intermediary namespace active.

The full original is downloaded only into the ephemeral runner when publicly accessible. Publication is gated on the successful main-branch build; release assets are downloaded and checksum-verified before and after publication. This production test does not automate account login or exercise the authenticated Minecraft Launcher. Runtime reports count actual presented frames and skips; a complete playthrough does not imply every source frame was displayed.

See the [v1.1.0 verification record](docs/releases/v1.1.0.md) and [historical v1.0.0 record](docs/releases/v1.0.0.md) for completed checks and explicit limits. Physical speakers, other players' computers, multiplayer synchronization, shader compatibility, and equivalence to an inaccessible YouTube reupload are not inferred from cloud tests.

## Archive format

`.bapple` is a ZIP containing `manifest.json`, `frames/000000.png` onward, and optional `audio.wav`. Manifest version 1 records `width`, `height`, `frameCount`, `durationMicros`, `frameTimestampsMicros`, `audio`, and source information. Timestamps begin at 0 and increase strictly. The PNG frames preserve decoded RGB pixels; WAV is signed little-endian 16-bit PCM. The converter’s source is the authoritative writer for this format.
