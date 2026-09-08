#!/usr/bin/env python3
"""Generate an original, deterministic audiovisual verification fixture.

This is a diagnostic pattern, NOT Bad Apple or a replacement for its reference.
Lossless FFV1 RGB video is streamed to FFmpeg without raw intermediate
files. Audio is a 440 Hz tone, signed 16-bit stereo PCM at 48 kHz.

Pixel layout (zero-based coordinates, no fonts required):
  * Fixed corner patches are pure RGB red (top left), green (top right), blue
    (bottom left), and yellow (bottom right). Each is width//4 by height//4,
    flush with the image edges: 120 x 90 pixels at the default 480 x 360.
  * The grayscale background quadrants are 32 (top left), 96 (top right), 160
    (bottom left), and 224 (bottom right).
  * The central horizontal band ramps from exactly 0 to 255, left to right.
  * A white square traverses the upper center, one horizontal pixel per frame;
    a black one-pixel border around it makes its edges visible.
  * A 32-cell binary frame counter occupies the band beginning at 5/8 height.
    Cells run most-significant bit first, left to right: black=0, white=1.
    Frame numbering starts at zero. Each cell has a mid-gray one-pixel gutter.
    Cell i occupies [i*width//32, (i+1)*width//32), excluding that gutter.

The decoded video and audio are deterministic. Matroska container identifiers
may differ between FFmpeg versions. At 30 FPS, Matroska's millisecond time base
represents timestamps as the usual 0, 33, 67, 100, ... milliseconds; frames are
not duplicated or dropped. The requested duration must be an integral number
of both video frames and 48 kHz audio samples.

Examples:
  python tools/generate_fixture.py --output /tmp/fixture.mkv
  python tools/generate_fixture.py --output /tmp/stress.mkv --duration 219
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def duration_seconds(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds") from exc
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def rectangle(
    pixels: bytearray, width: int, x: int, y: int, w: int, h: int,
    value: int | tuple[int, int, int]
) -> None:
    if isinstance(value, int):
        color = bytes([value, value, value, 0])
    else:
        red, green, blue = value
        color = bytes([blue, green, red, 0])
    row = color * w
    for line in range(y, y + h):
        start = (line * width + x) * 4
        pixels[start : start + w * 4] = row


def frame_template(width: int, height: int) -> bytearray:
    half = width // 2
    top = bytes([32, 32, 32, 0]) * half + bytes([96, 96, 96, 0]) * (width - half)
    bottom = bytes([160, 160, 160, 0]) * half + bytes([224, 224, 224, 0]) * (width - half)
    pixels = bytearray(top * (height // 2) + bottom * (height - height // 2))
    gradient = b"".join(bytes([v, v, v, 0]) for v in (x * 255 // (width - 1) for x in range(width)))
    band_height = max(2, height // 12)
    band_y = height // 2 - band_height // 2
    for y in range(band_y, band_y + band_height):
        pixels[y * width * 4 : (y + 1) * width * 4] = gradient
    patch_width, patch_height = width // 4, height // 4
    rectangle(pixels, width, 0, 0, patch_width, patch_height, (255, 0, 0))
    rectangle(pixels, width, width - patch_width, 0, patch_width, patch_height, (0, 255, 0))
    rectangle(pixels, width, 0, height - patch_height, patch_width, patch_height, (0, 0, 255))
    rectangle(pixels, width, width - patch_width, height - patch_height, patch_width, patch_height, (255, 255, 0))
    return pixels


def make_frame(template: bytearray, width: int, height: int, index: int) -> bytearray:
    pixels = template.copy()
    square = max(4, min(width, height) // 12)
    motion_width = width // 2 - square - 2
    x = width // 4 + 1 + index % motion_width
    motion_height = max(1, height // 8 - square - 2)
    y = height // 4 + 1 + (index // motion_width) % motion_height
    rectangle(pixels, width, x, y, square, square, 0)
    rectangle(pixels, width, x + 1, y + 1, square - 2, square - 2, 255)

    counter_y = 5 * height // 8
    counter_height = max(2, height // 10)
    rectangle(pixels, width, 0, counter_y, width, counter_height, 128)
    for cell in range(32):
        start = cell * width // 32
        end = (cell + 1) * width // 32
        value = 255 if index & (1 << (31 - cell)) else 0
        rectangle(pixels, width, start, counter_y, end - start - 1, counter_height, value)

    return pixels


def generate(args: argparse.Namespace) -> tuple[int, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required on PATH")
    if args.width < 64 or args.height < 48:
        raise ValueError("fixture dimensions must be at least 64 x 48")
    if args.width * args.height > 33_177_600:
        raise ValueError("fixture dimensions may not exceed 33,177,600 pixels")
    if args.fps > 1000:
        raise ValueError("fps must be at most 1000 for Matroska timestamps")
    frame_count_decimal = args.duration * args.fps
    sample_count_decimal = args.duration * 48000
    if frame_count_decimal != frame_count_decimal.to_integral_value():
        raise ValueError("duration multiplied by fps must be an integer number of frames")
    if sample_count_decimal != sample_count_decimal.to_integral_value():
        raise ValueError("duration must correspond to an integer number of 48 kHz samples")
    frame_count = int(frame_count_decimal)
    if not 1 <= frame_count <= 2**32:
        raise ValueError("duration must produce between 1 and 2^32 video frames")
    output = args.output.expanduser().absolute()
    if output.exists() and not args.force:
        raise ValueError(f"output already exists: {output} (use --force to replace it)")
    if output.is_dir():
        raise ValueError(f"output is a directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    # A sibling temporary directory guarantees a same-filesystem atomic publish.
    # FFmpeg diagnostics go to a file so a full stderr pipe cannot deadlock stdin.
    with tempfile.TemporaryDirectory(prefix=".fixture-", dir=output.parent) as temporary:
        temp = Path(temporary)
        encoded = temp / "fixture.mkv"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "rawvideo", "-pixel_format", "bgr0",
            "-video_size", f"{args.width}x{args.height}",
            "-framerate", str(args.fps), "-i", "pipe:0",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgr0",
            "-threads:v", "1", "-fps_mode", "passthrough",
            "-af", f"atrim=end_sample={int(sample_count_decimal)},asetpts=PTS-STARTPTS",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            "-map_metadata", "-1", "-fflags", "+bitexact",
            "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-metadata", "title=Original diagnostic fixture (not Bad Apple)",
            "-f", "matroska", str(encoded),
        ]
        with (temp / "ffmpeg.log").open("w+b") as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
            assert process.stdin is not None
            broken_pipe = False
            try:
                template = frame_template(args.width, args.height)
                for index in range(frame_count):
                    process.stdin.write(make_frame(template, args.width, args.height, index))
            except BrokenPipeError:
                broken_pipe = True
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    broken_pipe = True
            return_code = process.wait()
            if return_code or broken_pipe:
                log.seek(0)
                diagnostics = log.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"FFmpeg failed ({return_code}): {diagnostics}")
        if args.force:
            os.replace(encoded, output)
        else:
            # Unlike a check followed by replace, link refuses a racing creator.
            os.link(encoded, output)
    return frame_count, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True, help="destination Matroska file (.mkv)")
    parser.add_argument("--duration", type=duration_seconds, default=Decimal("6"), help="seconds (default: 6; full-length stress: 219)")
    parser.add_argument("--width", type=positive_integer, default=480)
    parser.add_argument("--height", type=positive_integer, default=360)
    parser.add_argument("--fps", type=positive_integer, default=30)
    parser.add_argument("--force", action="store_true", help="atomically replace an existing output after success")
    args = parser.parse_args()
    try:
        count, output = generate(args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled; destination preserved", file=sys.stderr)
        return 130
    print(f"Created {output}: {args.width}x{args.height}, {count} frames at {args.fps} FPS, {args.duration}s, stereo PCM 48 kHz.")
    print("Original diagnostic pattern only; this is not the Bad Apple reference video.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
