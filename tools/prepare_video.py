#!/usr/bin/env python3
"""Package decoded, full-resolution video and synchronized audio for Minecraft.

Requires Python 3.10+, ffmpeg and ffprobe. No Python dependencies are needed for
local files. Use --download-reference only for a source you are permitted to use.
The converter never resizes, thresholds, interpolates, or changes the frame rate.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import zipfile
from fractions import Fraction

REFERENCE_URL = "https://www.youtube.com/watch?v=FtutLA63Cp8"
MAX_DIMENSION = 4096
MAX_FRAMES = 500_000
MAX_DURATION_MICROS = 6 * 60 * 60 * 1_000_000
AUDIO_RATE = 48_000
MAX_AUDIO_BYTES = 256 * 1024 * 1024


class ConversionError(Exception):
    """An actionable source, dependency, or conversion problem."""


def run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise ConversionError(f"Missing executable: {command[0]}. Install FFmpeg (including ffprobe).") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()[-4000:]
        raise ConversionError(f"{Path(command[0]).name} failed: {detail or 'no diagnostic output'}") from exc


def _fraction(value: object) -> Fraction | None:
    if value in (None, "N/A", "0/0"):
        return None
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None


def _micros(seconds: Fraction) -> int:
    # Preserve timestamps to the manifest's nearest microsecond, without floats.
    return round(seconds * 1_000_000)


def probe_video(source: Path, ffprobe: str = "ffprobe") -> tuple[dict, Fraction, bool]:
    stream_result = run([
        ffprobe, "-v", "error", "-show_streams", "-of", "json", str(source),
    ])
    try:
        streams = json.loads(stream_result.stdout)["streams"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ConversionError("ffprobe returned invalid stream metadata.") from exc
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        raise ConversionError("The input contains no video stream.")
    stream = videos[0]
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise ConversionError(f"Video dimensions must be between 1 and {MAX_DIMENSION} pixels per side; got {width}x{height}.")
    time_base = _fraction(stream.get("time_base"))
    result = run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp,best_effort_timestamp_time,pts_time,duration,pkt_duration,duration_time,pkt_duration_time,width,height",
        "-of", "json", str(source),
    ])
    try:
        frames = json.loads(result.stdout)["frames"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ConversionError("ffprobe returned invalid frame metadata.") from exc
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ConversionError(f"Video must contain 1 to {MAX_FRAMES:,} frames; found {len(frames):,}.")
    timestamps: list[Fraction] = []
    for index, frame in enumerate(frames):
        if (int(frame.get("width", width)), int(frame.get("height", height))) != (width, height):
            raise ConversionError(f"Frame {index} changes dimensions; variable-resolution video is not supported.")
        ticks = _fraction(frame.get("best_effort_timestamp"))
        stamp = ticks * time_base if ticks is not None and time_base is not None else _fraction(frame.get("best_effort_timestamp_time", frame.get("pts_time")))
        if stamp is None:
            raise ConversionError(f"Frame {index} has no usable presentation timestamp.")
        if timestamps and stamp <= timestamps[-1]:
            raise ConversionError(f"Frame {index} has a duplicate or decreasing presentation timestamp.")
        timestamps.append(stamp)
    start = timestamps[0]
    relative = [_micros(t - start) for t in timestamps]
    if any(b <= a for a, b in zip(relative, relative[1:])):
        raise ConversionError("Frame timestamps are closer than the supported microsecond precision.")
    last = frames[-1]
    duration_ticks = _fraction(last.get("duration", last.get("pkt_duration")))
    last_duration = duration_ticks * time_base if duration_ticks is not None and time_base is not None else _fraction(last.get("duration_time", last.get("pkt_duration_time")))
    if last_duration is None or last_duration <= 0:
        if len(timestamps) > 1:
            last_duration = timestamps[-1] - timestamps[-2]
        else:
            rate = _fraction(stream.get("avg_frame_rate")) or _fraction(stream.get("r_frame_rate"))
            if rate is None or rate <= 0:
                raise ConversionError("Cannot determine the duration of the single video frame.")
            last_duration = 1 / rate
    duration = _micros(timestamps[-1] - start + last_duration)
    if duration <= relative[-1] or duration > MAX_DURATION_MICROS:
        raise ConversionError("Video duration must be positive and no longer than six hours.")
    manifest = {
        "formatVersion": 1, "width": width, "height": height,
        "frameCount": len(frames), "durationMicros": duration,
        "frameTimestampsMicros": relative, "audio": None, "source": source.name,
    }
    return manifest, start, any(s.get("codec_type") == "audio" for s in streams)


def _validate_png(path: Path, width: int, height: int) -> None:
    with path.open("rb") as handle:
        header = handle.read(29)
    if (len(header) != 29 or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[12:16] != b"IHDR"
            or struct.unpack(">II", header[16:24]) != (width, height)
            or header[24:26] != bytes((8, 2))):
        raise ConversionError(f"Unexpected frame image format or dimensions: {path.name}.")


def prepare_video(source: Path, output: Path, *, force: bool = False,
                  source_url: str | None = None, ffmpeg: str = "ffmpeg",
                  ffprobe: str = "ffprobe") -> dict:
    """Atomically write a .bapple ZIP; existing output survives any failed job."""
    source, output = Path(source).resolve(), Path(output).absolute()
    if not source.is_file():
        raise ConversionError(f"Input video does not exist or is not a file: {source}")
    if source == output.resolve() or (output.exists() and os.path.samefile(source, output)):
        raise ConversionError("The output must not overwrite the input video.")
    if output.exists() and not force:
        raise ConversionError(f"Output already exists: {output}. Use --force to replace it after a successful conversion.")
    if output.exists() and not output.is_file():
        raise ConversionError(f"Output is not a regular file: {output}")
    manifest, video_start, has_audio = probe_video(source, ffprobe)
    sample_count = math.ceil(Fraction(manifest["durationMicros"] * AUDIO_RATE, 1_000_000))
    if has_audio and sample_count * 4 + 4096 > MAX_AUDIO_BYTES:
        raise ConversionError("Audio exceeds the player's 256 MiB limit (about 23 minutes 18 seconds at 48 kHz stereo). Use a shorter source.")
    if source_url:
        manifest["sourceUrl"] = source_url
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bapple-", dir=output.parent) as folder:
        temp = Path(folder)
        frames_dir = temp / "frames"
        frames_dir.mkdir()
        run([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
            "-noautorotate", "-i", str(source), "-map", "0:v:0", "-an", "-sn",
            "-vsync", "0", "-c:v", "png", "-pix_fmt", "rgb24", "-threads", "1",
            "-start_number", "0", str(frames_dir / "%06d.png"),
        ])
        frame_paths = sorted(frames_dir.glob("*.png"))
        if len(frame_paths) != manifest["frameCount"]:
            raise ConversionError(f"Decoded {len(frame_paths)} frames, but the source reports {manifest['frameCount']}.")
        for index, path in enumerate(frame_paths):
            if path.name != f"{index:06d}.png":
                raise ConversionError("Decoded frame numbering is not contiguous.")
            _validate_png(path, manifest["width"], manifest["height"])
        if has_audio:
            start_expr = f"({video_start.numerator}/{video_start.denominator})"
            # copyts keeps the input audio clock. first_pts=0 pads late audio and
            # trims audio before the first video frame, including negative PTS.
            audio_filter = (
                f"asetpts=PTS-{start_expr}/TB,"
                f"aresample={AUDIO_RATE}:async=1:first_pts=0,"
                f"apad,atrim=end_sample={sample_count},asetpts=N/SR/TB"
            )
            run([
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
                "-copyts", "-i", str(source), "-map", "0:a:0", "-vn", "-sn",
                "-af", audio_filter, "-ar", str(AUDIO_RATE), "-ac", "2",
                "-c:a", "pcm_s16le", str(temp / "audio.wav"),
            ])
            with wave.open(str(temp / "audio.wav"), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) != (2, 2, AUDIO_RATE, sample_count):
                    raise ConversionError("Decoded audio does not match the required format or video duration.")
            manifest["audio"] = "audio.wav"
        archive = temp / "result.bapple"
        with zipfile.ZipFile(archive, "w", allowZip64=True) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", compress_type=zipfile.ZIP_DEFLATED)
            for path in frame_paths:
                bundle.write(path, f"frames/{path.name}", compress_type=zipfile.ZIP_STORED)
            if has_audio:
                bundle.write(temp / "audio.wav", "audio.wav", compress_type=zipfile.ZIP_DEFLATED)
        # A concurrent creator is also protected when --force was not requested.
        if force:
            os.replace(archive, output)
        else:
            try:
                os.link(archive, output)
            except FileExistsError as exc:
                raise ConversionError(f"Output was created during conversion; preserved existing file: {output}") from exc
    return manifest


def _downloader() -> list[str] | None:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python tools/prepare_video.py 'Bad Apple.mp4' --output bad_apple.bapple\n  python tools/prepare_video.py --check\n  python tools/prepare_video.py --download-reference --output bad_apple.bapple\n\nThe download option uses the exact linked reference URL. Downloads require yt-dlp.\nLimits: 4096 pixels per side, 500,000 frames, six hours without audio;\n256 MiB audio (about 23 minutes 18 seconds at 48 kHz stereo).\nFull-resolution frames can require several gigabytes of temporary disk space.")
    parser.add_argument("video", nargs="?", type=Path, help="local source video (first video and audio streams)")
    parser.add_argument("--output", "-o", type=Path, help="destination .bapple archive (defaults to the source filename)")
    parser.add_argument("--force", action="store_true", help="replace an existing output only after successful conversion")
    parser.add_argument("--download-reference", action="store_true", help=f"download {REFERENCE_URL} using yt-dlp, then convert")
    parser.add_argument("--check", action="store_true", help="check prerequisites and exit")
    args = parser.parse_args(argv)
    if args.check:
        missing = []
        for executable in ("ffmpeg", "ffprobe"):
            found = shutil.which(executable)
            print(f"{executable}: {found or 'MISSING — install FFmpeg'}")
            if found is None:
                missing.append(executable)
        print(f"yt-dlp (optional download): {'available' if _downloader() else 'not installed; python -m pip install yt-dlp'}")
        return 1 if missing else 0
    if bool(args.video) == bool(args.download_reference):
        parser.error("provide a local video or --download-reference")
    try:
        if args.download_reference:
            downloader = _downloader()
            if downloader is None:
                raise ConversionError("Reference downloads require yt-dlp. Install it with: python -m pip install yt-dlp")
            output = args.output or Path("bad_apple.bapple")
            with tempfile.TemporaryDirectory(prefix="bad-apple-source-") as folder:
                print("Downloading the linked reference…", file=sys.stderr)
                result = run(downloader + ["--no-playlist", "--format", "bv*+ba/b", "--merge-output-format", "mkv",
                    "--output", str(Path(folder) / "reference.%(ext)s"), "--print", "after_move:filepath", REFERENCE_URL])
                paths = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
                if not paths or not Path(paths[-1]).is_file():
                    raise ConversionError("yt-dlp did not return a usable downloaded video path.")
                manifest = prepare_video(Path(paths[-1]), output, force=args.force, source_url=REFERENCE_URL)
        else:
            output = args.output or args.video.with_suffix(".bapple")
            manifest = prepare_video(args.video, output, force=args.force)
        print(f"Created {output.resolve()} — {manifest['width']}×{manifest['height']}, {manifest['frameCount']:,} frames, "
              f"{manifest['durationMicros'] / 1_000_000:.3f}s, {'synchronized audio' if manifest['audio'] else 'no audio stream'}.")
        return 0
    except (ConversionError, OSError, wave.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
