"""Regression tests for the fail-closed runtime evidence gate."""
from array import array
from contextlib import redirect_stderr
import importlib.util
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "verify_cloud_smoke.py"
SPEC = importlib.util.spec_from_file_location("verify_cloud_smoke", MODULE_PATH)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class CloudSmokeVerifierTest(unittest.TestCase):
    def full_reference_report(self):
        duration = 219.1
        frame_count = 6573
        samples = []
        for elapsed in [*range(0, 220, 5), duration]:
            final = elapsed == duration
            requested = min(frame_count - 1, int(elapsed * 30))
            uploaded = requested if final else max(0, requested - 2)
            samples.append({
                "elapsedSeconds": elapsed, "positionSeconds": elapsed,
                "audioPositionSeconds": elapsed, "requestedFrame": requested,
                "uploadedFrame": uploaded, "uploadedPositionSeconds": uploaded / 30,
                "playing": not final, "clipRunning": not final, "audioDriving": not final,
                "warning": None, "heapUsedBytes": 256_000_000,
                "nativeWidth": 480, "nativeHeight": 360,
                "gpuExactMatch": True, "gpuPixelsCompared": 480 * 360,
            })
        return {
            "sourceDurationSeconds": duration, "sourceFrameCount": frame_count,
            "sourceWidth": 480, "sourceHeight": 360, "archiveSha256": "a" * 64,
            "fullPlayback": {
                "status": "passed", "uninterrupted": True, "durationSeconds": duration,
                "elapsedSeconds": duration, "startedAtPositionSeconds": 0,
                "finalPositionSeconds": duration, "finalFrame": frame_count - 1,
                "finalPlaying": False, "finalClipRunning": False, "audioFallback": False,
                "startingCommandIndex": 23, "endingCommandIndex": 23,
                "initialFramePreloadedAtStart": True,
                "sampleIntervalSeconds": 5, "samples": samples, "renderCallbacks": 2100,
                "uniqueUploadedFrames": 2000, "frameCount": frame_count,
                "frameCoverageFraction": 2000 / frame_count,
                "skippedSourceFrames": frame_count - 2000,
                "firstUploadedFrame": 2, "lastUploadedFrame": frame_count - 1,
                "gpuComparisons": len(samples), "gpuPixelsCompared": len(samples) * 480 * 360,
                "peakHeapUsedBytes": 280_000_000, "maxObservedHeapBytes": 3_000_000_000,
                "notes": "Software rendering skipped frames; frame zero was preloaded at start. "
                         "GPU checks prove sampled uploaded pixels, not presentation of every source frame.",
            },
        }

    def test_full_reference_run_accepts_honest_frame_skips(self):
        report = self.full_reference_report()
        self.assertIs(report["fullPlayback"], VERIFIER.verify_reference_full_run(report))

    def test_full_reference_run_accepts_one_second_upload_lag_with_advancing_frames(self):
        report = self.full_reference_report()
        for sample in report["fullPlayback"]["samples"][1:-1]:
            sample["uploadedFrame"] = sample["requestedFrame"] - 30
            sample["uploadedPositionSeconds"] = sample["positionSeconds"] - 1
        VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_frozen_uploaded_frame_despite_advancing_clocks(self):
        report = self.full_reference_report()
        for sample in report["fullPlayback"]["samples"][13:40]:
            sample["uploadedFrame"] = 1884
            sample["uploadedPositionSeconds"] = 1884 / 30
        with self.assertRaisesRegex(ValueError, "uploaded frame is stale"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_upload_lag_over_one_second(self):
        report = self.full_reference_report()
        sample = report["fullPlayback"]["samples"][8]
        sample["uploadedFrame"] = sample["requestedFrame"] - 31
        sample["uploadedPositionSeconds"] = sample["uploadedFrame"] / 30
        with self.assertRaisesRegex(ValueError, "uploaded frame is stale"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_missing_or_invalid_uploaded_timestamp(self):
        for value in (None, -1, float("nan"), 16):
            with self.subTest(value=value):
                report = self.full_reference_report()
                report["fullPlayback"]["samples"][3]["uploadedPositionSeconds"] = value
                with self.assertRaisesRegex(ValueError, "uploadedPositionSeconds|uploaded source timestamp"):
                    VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_stalled_upload_progress_within_freshness_bound(self):
        report = self.full_reference_report()
        sample = report["fullPlayback"]["samples"][4]
        sample.update({"positionSeconds": 17.3, "audioPositionSeconds": 17.3,
                       "requestedFrame": 519, "uploadedFrame": 489,
                       "uploadedPositionSeconds": 16.3})
        with self.assertRaisesRegex(ValueError, "uploaded source clock stalled"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_accepts_initial_asynchronous_seek_and_final_audio_tail(self):
        report = self.full_reference_report()
        first = report["fullPlayback"]["samples"][0]
        first.update({"audioPositionSeconds": 60, "clipRunning": False, "audioDriving": False})
        report["fullPlayback"]["samples"][-1]["audioPositionSeconds"] -= 1.5
        VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_is_required(self):
        with self.assertRaisesRegex(ValueError, "Missing full uninterrupted"):
            VERIFIER.verify_reference_full_run({})

    def test_full_reference_run_rejects_stalled_clocks(self):
        for clock in ("positionSeconds", "audioPositionSeconds"):
            with self.subTest(clock=clock):
                report = self.full_reference_report()
                samples = report["fullPlayback"]["samples"]
                samples[4][clock] = samples[3][clock]
                with self.assertRaisesRegex(ValueError, "stalled|diverged"):
                    VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_warnings_and_fallback(self):
        report = self.full_reference_report()
        report["fullPlayback"]["samples"][5]["warning"] = "audio device stopped advancing"
        with self.assertRaisesRegex(ValueError, "warning or fallback"):
            VERIFIER.verify_reference_full_run(report)
        report = self.full_reference_report()
        report["fullPlayback"]["audioFallback"] = True
        with self.assertRaisesRegex(ValueError, "fallback or warning"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_non_exact_gpu_samples(self):
        for key, value in (("gpuExactMatch", False), ("gpuPixelsCompared", 1), ("nativeWidth", 240)):
            with self.subTest(key=key):
                report = self.full_reference_report()
                report["fullPlayback"]["samples"][8][key] = value
                with self.assertRaisesRegex(ValueError, "GPU pixel match"):
                    VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_inflated_coverage(self):
        for key, value in (("frameCoverageFraction", 1), ("uniqueUploadedFrames", 6573),
                           ("skippedSourceFrames", 0), ("renderCallbacks", 100)):
            with self.subTest(key=key):
                report = self.full_reference_report()
                report["fullPlayback"][key] = value
                with self.assertRaisesRegex(ValueError, "coverage|callback count"):
                    VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_premature_end(self):
        for key, value in (("elapsedSeconds", 100), ("finalPositionSeconds", 218), ("finalFrame", 6571)):
            with self.subTest(key=key):
                report = self.full_reference_report()
                report["fullPlayback"][key] = value
                with self.assertRaisesRegex(ValueError, "duration|final source"):
                    VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_missing_periodic_samples(self):
        report = self.full_reference_report()
        report["fullPlayback"]["samples"] = report["fullPlayback"]["samples"][::2]
        with self.assertRaisesRegex(ValueError, "periodic samples"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_sparse_sampling_even_with_enough_samples(self):
        report = self.full_reference_report()
        samples = report["fullPlayback"]["samples"]
        samples.pop(3)
        with self.assertRaisesRegex(ValueError, "periodic samples"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_intervening_commands(self):
        report = self.full_reference_report()
        report["fullPlayback"]["endingCommandIndex"] += 1
        with self.assertRaisesRegex(ValueError, "Commands interrupted"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_missing_archive_identity(self):
        report = self.full_reference_report()
        report["archiveSha256"] = "not a hash"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            VERIFIER.verify_reference_full_run(report)

    def test_full_reference_run_rejects_non_finite_clock(self):
        report = self.full_reference_report()
        report["fullPlayback"]["samples"][3]["positionSeconds"] = float("nan")
        with self.assertRaisesRegex(ValueError, "invalid full playback positionSeconds"):
            VERIFIER.verify_reference_full_run(report)

    def test_reference_report_requires_the_full_run_but_synthetic_report_does_not(self):
        for reference in (False, True):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "smoke-report.json"
                report = self.full_reference_report()
                report.update({"schemaVersion": 1, "status": "passed", "finalStage": 12,
                               "minecraft": "1.21.1", "referenceMode": reference,
                               "assertions": ["checked"] * 30, "commands": [{"result": 1}] * 20,
                               "syntheticUninterruptedPlaybackSeconds": 6.1,
                               "syntheticUninterruptedFinalPositionSeconds": 6.0,
                               "checkpoints": []})
                expected = VERIFIER.REFERENCE_CHECKPOINTS if reference else VERIFIER.EXPECTED_CHECKPOINTS
                for name, position in expected.items():
                    framebuffer = {"grayscaleFraction": 1, "luminanceRange": 255,
                                   "darkPixels": 1000, "lightPixels": 1000}
                    for corner, x, y in zip(VERIFIER.CORNERS, (0, 100, 0, 100), (0, 0, 100, 100)):
                        framebuffer[corner] = {"pixels": 300, "centerX": x, "centerY": y}
                    report["checkpoints"].append({
                        "name": name, "seconds": position, "frame": position * 30 if reference else position,
                        "gpuExactMatch": True, "gpuPixelsCompared": 480 * 360,
                        "worldWidth": 480, "worldHeight": 360, "framebuffer": framebuffer,
                    })
                    for suffix in ("-gpu.png", "-world.png"):
                        (path.parent / f"{name}{suffix}").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))
                path.write_text(json.dumps(report))
                VERIFIER.verify_report(path, reference=reference)
                del report["fullPlayback"]
                path.write_text(json.dumps(report))
                if reference:
                    with self.assertRaisesRegex(ValueError, "Missing full uninterrupted"):
                        VERIFIER.verify_report(path, reference=True)
                else:
                    VERIFIER.verify_report(path)
                    del report["syntheticUninterruptedPlaybackSeconds"]
                    path.write_text(json.dumps(report))
                    with self.assertRaisesRegex(ValueError, "six-second uninterrupted synthetic"):
                        VERIFIER.verify_report(path)

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

    def write_tone(self, path, frequency=440, amplitude=6000, right_channel=True, right_frequency=None):
        values = array("h")
        for i in range(48000):
            sample = int(amplitude * math.sin(2 * math.pi * frequency * i / 48000))
            other = int(amplitude * math.sin(2 * math.pi * right_frequency * i / 48000)) if right_frequency else sample
            values.extend((sample, other if right_channel else 0))
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

    def test_wrong_tone_failure_preserves_bounded_signal_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.write_tone(path / "sink.wav", frequency=880)
            with redirect_stderr(io.StringIO()):
                result = VERIFIER.main([str(path / "smoke-report.json"), "--audio-capture",
                                        str(path / "sink.wav"), "--require-audio"])
            self.assertEqual(1, result)
            diagnostic = json.loads((path / "audio-output-report.json").read_text())
            self.assertEqual("failed", diagnostic["status"])
            self.assertEqual(440, diagnostic["expectedToneHz"])
            self.assertEqual(10, diagnostic["nonSilentWindowsLeft"])
            self.assertEqual(10, diagnostic["nonSilentWindowsRight"])
            self.assertEqual({"880": 10}, diagnostic["measuredFrequencyHistogram"])
            self.assertEqual(10, len(diagnostic["audibleWindowDiagnostics"]))
            self.assertEqual(0, diagnostic["audibleWindowDiagnostics"][0]["startSeconds"])
            self.assertGreater(diagnostic["audibleWindowDiagnostics"][0]["rmsLeft"], 100)
            self.assertLessEqual(len(diagnostic["firstCaptureWindows"]), 10)
            self.assertFalse(diagnostic["virtualSinkOutputVerified"])

    def test_silent_failure_preserves_zero_signal_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.wav"
            self.write_tone(path, amplitude=0)
            with self.assertRaises(VERIFIER.AudioVerificationError) as raised:
                VERIFIER.verify_audio(path)
            diagnostic = raised.exception.diagnostic
            self.assertEqual(0, diagnostic["nonSilentWindowsLeft"])
            self.assertEqual(0, diagnostic["nonSilentWindowsRight"])
            self.assertEqual({}, diagnostic["measuredFrequencyHistogram"])
            self.assertEqual([], diagnostic["audibleWindowDiagnostics"])
            self.assertEqual(10, len(diagnostic["firstCaptureWindows"]))

    def test_missing_right_channel_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.wav"
            self.write_tone(path, right_channel=False)
            with self.assertRaisesRegex(ValueError, "between channels"):
                VERIFIER.verify_audio(path)

    def test_reference_audio_accepts_real_stereo_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.wav"
            self.write_tone(path, right_frequency=880)
            result = VERIFIER.verify_audio(path, frequency=None)
            self.assertEqual(1, result["nonSilentSecondsLeft"])
            self.assertEqual(1, result["nonSilentSecondsRight"])
            self.assertFalse(result["syntheticStereoAgreementVerified"])

    def test_reference_audio_rejects_missing_right_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.wav"
            self.write_tone(path, right_channel=False)
            with self.assertRaisesRegex(ValueError, "right virtual output channel"):
                VERIFIER.verify_audio(path, frequency=None)

    def test_reference_audio_requires_full_song_coverage_in_both_channels(self):
        report = self.full_reference_report()
        audio = {"status": "passed", "virtualSinkOutputVerified": True,
                 "nonSilentSecondsLeft": 180, "nonSilentSecondsRight": 180}
        VERIFIER.verify_reference_audio_duration(report, audio)
        for channel in ("Left", "Right"):
            with self.subTest(channel=channel):
                insufficient = dict(audio, **{f"nonSilentSeconds{channel}": 1})
                with self.assertRaisesRegex(ValueError, "insufficient full-song coverage"):
                    VERIFIER.verify_reference_audio_duration(report, insufficient)

    def test_reference_cli_rejects_short_capture_and_preserves_failed_audio_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.write_tone(path / "sink.wav", right_frequency=880)
            report = self.full_reference_report()
            report.update({"javaSoundClipOpened": True, "audioClockAdvanced": True})
            with patch.object(VERIFIER, "verify_report", return_value=report), redirect_stderr(io.StringIO()):
                result = VERIFIER.main([str(path / "smoke-report.json"), "--reference", "--require-audio",
                                        "--audio-capture", str(path / "sink.wav")])
            self.assertEqual(1, result)
            audio = json.loads((path / "audio-output-report.json").read_text())
            self.assertEqual("failed", audio["status"])
            self.assertFalse(audio["fullReferenceDurationVerified"])

    def test_sink_diagnostic_survives_runtime_failure_without_passing_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.write_tone(path / "sink.wav")
            (path / "smoke-report.json").write_text(json.dumps({"schemaVersion": 1, "status": "failed"}))
            with redirect_stderr(io.StringIO()):
                result = VERIFIER.main([str(path / "smoke-report.json"), "--audio-capture", str(path / "sink.wav"),
                                        "--require-audio"])
            self.assertEqual(1, result)
            self.assertEqual("passed", json.loads((path / "audio-output-report.json").read_text())["status"])


if __name__ == "__main__":
    unittest.main()
