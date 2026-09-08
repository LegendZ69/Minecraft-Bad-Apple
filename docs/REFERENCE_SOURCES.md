# Reference sources and media provenance

Research checked September 8, 2026. This is an independent fan project based on
Touhou Project. It is not endorsed by Mojang, Microsoft, Team Shanghai Alice,
Alstroemeria Records, or the animation's creators.

## Three distinct sources

| Source | Role | Converter selection |
| --- | --- | --- |
| [User-linked YouTube video: FtutLA63Cp8](https://www.youtube.com/watch?v=FtutLA63Cp8) | The user's requested visual reference; not established as a rights-holder upload. | `--download-reference` |
| [Alstroemeria Records: i41KoE0iMYU](https://www.youtube.com/watch?v=i41KoE0iMYU) | Official label upload, titled “Bad Apple!! feat.nomico (Shadow Animation Version)”, catalog reference ACVS.008_Tr.00. | `--download-official` |
| [Anira's original NicoVideo: sm8628149](https://www.nicovideo.jp/watch/sm8628149) | Original shadow-animation publication. | `--download-original` |

These selections never silently substitute for each other. An official upload is
not necessarily the same edit, encoding, duration, or frame sequence as the
user-linked upload. Their exact equivalence has **not** been verified. Source
comparisons below verify downloaded NicoVideo files and their conversions;
they do not establish equivalence with either YouTube upload.

## Why the label upload is an official reference

- The [artist's own About page](https://alst.net/about/) identifies Masayoshi
  Minoshima as the owner of Alstroemeria Records and lists “Bad Apple feat.nomico”
  among his works.
- The [label's YouTube channel](https://www.youtube.com/@AlstroemeriaRecords)
  identifies the label and links its official website, `alst.net`.
- Search-indexed excerpts of the [official video description](https://www.youtube.com/watch?v=i41KoE0iMYU)
  link that website, credit the shadow animation to あにら (Anira), and identify
  the original NicoVideo publication date as October 27, 2009. The description
  also asserts ownership of the sound sources, rather than offering an open
  redistribution license.
- The arranger's [October 27, 2024 anniversary post](https://x.com/M_Minoshima/status/1850502011508437182)
  references `sm8628149` and the original animation's fifteenth anniversary.

The [official album page](https://alst.net/arcd0056/) credits Masayoshi Minoshima,
Haruka, and nomico. The [official Danmaku Kagura music page](https://danmaku.jp/archive/music/m018/)
identifies the arrangement by Masayoshi Minoshima, lyrics by Haruka, vocals by
nomico, and original composition by ZUN. These credits identify contributors;
they are not a license to redistribute their recordings or animation.

## Retrieval and verification record

Built-in web search returned the indexed title and description excerpts above.
Direct retrieval of the two YouTube watch pages and the original NicoVideo page
failed during the initial research. The subsequent cloud run successfully
obtained media from the original NicoVideo URL and verified its conversion, as
recorded below. Both YouTube URLs returned a sign-in/bot-confirmation gate in
that cloud run; neither was downloaded or compared. Search snippets and uploader
metadata alone are not media-level verification.

### First completed original-source conversion check

[Cloud run 34176311242](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34176311242)
on September 8, 2026 downloaded `sm8628149`, converted it, and exhaustively
compared the archive with that local source. Its
[reference-acquisition-evidence artifact](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34176311242/artifacts/10037389762)
contains `reference-access.json`, `reference-reports/reference-acquisition.json`,
and `reference-reports/original-source-comparison.json`.

| Measurement | Verified result |
| --- | --- |
| Source dimensions | 512 × 384 decoded pixels |
| Video frames | All 6,573 decoded frames compared |
| RGB data | All 3,876,913,152 RGB24 bytes equal |
| Timing | Every relative presentation timestamp and video duration equal |
| Video/archive duration | 219.100 seconds; the platform metadata rounds to 219 seconds |
| Normalized audio | All 10,516,800 stereo PCM sample frames equal at 48 kHz, signed 16-bit |
| Archive integrity | ZIP CRCs, complete frame sequence, every PNG's dimensions/chunk CRCs/scanlines, timestamps, PCM format and duration passed |

Downloaded source file SHA-256:

```text
93480e9b35738d81435d8434df3e031bd3ac34634c73ba4f4ce99d4595bbf4fa
```

The comparison preserves every FFmpeg-decoded RGB24 pixel and relative timestamp.
Audio equality is against the documented aligned, resampled, padded/trimmed
48 kHz stereo signed-16-bit output, not against the compressed source audio
bytes. The hash identifies this downloaded file, not every encoding the service
might provide later. The technical report does not independently authenticate
ownership or establish redistribution rights.

### Completed full-animation Minecraft check

[Integrated run 34178311059](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34178311059)
repeated the source-to-archive comparison and completed the original animation
in actual Minecraft 1.21.1 using Fabric's development runtime: 219.1 seconds of
media in 219.2002 seconds, ending at frame 6,572 without audio fallback or
warnings. All 59 runtime assertions passed, including 45 periodic GPU samples
covering 8,847,360 exact uploaded-texture pixels. Both virtual audio channels
contained 218.4 seconds of non-silent output; physical speakers were not tested.

Only 2,295 of the 6,573 source frames (34.9%) were uploaded during the playthrough;
4,278 were skipped. All source frames are preserved in the archive, but this
test does **not** demonstrate every frame displayed or 30 FPS presentation. Its
mean was 10.74 world-render callbacks per second and maximum sampled uploaded
frame lag was 374 ms. The evidence does not isolate the throughput limitation's
cause. GPU equality is checked against the texture actually uploaded, not every
original frame or the final perspective-transformed screen.

This full run used a separately downloaded container. Its decoded RGB sequence
hash matches the first acquisition above, even though the container and archive
hashes differ:

| Full-run file | SHA-256 |
| --- | --- |
| Downloaded source | `d6c418b138b2c2003500175b3f121eff0fb910f1cfc6966a3128d17589fd75cf` |
| Converted archive | `3ef298e4be6ee183a678d56140a370ad01bf33b9b5f095351a1e92e26a127c53` |

The exact-JAR synthetic production controls/GPU/stereo stage subsequently passed
in [run 34178900645](https://github.com/LegendZ69/Minecraft-Bad-Apple/actions/runs/34178900645);
that completed-step result does not assert whole-run completion. The final
publication gate additionally requires a full-original production-namespace run
bound to the same release JAR SHA-256. Its actual result is recorded in packaged
`build-info.json` and runtime reports, not inferred from the earlier development
run above. The official Fabric production-test launcher is distinct from an
authenticated Minecraft Launcher installation. Neither YouTube upload has a
verified frame/audio comparison with this source. See the
[v1.1.0 verification record](releases/v1.1.0.md) for the complete evidence scope.

`python tools/probe_references.py --report build/reference-access.json` records a fresh,
bounded public metadata probe for each exact URL. A metadata success does not
mean media was downloaded or its identity verified. Access failures remain
explicit in the report; the project does not use cookies, proxies, or challenge
bypasses to hide them.

## Prepare and verify an authorized copy

The mod and tools do not bundle the original video or audio. For a local copy you
are entitled to use, with Python 3.10+, FFmpeg, and ffprobe installed:

```sh
python tools/prepare_video.py "reference.mp4" --source-url "https://www.youtube.com/watch?v=FtutLA63Cp8" --output bad_apple.bapple
python tools/verify_archive.py bad_apple.bapple --source "reference.mp4" --report verification.json
```

The URL here records your provenance assertion. It does not authenticate the
file. Preserve the source file and generated report so the archive can be
compared with the exact input again. Verification checks the conversion against
that input, not against an inaccessible online original.

For an optional public download, install `yt-dlp` and deliberately choose one
source. For example, to choose the label's official upload instead:

```sh
python tools/prepare_video.py --download-official --output bad_apple.bapple
```

If the service requires sign-in, rejects the request, or challenges automated
access, stop that download and use an authorized local file. Do not describe a
different source as the user's exact reference without comparing it.

## Distribution boundary

Public project releases contain project code, synthetic fixtures, and diagnostic
evidence such as reports and selected in-game screenshots. Screenshots may show
animation stills, but the source video, complete animation/archive, and recording
are not bundled. Synthetic fixtures are clearly labeled and do not stand in for
the requested animation.

No permission to bundle the original media has been established. The
[official Touhou fan guidelines](https://touhou-project.news/guidelines_en/)
require fan-work identification and do not permit inclusion of other fan
creators' works without their permission. The presence of an official video on
a streaming site does not establish redistribution permission for this project.
Obtain the necessary rights before publishing a media-containing build.
