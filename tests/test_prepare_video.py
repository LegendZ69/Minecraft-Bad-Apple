"""Integration tests using tiny, generated sources; no reference media required."""

import io
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import wave
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.prepare_video import (
    ConversionError, OFFICIAL_URL, ORIGINAL_URL, REFERENCE_URL, main,
    prepare_video, probe_video, sanitize_source_metadata,
)


class ProvenanceTests(unittest.TestCase):
    def test_metadata_allowlist_never_copies_private_downloader_fields(self):
        safe = sanitize_source_metadata({"id": "123", "title": "x" * 5000, "channel": "Creator",
            "duration": 219.0, "http_headers": {"Cookie": "secret"}, "url": "signed token",
            "cookies": "secret", "requested_formats": [{"url": "secret"}], "uploader": {"unexpected": 1},
            "upload_date": True, "extractor_key": float("nan")})
        self.assertEqual(safe, {"id": "123", "title": "x" * 2048, "channel": "Creator", "duration": 219.0})

    def test_explicit_download_options_use_exact_source_and_safe_metadata(self):
        for option, expected_url in (("--download-reference", REFERENCE_URL),
                                     ("--download-official", OFFICIAL_URL),
                                     ("--download-original", ORIGINAL_URL)):
            with self.subTest(option=option):
                def download(command):
                    self.assertEqual(command[-1], expected_url)
                    self.assertIn("--write-info-json", command)
                    path = Path(command[command.index("--output") + 1].replace("%(ext)s", "mkv"))
                    path.write_bytes(b"mock source")
                    path.with_suffix(".info.json").write_text(json.dumps({"channel": "Creator", "http_headers": {"Cookie": "secret"}}))
                    return subprocess.CompletedProcess(command, 0, stdout=(str(path) + "\n").encode())
                fake_manifest = {"width": 1, "height": 1, "frameCount": 1, "durationMicros": 1_000_000, "audio": None}
                with mock.patch("tools.prepare_video._downloader", return_value=["yt-dlp"]), \
                     mock.patch("tools.prepare_video.run", side_effect=download), \
                     mock.patch("tools.prepare_video.prepare_video", return_value=fake_manifest) as convert, \
                     mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO):
                    self.assertEqual(main([option]), 0)
                    self.assertEqual(convert.call_args.kwargs["source_url"], expected_url)
                    self.assertEqual(convert.call_args.kwargs["source_metadata"]["channel"], "Creator")

    def test_download_selection_is_exclusive(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                main(["--download-official", "--download-reference"])
            with self.assertRaises(SystemExit):
                main(["source.mkv", "--download-official"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class PrepareVideoTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="bad apple test ")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.width, self.height, self.count = 16, 12, 6
        self.pixels = bytes(
            (frame * 31 + x * 7 + y * 11 + channel * 71) % 256
            for frame in range(self.count)
            for y in range(self.height)
            for x in range(self.width)
            for channel in range(3)
        )
        (self.root / "source pixels.rgb").write_bytes(self.pixels)

    def ffmpeg(self, *args):
        return subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                              check=True, capture_output=True).stdout

    def make_source(self, *, audio=False, video_offset=0, audio_offset=0, variable=False):
        args = []
        if video_offset:
            args += ["-itsoffset", str(video_offset)]
        args += ["-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{self.width}x{self.height}",
                 "-framerate", "4", "-i", self.root / "source pixels.rgb"]
        if audio:
            with wave.open(str(self.root / "source audio.wav"), "wb") as wav:
                wav.setparams((2, 2, 48_000, 0, "NONE", "not compressed"))
                # A deterministic step reveals padding and trimming errors.
                wav.writeframes(b"".join(struct.pack("<hh", value, value)
                    for value in ([5000] * 12_000 + [10_000] * 84_000)))
            if audio_offset:
                args += ["-itsoffset", str(audio_offset)]
            args += ["-i", self.root / "source audio.wav", "-map", "0:v:0", "-map", "1:a:0", "-c:a", "pcm_s16le"]
        if variable:
            args += ["-vf", "settb=1/1000,setpts=N*N*100", "-fps_mode", "passthrough", "-enc_time_base", "1/1000"]
        args += ["-c:v", "ffv1", "-pix_fmt", "bgr0", self.root / "source clip.mkv"]
        self.ffmpeg(*args)
        return self.root / "source clip.mkv"

    def load_audio(self, output):
        with zipfile.ZipFile(output) as bundle:
            with wave.open(io.BytesIO(bundle.read("audio.wav")), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (2, 2, 48_000))
                count = audio.getnframes()
                samples = struct.unpack(f"<{count * 2}h", audio.readframes(count))
                return count, samples[::2]

    def test_exact_pixels_timestamps_dimensions_and_audio_duration(self):
        source = self.make_source(audio=True)
        output = self.root / "output movie.bapple"
        manifest = prepare_video(source, output, source_url="https://example.invalid/source",
                                 source_metadata={"channel": "Test creator", "http_headers": {"Cookie": "secret"}})
        self.assertEqual((manifest["width"], manifest["height"], manifest["frameCount"]), (16, 12, 6))
        self.assertEqual(manifest["frameTimestampsMicros"], [0, 250_000, 500_000, 750_000, 1_000_000, 1_250_000])
        self.assertEqual(manifest["durationMicros"], 1_500_000)
        self.assertEqual(manifest["audio"], "audio.wav")
        with zipfile.ZipFile(output) as bundle:
            self.assertEqual(json.loads(bundle.read("manifest.json")), manifest)
            self.assertEqual(manifest["source"], "source clip.mkv")
            self.assertEqual(manifest["sourceUrl"], "https://example.invalid/source")
            self.assertEqual(manifest["sourceSha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(manifest["sourceMetadata"], {"channel": "Test creator"})
            for index in range(self.count):
                self.assertEqual(bundle.getinfo(f"frames/{index:06d}.png").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(bundle.getinfo("audio.wav").compress_type, zipfile.ZIP_DEFLATED)
            bundle.extractall(self.root / "unpacked")
        decoded = self.ffmpeg("-framerate", "4", "-i", self.root / "unpacked/frames/%06d.png",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1")
        self.assertEqual(decoded, self.pixels)
        count, _ = self.load_audio(output)
        self.assertEqual(count, 72_000)

    def test_variable_frame_timestamps_are_preserved(self):
        source = self.make_source(variable=True)
        manifest = prepare_video(source, self.root / "variable.bapple")
        self.assertEqual(manifest["frameTimestampsMicros"], [0, 100_000, 400_000, 900_000, 1_600_000, 2_500_000])
        self.assertEqual(manifest["durationMicros"], 2_750_000)
        self.assertEqual(manifest["frameCount"], self.count)
        self.assertIsNone(manifest["audio"])

    def test_audio_start_after_video_is_padded(self):
        source = self.make_source(audio=True, audio_offset=0.25)
        output = self.root / "late audio.bapple"
        prepare_video(source, output)
        count, samples = self.load_audio(output)
        self.assertEqual(count, 72_000)
        self.assertTrue(all(value == 0 for value in samples[:11_900]))
        self.assertEqual(samples[12_100], 5000)
        self.assertEqual(samples[24_100], 10_000)

    def test_audio_start_before_video_is_trimmed(self):
        source = self.make_source(audio=True, video_offset=0.25)
        output = self.root / "early audio.bapple"
        manifest = prepare_video(source, output)
        self.assertEqual(manifest["frameTimestampsMicros"][0], 0)
        count, samples = self.load_audio(output)
        self.assertEqual(count, 72_000)
        self.assertEqual(samples[100], 10_000)
        self.assertEqual(samples[-100], 10_000)

    def test_missing_or_corrupt_input_preserves_output(self):
        output = self.root / "existing.bapple"
        output.write_bytes(b"keep existing output")
        with self.assertRaisesRegex(ConversionError, "does not exist"):
            prepare_video(self.root / "missing.mp4", output, force=True)
        corrupt = self.root / "corrupt.mp4"
        corrupt.write_bytes(b"not a video")
        with self.assertRaises(ConversionError):
            prepare_video(corrupt, output, force=True)
        self.assertEqual(output.read_bytes(), b"keep existing output")
        self.assertEqual(list(self.root.glob(".bapple-*")), [])

    def test_failed_extraction_preserves_output_and_cleans_temporary_files(self):
        source = self.make_source()
        output = self.root / "existing.bapple"
        output.write_bytes(b"keep existing output")
        with self.assertRaisesRegex(ConversionError, "Missing executable"):
            prepare_video(source, output, force=True, ffmpeg="nonexistent-ffmpeg-for-test")
        self.assertEqual(output.read_bytes(), b"keep existing output")
        self.assertEqual(list(self.root.glob(".bapple-*")), [])

    def test_overwrite_requires_force_and_source_cannot_be_overwritten(self):
        source = self.make_source()
        output = self.root / "existing.bapple"
        output.write_bytes(b"original output")
        with self.assertRaisesRegex(ConversionError, "already exists"):
            prepare_video(source, output)
        with self.assertRaisesRegex(ConversionError, "must not overwrite the input"):
            prepare_video(source, source, force=True)
        prepare_video(source, output, force=True)
        self.assertTrue(zipfile.is_zipfile(output))

    def test_dimension_and_duration_limits_are_enforced(self):
        source = self.make_source()
        with mock.patch("tools.prepare_video.MAX_DIMENSION", 8):
            with self.assertRaisesRegex(ConversionError, "dimensions"):
                probe_video(source)
        with mock.patch("tools.prepare_video.MAX_DURATION_MICROS", 100_000):
            with self.assertRaisesRegex(ConversionError, "duration"):
                probe_video(source)
        with mock.patch("tools.prepare_video.MAX_FRAMES", 2):
            with self.assertRaisesRegex(ConversionError, "frames"):
                probe_video(source)

    def test_oversized_audio_is_rejected_before_creating_output(self):
        source = self.make_source(audio=True)
        output = self.root / "too much audio.bapple"
        with mock.patch("tools.prepare_video.MAX_AUDIO_BYTES", 4096):
            with self.assertRaisesRegex(ConversionError, "256 MiB"):
                prepare_video(source, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".bapple-*")), [])


if __name__ == "__main__":
    unittest.main()
