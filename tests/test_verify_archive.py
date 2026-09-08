"""Archive corruption and exhaustive local-source verification tests."""

import io
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
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.prepare_video import prepare_video
from tools.verify_archive import VerificationError, main, validate_png, verify_archive


def png(width, height, value=128):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([value]) * width * 3) * height)) + chunk(b"IEND", b""))


def pcm(sample_count):
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00" * sample_count * 4)
    return result.getvalue()


def write_archive(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


class OfflineVerificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="archive-verification-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.archive = self.root / "test.bapple"
        self.manifest = {"formatVersion": 1, "width": 2, "height": 2, "frameCount": 2,
                         "durationMicros": 100_000, "frameTimestampsMicros": [0, 50_000], "audio": "audio.wav"}
        self.entries = {"frames/000000.png": png(2, 2), "frames/000001.png": png(2, 2, 255), "audio.wav": pcm(4800)}

    def save(self):
        self.entries["manifest.json"] = json.dumps(self.manifest).encode()
        write_archive(self.archive, self.entries)

    def test_complete_offline_report_does_not_claim_source_equivalence(self):
        self.save()
        result = verify_archive(self.archive)
        self.assertTrue(result["ok"])
        self.assertEqual(result["video"]["frameCount"], 2)
        self.assertEqual(result["sourceComparison"]["status"], "not_requested")
        self.assertIsNone(result["sourceComparison"]["allDecodedRgbFramesEqual"])
        self.assertFalse(result["provenance"]["officialSourceIdentityVerified"])
        self.assertEqual(result["audio"]["sampleFrames"], 4800)
        self.assertEqual(len(result["archiveSha256"]), 64)

    def test_missing_frame_and_undeclared_file_rejected(self):
        del self.entries["frames/000001.png"]
        self.save()
        with self.assertRaisesRegex(VerificationError, "entries"):
            verify_archive(self.archive)
        self.entries["frames/000001.png"] = png(2, 2)
        self.entries["../../escape"] = b"unsafe"
        self.save()
        with self.assertRaisesRegex(VerificationError, "entries"):
            verify_archive(self.archive)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_bad_png_chunk_crc_rejected_even_with_valid_zip_crc(self):
        data = bytearray(self.entries["frames/000000.png"])
        data[29] ^= 1
        self.entries["frames/000000.png"] = bytes(data)
        self.save()
        with self.assertRaisesRegex(VerificationError, "PNG CRC"):
            verify_archive(self.archive)

    def test_dimensions_and_timestamps_checked(self):
        self.entries["frames/000001.png"] = png(3, 2)
        self.save()
        with self.assertRaisesRegex(VerificationError, "RGB24 PNG"):
            verify_archive(self.archive)
        self.entries["frames/000001.png"] = png(2, 2)
        for timestamps in ([0, 0], [1, 50_000], [0], [0, True], [0, 100_000]):
            with self.subTest(timestamps=timestamps):
                self.manifest["frameTimestampsMicros"] = timestamps
                self.save()
                with self.assertRaises(VerificationError):
                    verify_archive(self.archive)

    def test_audio_sample_count_and_truncation_rejected(self):
        self.entries["audio.wav"] = pcm(4799)
        self.save()
        with self.assertRaisesRegex(VerificationError, "PCM format"):
            verify_archive(self.archive)
        self.entries["audio.wav"] = pcm(4800)[:-4]
        self.save()
        with self.assertRaisesRegex(VerificationError, "Truncated PCM"):
            verify_archive(self.archive)

    def test_duplicate_entries_rejected(self):
        self.save()
        with zipfile.ZipFile(self.archive, "a") as archive, self.assertWarns(UserWarning):
            archive.writestr("manifest.json", json.dumps(self.manifest))
        with self.assertRaisesRegex(VerificationError, "duplicate"):
            verify_archive(self.archive)

    def test_png_trailing_data_and_invalid_scanlines_rejected(self):
        with self.assertRaisesRegex(VerificationError, "trailing bytes"):
            validate_png(png(2, 2) + b"extra", 2, 2, "bad.png")
        data = png(2, 2)
        # CRC-valid dimensions can still disagree with the compressed row count.
        ihdr = struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0)
        data = data[:16] + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF) + data[33:]
        with self.assertRaisesRegex(VerificationError, "scanline payload"):
            validate_png(data, 2, 3, "bad.png")

    def test_cli_writes_truthful_failure_report(self):
        self.save()
        self.entries["frames/000000.png"] = b"not PNG"
        self.save()
        report = self.root / "report.json"
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main([str(self.archive), "--report", str(report)]), 1)
        self.assertFalse(json.loads(report.read_text())["ok"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class SourceVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="source-verification-")
        cls.addClassCleanup(cls.folder.cleanup)
        cls.root = Path(cls.folder.name)
        cls.source = cls.root / "fixture.mkv"
        subprocess.run([sys.executable, "tools/generate_fixture.py", "--output", str(cls.source),
                        "--duration", "0.2", "--width", "64", "--height", "48"], check=True, capture_output=True,
                       cwd=Path(__file__).resolve().parents[1])
        cls.archive = cls.root / "fixture.bapple"
        prepare_video(cls.source, cls.archive)

    def test_every_frame_timestamp_and_pcm_sample_matches(self):
        result = verify_archive(self.archive, source=self.source)
        comparison = result["sourceComparison"]
        self.assertEqual(comparison["framesCompared"], 6)
        self.assertEqual(comparison["rgbBytesCompared"], 64 * 48 * 3 * 6)
        self.assertTrue(comparison["allDecodedRgbFramesEqual"])
        self.assertTrue(comparison["allRelativeTimestampsEqual"])
        self.assertTrue(comparison["normalizedPcmEqual"])
        self.assertTrue(comparison["sourceSha256MatchesManifest"])

    def mutated_archive(self):
        with zipfile.ZipFile(self.archive) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def test_valid_png_with_changed_pixels_fails_source_comparison(self):
        entries = self.mutated_archive()
        entries["frames/000005.png"] = png(64, 48)
        output = self.root / "changed-pixels.bapple"
        write_archive(output, entries)
        self.assertTrue(verify_archive(output)["ok"])
        with self.assertRaisesRegex(VerificationError, "pixels differ at frame 5"):
            verify_archive(output, source=self.source)

    def test_changed_valid_timestamp_fails_source_comparison(self):
        entries = self.mutated_archive()
        manifest = json.loads(entries["manifest.json"])
        manifest["frameTimestampsMicros"][1] += 1
        entries["manifest.json"] = json.dumps(manifest).encode()
        output = self.root / "changed-timestamp.bapple"
        write_archive(output, entries)
        with self.assertRaisesRegex(VerificationError, "frameTimestampsMicros"):
            verify_archive(output, source=self.source)

    def test_changed_valid_audio_fails_source_comparison(self):
        entries = self.mutated_archive()
        data = bytearray(entries["audio.wav"])
        data[-4] ^= 1
        entries["audio.wav"] = bytes(data)
        output = self.root / "changed-audio.bapple"
        write_archive(output, entries)
        self.assertTrue(verify_archive(output)["ok"])
        with self.assertRaisesRegex(VerificationError, "PCM differs"):
            verify_archive(output, source=self.source)


if __name__ == "__main__":
    unittest.main()
