"""Regression tests for the fail-closed runtime evidence gate."""
from array import array
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
import wave

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "verify_cloud_smoke.py"
SPEC = importlib.util.spec_from_file_location("verify_cloud_smoke", MODULE_PATH)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class CloudSmokeVerifierTest(unittest.TestCase):
    def test_failed_report_never_counts_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smoke-report.json"
            path.write_text(json.dumps({"schemaVersion": 1, "status": "failed", "failure": "GPU differs"}))
            with self.assertRaisesRegex(ValueError, "GPU differs"):
                VERIFIER.verify_report(path)

    def test_empty_report_never_counts_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smoke-report.json"
            path.write_text("{}")
            with self.assertRaises(ValueError):
                VERIFIER.verify_report(path)

    def test_incomplete_passed_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smoke-report.json"
            path.write_text(json.dumps({"schemaVersion": 1, "status": "passed", "finalStage": 2}))
            with self.assertRaisesRegex(ValueError, "every stage"):
                VERIFIER.verify_report(path)

    def write_tone(self, path, frequency=440, amplitude=6000):
        values = array("h")
        for i in range(48000):
            sample = int(amplitude * math.sin(2 * math.pi * frequency * i / 48000))
            values.extend((sample, sample))
        with wave.open(str(path), "wb") as stream:
            stream.setparams((2, 2, 48000, 48000, "NONE", "not compressed"))
            stream.writeframes(values.tobytes())

    def test_known_tone_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.wav"
            self.write_tone(path)
            result = VERIFIER.verify_audio(path)
            self.assertEqual("passed", result["status"])
            self.assertGreaterEqual(result["matchingToneWindows"], 9)
            self.assertFalse(result["physicalAudioVerified"])

    def test_silent_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.wav"
            self.write_tone(path, amplitude=0)
            with self.assertRaisesRegex(ValueError, "No sustained"):
                VERIFIER.verify_audio(path)

    def test_wrong_tone_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.wav"
            self.write_tone(path, frequency=880)
            with self.assertRaisesRegex(ValueError, "does not match"):
                VERIFIER.verify_audio(path)


if __name__ == "__main__":
    unittest.main()
