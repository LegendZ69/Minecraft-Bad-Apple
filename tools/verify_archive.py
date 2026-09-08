#!/usr/bin/env python3
"""Verify a .bapple archive, optionally against EVERY decoded source frame.

Archive-only verification uses the Python standard library. --source additionally
requires ffmpeg and ffprobe. It checks all RGB pixels, all timestamps, and the
normalized PCM audio against that exact local file. A match does not establish
that a file is an official release or grant rights to distribute its content.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
import wave
import zipfile
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.prepare_video import (
    AUDIO_RATE, MAX_AUDIO_BYTES, MAX_DIMENSION, MAX_DURATION_MICROS,
    MAX_FRAMES, MAX_MANIFEST_BYTES, ConversionError, probe_video, sha256_file,
)


class VerificationError(Exception):
    """The archive or source comparison failed a verification check."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def integer(value: object, minimum: int, maximum: int, name: str) -> int:
    require(type(value) is int and minimum <= value <= maximum, f"Invalid {name}: {value!r}.")
    return value


def validate_png(data: bytes, width: int, height: int, name: str) -> None:
    """Validate every PNG chunk CRC, RGB24 dimensions and scanline payload."""
    require(data[:8] == b"\x89PNG\r\n\x1a\n", f"{name}: invalid PNG signature.")
    offset, chunks, compressed = 8, [], bytearray()
    ended_idat = False
    while offset < len(data):
        require(len(data) - offset >= 12, f"{name}: truncated PNG chunk.")
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        require(end <= len(data), f"{name}: truncated PNG chunk payload.")
        payload = data[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        require(zlib.crc32(kind + payload) & 0xFFFFFFFF == crc, f"{name}: PNG CRC mismatch in {kind!r}.")
        require(all(65 <= ch <= 90 or 97 <= ch <= 122 for ch in kind), f"{name}: invalid chunk type.")
        if not chunks:
            require(kind == b"IHDR" and size == 13, f"{name}: missing initial IHDR.")
        if kind == b"IHDR":
            require(not chunks and size == 13, f"{name}: duplicate or malformed IHDR.")
            require(struct.unpack(">IIBBBBB", payload) == (width, height, 8, 2, 0, 0, 0),
                    f"{name}: expected noninterlaced RGB24 PNG at {width}x{height}.")
        elif kind == b"IDAT":
            require(not ended_idat, f"{name}: non-contiguous IDAT chunks.")
            compressed.extend(payload)
        elif kind == b"IEND":
            require(size == 0 and end == len(data), f"{name}: malformed IEND or trailing bytes.")
        elif not (kind[0] & 32):
            require(kind == b"PLTE", f"{name}: unsupported critical PNG chunk {kind!r}.")
        if b"IDAT" in chunks and kind != b"IDAT":
            ended_idat = True
        chunks.append(kind)
        offset = end
    require(chunks and chunks[-1] == b"IEND" and b"IDAT" in chunks, f"{name}: incomplete PNG.")
    expected_size = (width * 3 + 1) * height
    decoder = zlib.decompressobj()
    try:
        scanlines = decoder.decompress(compressed, expected_size + 1)
    except zlib.error as exc:
        raise VerificationError(f"{name}: invalid compressed pixel payload.") from exc
    require(len(scanlines) == expected_size and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
            f"{name}: invalid or oversized PNG scanline payload.")
    require(all(scanlines[row * (width * 3 + 1)] <= 4 for row in range(height)),
            f"{name}: invalid PNG row filter.")


def _read_exact(handle, count: int) -> bytes:
    parts = []
    while count:
        part = handle.read(count)
        if not part:
            break
        parts.append(part)
        count -= len(part)
    return b"".join(parts)


def _start_decoder(stack: ExitStack, command: list[str]):
    errors = stack.enter_context(tempfile.TemporaryFile())
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
    except FileNotFoundError as exc:
        raise VerificationError(f"Missing executable: {command[0]}.") from exc
    def cleanup():
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
    stack.callback(cleanup)
    return process, errors


def _finish_decoder(process, errors, label: str):
    code = process.wait()
    if code:
        errors.seek(0)
        detail = errors.read().decode("utf-8", errors="replace")[-3000:]
        raise VerificationError(f"{label} decoder failed ({code}): {detail}")


def compare_source(source: Path, frames_dir: Path, manifest: dict, audio_pcm: bytes | None,
                   *, ffmpeg: str, ffprobe: str) -> dict:
    started = time.perf_counter()
    source_manifest, video_start, source_has_audio = probe_video(source, ffprobe)
    for field in ("width", "height", "frameCount", "durationMicros", "frameTimestampsMicros"):
        require(source_manifest[field] == manifest[field], f"Source {field} does not match the archive.")
    require(source_has_audio == (audio_pcm is not None), "Source and archive audio presence differ.")
    source_digest = sha256_file(source)
    recorded_digest = manifest.get("sourceSha256")
    if recorded_digest is not None:
        require(source_digest == recorded_digest, "Source SHA-256 does not match the recorded sourceSha256.")
    common = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-noautorotate"]
    output = ["-map", "0:v:0", "-an", "-sn", "-vsync", "0", "-pix_fmt", "rgb24", "-threads", "1", "-f", "rawvideo", "pipe:1"]
    rgb_digest = hashlib.sha256()
    frame_bytes = manifest["width"] * manifest["height"] * 3
    with ExitStack() as stack:
        source_process, source_errors = _start_decoder(stack, common + ["-i", str(source)] + output)
        archive_process, archive_errors = _start_decoder(stack, common + ["-framerate", "1", "-i", str(frames_dir / "%06d.png")] + output)
        for index in range(manifest["frameCount"]):
            source_frame = _read_exact(source_process.stdout, frame_bytes)
            archive_frame = _read_exact(archive_process.stdout, frame_bytes)
            require(len(source_frame) == frame_bytes, f"Source decode ended before complete frame {index}.")
            require(len(archive_frame) == frame_bytes, f"Archive decode ended before complete frame {index}.")
            require(source_frame == archive_frame, f"Decoded RGB pixels differ at frame {index}.")
            rgb_digest.update(source_frame)
        require(not source_process.stdout.read(1), "Source decoder produced extra frames.")
        require(not archive_process.stdout.read(1), "Archive decoder produced extra frames.")
        _finish_decoder(source_process, source_errors, "Source video")
        _finish_decoder(archive_process, archive_errors, "Archive video")
    audio_equal = None
    if audio_pcm is not None:
        sample_count = math.ceil(Fraction(manifest["durationMicros"] * AUDIO_RATE, 1_000_000))
        start_expr = f"({video_start.numerator}/{video_start.denominator})"
        audio_filter = (f"asetpts=PTS-{start_expr}/TB,aresample={AUDIO_RATE}:async=1:first_pts=0,"
                        f"apad,atrim=end_sample={sample_count},asetpts=N/SR/TB")
        command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-copyts", "-i", str(source),
                   "-map", "0:a:0", "-vn", "-sn", "-af", audio_filter, "-ar", str(AUDIO_RATE), "-ac", "2",
                   "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]
        with ExitStack() as stack:
            process, errors = _start_decoder(stack, command)
            for offset in range(0, len(audio_pcm), 1024 * 1024):
                expected = audio_pcm[offset:offset + 1024 * 1024]
                require(_read_exact(process.stdout, len(expected)) == expected,
                        f"Normalized source PCM differs at or after byte {offset}.")
            require(not process.stdout.read(1), "Source audio has unexpected extra PCM samples.")
            _finish_decoder(process, errors, "Source audio")
        audio_equal = True
    return {
        "status": "passed", "sourceFile": source.name, "sourceSha256": source_digest,
        "sourceSha256MatchesManifest": source_digest == recorded_digest if recorded_digest else None,
        "framesCompared": manifest["frameCount"], "rgbBytesCompared": frame_bytes * manifest["frameCount"],
        "allDecodedRgbFramesEqual": True, "allRelativeTimestampsEqual": True, "durationEqual": True,
        "rgbFrameSequenceSha256": rgb_digest.hexdigest(), "normalizedPcmEqual": audio_equal,
        "elapsedSeconds": round(time.perf_counter() - started, 6),
        "comparisonDefinition": "Every FFmpeg-decoded RGB24 pixel and relative presentation timestamp; audio compared after documented 48 kHz stereo signed-16-bit alignment, resampling, padding and trimming.",
    }


def verify_archive(archive: Path, *, source: Path | None = None,
                   ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> dict:
    started = time.perf_counter()
    archive = Path(archive).resolve()
    source = Path(source).resolve() if source is not None else None
    require(archive.is_file(), f"Archive does not exist: {archive}")
    if source is not None:
        require(source.is_file(), f"Source does not exist: {source}")
    try:
        with ExitStack() as stack:
            bundle = stack.enter_context(zipfile.ZipFile(archive))
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            require(len(names) == len(set(names)), "Archive contains duplicate entry names.")
            require("manifest.json" in names, "Archive is missing manifest.json.")
            info = bundle.getinfo("manifest.json")
            require(0 < info.file_size <= MAX_MANIFEST_BYTES, "Manifest exceeds the size limit or is empty.")
            manifest_bytes = bundle.read(info)
            manifest = json.loads(manifest_bytes)
            require(isinstance(manifest, dict), "Manifest must be a JSON object.")
            require(type(manifest.get("formatVersion")) is int and manifest["formatVersion"] == 1, "Unsupported formatVersion.")
            width = integer(manifest.get("width"), 1, MAX_DIMENSION, "width")
            height = integer(manifest.get("height"), 1, MAX_DIMENSION, "height")
            count = integer(manifest.get("frameCount"), 1, MAX_FRAMES, "frameCount")
            duration = integer(manifest.get("durationMicros"), 1, MAX_DURATION_MICROS, "durationMicros")
            timestamps = manifest.get("frameTimestampsMicros")
            require(isinstance(timestamps, list) and len(timestamps) == count, "Timestamp count does not match frameCount.")
            for stamp in timestamps:
                integer(stamp, 0, duration - 1, "frame timestamp")
            require(timestamps[0] == 0 and all(a < b for a, b in zip(timestamps, timestamps[1:])),
                    "Timestamps must start at zero and strictly increase below durationMicros.")
            audio_name = manifest.get("audio")
            require(audio_name in (None, "audio.wav"), "Audio must be audio.wav or null.")
            recorded_digest = manifest.get("sourceSha256")
            require(recorded_digest is None or isinstance(recorded_digest, str) and re.fullmatch(r"[0-9a-f]{64}", recorded_digest) is not None,
                    "Invalid sourceSha256.")
            expected = {"manifest.json"} | {f"frames/{index:06d}.png" for index in range(count)}
            if audio_name:
                expected.add(audio_name)
            require(set(names) == expected, "Archive entries do not exactly match the declared contiguous frame sequence and audio.")
            frames_dir = None
            if source is not None:
                frames_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="bapple-verify-")))
            frame_digest = hashlib.sha256()
            # PNG can add filter/chunk overhead but should never approach this
            # bound. The decompressed pixel payload is separately bounded.
            max_png_bytes = (width * 3 + 1) * height * 2 + 1024 * 1024
            for index in range(count):
                name = f"frames/{index:06d}.png"
                require(bundle.getinfo(name).file_size <= max_png_bytes, f"{name}: PNG entry exceeds the size limit.")
                data = bundle.read(name)  # zipfile verifies the ZIP entry CRC too.
                validate_png(data, width, height, name)
                frame_digest.update(struct.pack(">Q", len(data)))
                frame_digest.update(data)
                if frames_dir is not None:
                    (frames_dir / f"{index:06d}.png").write_bytes(data)
            audio_report, audio_pcm = None, None
            if audio_name:
                require(bundle.getinfo(audio_name).file_size <= MAX_AUDIO_BYTES, "Audio exceeds the size limit.")
                data = bundle.read(audio_name)
                with wave.open(io.BytesIO(data), "rb") as audio:
                    sample_count = math.ceil(Fraction(duration * AUDIO_RATE, 1_000_000))
                    require((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype(), audio.getnframes())
                            == (2, 2, AUDIO_RATE, "NONE", sample_count), "PCM format or sample count does not match the video duration.")
                    audio_pcm = audio.readframes(sample_count)
                    require(len(audio_pcm) == sample_count * 4, "Truncated PCM audio payload.")
                audio_report = {"channels": 2, "sampleRate": AUDIO_RATE, "bitsPerSample": 16,
                                "sampleFrames": sample_count, "durationSeconds": sample_count / AUDIO_RATE,
                                "sha256": hashlib.sha256(data).hexdigest(), "pcmSha256": hashlib.sha256(audio_pcm).hexdigest()}
            comparison = {"status": "not_requested", "framesCompared": 0,
                          "allDecodedRgbFramesEqual": None, "allRelativeTimestampsEqual": None, "normalizedPcmEqual": None}
            integrity_seconds = time.perf_counter() - started
            if source is not None:
                comparison = compare_source(source, frames_dir, manifest, audio_pcm, ffmpeg=ffmpeg, ffprobe=ffprobe)
            return {
                "reportVersion": 1, "ok": True, "archiveFile": archive.name,
                "archiveSha256": sha256_file(archive), "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "timings": {"archiveIntegritySeconds": round(integrity_seconds, 6),
                            "verificationSeconds": round(time.perf_counter() - started, 6)},
                "archiveChecks": {"zipEntryCrc": True, "exactFrameSequence": True, "timestamps": True,
                                  "allPngDimensionsAndChunkCrc": True, "allPngScanlinePayloads": True,
                                  "pcmFormatAndDuration": True if audio_name else None},
                "video": {"width": width, "height": height, "frameCount": count, "durationMicros": duration,
                          "pngSequenceSha256": frame_digest.hexdigest(),
                          "pngSequenceDigestDefinition": "SHA-256 of each PNG prefixed with its 8-byte big-endian length, in frame order"},
                "audio": audio_report, "sourceComparison": comparison,
                "provenance": {"recordedSource": manifest.get("source"), "recordedSourceUrl": manifest.get("sourceUrl"),
                               "recordedSourceSha256": recorded_digest, "recordedSourceMetadata": manifest.get("sourceMetadata"),
                               "officialSourceIdentityVerified": False,
                               "note": "Recorded metadata is an unverified provenance claim. This report verifies technical integrity and optional local-source equivalence, not official origin or redistribution rights."},
            }
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zlib.error,
            zipfile.BadZipFile, wave.Error, EOFError, ConversionError) as exc:
        raise VerificationError(f"Verification failed: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--source", type=Path, help="compare EVERY decoded RGB frame, relative timestamp and aligned PCM sample")
    parser.add_argument("--report", type=Path, help="also atomically write the JSON verification report")
    args = parser.parse_args(argv)
    if args.report and args.report.resolve() in {args.archive.resolve(), args.source.resolve() if args.source else None}:
        parser.error("the report must not overwrite the archive or source")
    try:
        report = verify_archive(args.archive, source=args.source)
    except VerificationError as exc:
        report = {"reportVersion": 1, "ok": False, "archiveFile": args.archive.name, "error": str(exc),
                  "sourceComparison": {"status": "failed_or_incomplete" if args.source else "not_requested"},
                  "provenance": {"officialSourceIdentityVerified": False}}
    serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=".verification-", dir=args.report.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(serialized)
            try:
                os.replace(temporary, args.report)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Cannot write verification report: {exc}", file=sys.stderr)
            return 1
    print(serialized, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
