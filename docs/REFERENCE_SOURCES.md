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
user-linked upload. Their exact equivalence has **not** been verified. No source
resolution, frame rate, duration, or media checksum is claimed as verified by this
research.

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

## What was and was not retrieved

Built-in web search returned the indexed title and description excerpts above.
Direct retrieval of the two YouTube watch pages and the original NicoVideo page
failed during this research. No animation or audio bytes were obtained through
that search, and no frame/audio comparison was performed. Search snippets and
uploader metadata are not media-level verification.

`tools/probe_references.py --report build/reference-access.json` records a fresh,
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

Public project releases contain project code and synthetic verification
material, not the Bad Apple!! animation or recording. Synthetic fixtures are
clearly labeled and do not stand in for the requested animation.

No permission to bundle the original media has been established. The
[official Touhou fan guidelines](https://touhou-project.news/guidelines_en/)
require fan-work identification and do not permit inclusion of other fan
creators' works without their permission. The presence of an official video on
a streaming site does not establish redistribution permission for this project.
Obtain the necessary rights before publishing a media-containing build.
