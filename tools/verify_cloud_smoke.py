#!/usr/bin/env python3
"""Fail closed unless an actual Minecraft smoke run produced complete evidence."""
from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
import re
import sys
import wave


EXPECTED_CHECKPOINTS = {
    "01-first-frame": 0,
    "02-seek-frame-90": 90,
    "03-native-one-pixel-per-block": 30,
    "04-reloaded-frame-60": 60,
}
CORNERS = ("topLeftRed", "topRightGreen", "bottomLeftBlue", "bottomRightYellow")
REFERENCE_CHECKPOINTS = {
    "01-reference-30-seconds": 30,
    "02-reference-60-seconds": 60,
    "03-reference-native-120-seconds": 120,
    "04-reference-reloaded-60-seconds": 60,
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify_reference_full_run(report: dict) -> dict:
    """Require native GPU and advancing audio evidence across the entire movie.

    This verifies uninterrupted real-time playback, not that software rendering
    presented every source frame. Frame skips must instead be honestly counted,
    and the actual uploaded frame must remain fresh and advance with playback.
    The first raw Clip position may still belong to the preceding seek while
    Java Sound settles asynchronously; audio-clock checks begin at sample 1.
    """
    full = report.get("fullPlayback")
    require(isinstance(full, dict), "Missing full uninterrupted reference playback report")
    require(full.get("status") == "passed" and full.get("uninterrupted") is True,
            "Full reference playback did not pass uninterrupted")
    require(full.get("audioFallback") is False and report.get("audioWarning") is None
            and not report.get("audioWarnings"), "Full reference playback used an audio fallback or warning")
    require(isinstance(report.get("archiveSha256"), str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", report["archiveSha256"]) is not None,
            "Full reference playback is missing the source archive SHA-256")

    def number(item: dict, key: str, *, integer: bool = False) -> float:
        value = item.get(key)
        require(type(value) in ((int,) if integer else (int, float)) and math.isfinite(value),
                f"Missing or invalid full playback {key}")
        return value

    duration = number(full, "durationSeconds")
    require(duration > 0 and abs(duration - number(report, "sourceDurationSeconds")) <= 0.05,
            "Full playback duration differs from the reference source")
    frame_count = number(full, "frameCount", integer=True)
    require(frame_count > 0 and frame_count == number(report, "sourceFrameCount", integer=True),
            "Full playback frame count differs from the reference source")
    width = number(report, "sourceWidth", integer=True)
    height = number(report, "sourceHeight", integer=True)
    require(width > 0 and height > 0, "Full playback source dimensions must be positive")
    pixel_count = width * height
    elapsed = number(full, "elapsedSeconds")
    require(abs(elapsed - duration) <= max(10.0, duration * 0.05),
            "Full playback elapsed time does not match the complete source duration")
    require(0 <= number(full, "startedAtPositionSeconds") <= 0.15,
            "Full playback did not start at the beginning")
    require(abs(number(full, "finalPositionSeconds") - duration) <= 0.05
            and full.get("finalFrame") == frame_count - 1,
            "Full playback ended before the final source position/frame")
    require(full.get("finalPlaying") is False and full.get("finalClipRunning") is False,
            "Full playback did not stop naturally at the end")
    require(number(full, "startingCommandIndex", integer=True)
            == number(full, "endingCommandIndex", integer=True) >= 0,
            "Commands interrupted full reference playback")
    require(full.get("initialFramePreloadedAtStart") is True,
            "Full playback did not preload source frame zero at the start")
    interval = number(full, "sampleIntervalSeconds")
    require(interval == 5, "Full playback must include five-second periodic samples")
    samples = full.get("samples")
    require(isinstance(samples, list) and len(samples) >= math.floor(duration / interval) + 1
            and 2 <= len(samples) <= math.ceil(duration / interval) + 3,
            "Missing or excessive full playback periodic samples")

    previous = None
    observed_frames = set()
    sampled_peak_heap = 0
    for index, sample in enumerate(samples):
        require(isinstance(sample, dict), f"Invalid full playback sample {index}")
        sample_elapsed = number(sample, "elapsedSeconds")
        position = number(sample, "positionSeconds")
        audio_position = number(sample, "audioPositionSeconds")
        uploaded_position = number(sample, "uploadedPositionSeconds")
        requested = number(sample, "requestedFrame", integer=True)
        uploaded = number(sample, "uploadedFrame", integer=True)
        require(0 <= uploaded <= requested < frame_count,
                f"Invalid requested/uploaded source frame at full playback sample {index}")
        require(sample.get("nativeWidth") == width and sample.get("nativeHeight") == height
                and sample.get("gpuExactMatch") is True and sample.get("gpuPixelsCompared") == pixel_count,
                f"No exhaustive native-resolution GPU pixel match at full playback sample {index}")
        require(sample.get("warning") is None and sample.get("audioFallback", False) is False,
                f"Audio warning or fallback at full playback sample {index}")
        require(0 <= sample_elapsed <= elapsed + 0.25 and 0 <= position <= duration + 0.05
                and 0 <= audio_position <= duration + 1,
                f"Out-of-range clock at full playback sample {index}")
        heap = number(sample, "heapUsedBytes", integer=True)
        require(heap > 0, f"Missing heap observation at full playback sample {index}")
        sampled_peak_heap = max(sampled_peak_heap, heap)
        if index > 0:
            observed_frames.add(uploaded)
        final = index == len(samples) - 1
        if index == 0:
            require(sample_elapsed <= 0.25 and position <= 0.15 and uploaded == 0,
                    "Full playback first sample missed the beginning")
            require(sample.get("playing") is True, "Full playback was not playing at the first sample")
        elif final:
            require(abs(position - duration) <= 0.05 and abs(sample_elapsed - elapsed) <= 0.25
                    and requested == frame_count - 1 and uploaded == frame_count - 1,
                    "Full playback final sample missed the final source position/frame")
            require(sample.get("playing") is False and sample.get("clipRunning") is False,
                    "Full playback final sample did not stop naturally")
            require(duration <= audio_position + 2 and audio_position <= position + 1,
                    "Full playback audio ended too far from the final video position")
        else:
            require(sample.get("playing") is True and sample.get("clipRunning") is True
                    and sample.get("audioDriving") is True,
                    f"Full playback was interrupted or lost its audio clock at sample {index}")
            require(abs(position - audio_position) <= 1,
                    f"Full playback audio/video clocks diverged at sample {index}")
        require(0 <= uploaded_position <= duration and uploaded_position <= position + 1e-6,
                f"Invalid or future uploaded source timestamp at full playback sample {index}")
        require(position - uploaded_position <= 1.0 + 1e-6,
                f"Full playback uploaded frame is stale by more than one second at sample {index}")
        if previous is not None:
            wall_gap = sample_elapsed - previous["elapsedSeconds"]
            require(0 < wall_gap <= interval * 1.5,
                    f"Missing or unordered full playback periodic samples before sample {index}")
            engine_advance = position - previous["positionSeconds"]
            require(engine_advance >= -0.05, f"Full playback engine clock moved backward at sample {index}")
            # A small allowance covers sampling jitter and the final fractional interval.
            require(engine_advance + 0.2 >= wall_gap * 0.5,
                    f"Full playback engine clock stalled at sample {index}")
            require(uploaded >= previous["uploadedFrame"] and requested >= previous["requestedFrame"],
                    f"Full playback source frames moved backward at sample {index}")
            uploaded_advance = uploaded_position - previous["uploadedPositionSeconds"]
            require(uploaded_advance >= -1e-6,
                    f"Full playback uploaded source clock moved backward at sample {index}")
            require(uploaded_advance + (1.0 if final else 0.2) >= wall_gap * 0.5,
                    f"Full playback uploaded source clock stalled at sample {index}")
            if index > 1:
                audio_advance = audio_position - previous["audioPositionSeconds"]
                require(audio_advance >= -0.05, f"Full playback audio clock moved backward at sample {index}")
                require(audio_advance + (2 if final else 0.2) >= wall_gap * 0.5,
                        f"Full playback audio clock stalled at sample {index}")
        previous = sample

    unique = number(full, "uniqueUploadedFrames", integer=True)
    skipped = number(full, "skippedSourceFrames", integer=True)
    require(len(observed_frames) <= unique <= frame_count and unique > 0 and skipped >= 0
            and unique + skipped == frame_count,
            "Full playback frame coverage accounting is inconsistent")
    coverage = number(full, "frameCoverageFraction")
    require(0 < coverage <= 1 and abs(coverage - unique / frame_count) <= 1e-6,
            "Full playback frame coverage fraction is inflated or inconsistent")
    require(number(full, "renderCallbacks", integer=True) >= unique,
            "Full playback render callback count is smaller than unique uploaded frames")
    require(0 <= number(full, "firstUploadedFrame", integer=True) <= min(observed_frames)
            and full.get("lastUploadedFrame") == frame_count - 1,
            "Full playback upload history is inconsistent or missed the final source frame")
    require(full.get("gpuComparisons") == len(samples)
            and full.get("gpuPixelsCompared") == len(samples) * pixel_count,
            "Full playback GPU comparison totals are inconsistent")
    require(number(full, "maxObservedHeapBytes", integer=True)
            >= number(full, "peakHeapUsedBytes", integer=True) >= sampled_peak_heap,
            "Full playback peak heap accounting is inconsistent")
    require(isinstance(full.get("notes"), str) and bool(full["notes"].strip()),
            "Full playback must document frame coverage and verification limitations")
    return full


def verify_report(path: Path, reference: bool = False) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    require(report.get("schemaVersion") == 1, "Unsupported or absent smoke report schema")
    require(report.get("status") == "passed", f"Minecraft smoke failed: {report.get('failure', report.get('status'))}")
    require(report.get("finalStage") == 12, "Smoke client did not finish every stage")
    require(report.get("minecraft") == "1.21.1", "Wrong Minecraft runtime version")
    require(bool(report.get("referenceMode")) == reference, "Wrong media mode in smoke evidence")
    require(len(report.get("assertions", [])) >= 30, "Too few runtime assertions")
    commands = report.get("commands", [])
    require(len(commands) >= 20 and all(item.get("result") == 1 for item in commands), "Missing or failed client commands")
    checkpoints = report.get("checkpoints", [])
    expected = REFERENCE_CHECKPOINTS if reference else EXPECTED_CHECKPOINTS
    require(len(checkpoints) == len(expected), "Missing or duplicate frame checkpoints")
    require({item.get("name") for item in checkpoints} == set(expected), "Unexpected checkpoint names")
    pixel_count = report.get("sourceWidth", 0) * report.get("sourceHeight", 0) if reference else 172800
    require(pixel_count > 0, "Missing source dimensions")
    for checkpoint in checkpoints:
        name = checkpoint["name"]
        if reference:
            require(abs(checkpoint.get("seconds", -1) - expected[name]) < 0.002, f"Wrong source timestamp for {name}")
            require(0 <= checkpoint.get("frame", -1) < report.get("sourceFrameCount", 0), f"Invalid source frame for {name}")
        else:
            require(checkpoint.get("frame") == expected[name], f"Wrong source frame for {name}")
        require(checkpoint.get("gpuExactMatch") is True and checkpoint.get("gpuPixelsCompared") == pixel_count,
                f"No exhaustive native-resolution GPU pixel match for {name}")
        framebuffer = checkpoint.get("framebuffer", {})
        if reference:
            require(framebuffer.get("grayscaleFraction", 0) >= 0.95, f"Reference plane not visible in centered ROI: {name}")
        else:
            for corner in CORNERS:
                require(framebuffer.get(corner, {}).get("pixels", 0) >= 200, f"World screen corner missing: {name}/{corner}")
            tl, tr, bl, br = (framebuffer[corner] for corner in CORNERS)
            require(tl["centerX"] + 50 < tr["centerX"] and bl["centerX"] + 50 < br["centerX"]
                    and tl["centerY"] + 50 < bl["centerY"] and tr["centerY"] + 50 < br["centerY"],
                    f"Incorrect rendered screen orientation for {name}")
        for suffix in ("-gpu.png", "-world.png"):
            image = path.parent / f"{name}{suffix}"
            require(image.is_file() and image.stat().st_size > 100, f"Missing screenshot {image.name}")
            with image.open("rb") as stream:
                require(stream.read(8) == b"\x89PNG\r\n\x1a\n", f"Invalid screenshot {image.name}")
    if reference:
        require(any(item["framebuffer"].get("luminanceRange", 0) >= 128
                    and item["framebuffer"].get("darkPixels", 0) >= 100
                    and item["framebuffer"].get("lightPixels", 0) >= 100 for item in checkpoints),
                "No reference checkpoint visibly contains non-uniform grayscale movie detail")
    native = next(item for item in checkpoints if item["name"].startswith("03-"))
    require(native.get("worldWidth") == (report["sourceWidth"] if reference else 480)
            and native.get("worldHeight") == (report["sourceHeight"] if reference else 360),
            "Native placement is not exactly one world block per source pixel")
    if reference:
        verify_reference_full_run(report)
    else:
        elapsed = report.get("syntheticUninterruptedPlaybackSeconds")
        final_position = report.get("syntheticUninterruptedFinalPositionSeconds")
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and 5.8 <= elapsed <= 11,
                "Missing or incomplete six-second uninterrupted synthetic playback")
        require(type(final_position) in (int, float) and math.isfinite(final_position)
                and abs(final_position - 6.0) <= 0.05,
                "Synthetic uninterrupted playback did not reach its final position")
    return report


class AudioVerificationError(ValueError):
    """A failed sink gate with bounded, serializable signal diagnostics."""

    def __init__(self, message: str, diagnostic: dict):
        super().__init__(message)
        self.diagnostic = dict(diagnostic)


def verify_audio(path: Path, frequency: float | None = 440.0) -> dict:
    """Verify a captured real virtual sink, not a copied source/archive WAV.

    Minecraft's master volume is muted in the isolated game options; Java Sound
    remains outside that mixer. Therefore the known test tone should dominate
    non-silent 100ms windows despite start/stop/seek discontinuities.
    """
    diagnostic = {"physicalAudioVerified": False, "expectedToneHz": frequency,
                  "virtualSinkOutputVerified": False, "nonSilentWindowsLeft": 0,
                  "nonSilentWindowsRight": 0, "nonSilentSecondsLeft": 0, "nonSilentSecondsRight": 0,
                  "measuredFrequencyHistogram": {}, "measuredFrequencyHistogramRight": {},
                  "firstCaptureWindows": [], "audibleWindowDiagnostics": []}

    def require_audio(condition: bool, message: str) -> None:
        if not condition:
            raise AudioVerificationError(message, diagnostic)

    with wave.open(str(path), "rb") as stream:
        diagnostic.update({"sampleRate": stream.getframerate(), "channels": stream.getnchannels(),
                           "sampleWidthBytes": stream.getsampwidth(),
                           "capturedSeconds": stream.getnframes() / stream.getframerate()})
        require_audio(stream.getsampwidth() == 2 and stream.getnchannels() == 2 and stream.getframerate() == 48000,
                      "Capture must be PCM16 stereo 48kHz from the PulseAudio monitor")
        require_audio(stream.getcomptype() == "NONE", "Capture must be uncompressed PCM")
        samples = array("h", stream.readframes(stream.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    mono = samples[::2]
    right = samples[1::2]
    require_audio(len(mono) >= 48000, "Less than one second of captured sink output")
    windows = []
    right_non_silent_windows = 0
    peak_right_rms = 0
    for offset in range(0, len(mono) - 4800 + 1, 4800):
        values = mono[offset:offset + 4800]
        right_values = right[offset:offset + 4800]
        right_rms = math.sqrt(sum(value * value for value in right_values) / len(right_values))
        rms = math.sqrt(sum(value * value for value in values) / len(values))
        measured = sum(a <= 0 < b for a, b in zip(values, values[1:])) * 10.0
        measured_right = sum(a <= 0 < b for a, b in zip(right_values, right_values[1:])) * 10.0
        peak_right_rms = max(peak_right_rms, right_rms)
        window_diagnostic = {"startSeconds": offset / 48000, "durationSeconds": 0.1,
                             "rmsLeft": round(rms, 3), "rmsRight": round(right_rms, 3),
                             "frequencyHzLeft": measured, "frequencyHzRight": measured_right}
        if len(diagnostic["firstCaptureWindows"]) < 10:
            diagnostic["firstCaptureWindows"].append(window_diagnostic)
        if (rms >= 100 or right_rms >= 100) and len(diagnostic["audibleWindowDiagnostics"]) < 60:
            diagnostic["audibleWindowDiagnostics"].append(window_diagnostic)
        if right_rms >= 100:
            right_non_silent_windows += 1
            histogram_right = diagnostic["measuredFrequencyHistogramRight"]
            key = f"{measured_right:g}"
            histogram_right[key] = histogram_right.get(key, 0) + 1
        if rms < 100:
            continue
        windows.append((rms, measured))
        histogram = diagnostic["measuredFrequencyHistogram"]
        key = f"{measured:g}"
        histogram[key] = histogram.get(key, 0) + 1
    matching = [item for item in windows if frequency is None or abs(item[1] - frequency) <= 20]
    diagnostic.update({"capturedSeconds": len(mono) / 48000,
                       "nonSilentWindows": len(windows), "nonSilentWindowsLeft": len(windows),
                       "nonSilentWindowsRight": right_non_silent_windows,
                       "nonSilentSecondsLeft": len(windows) / 10,
                       "nonSilentSecondsRight": right_non_silent_windows / 10,
                       "matchingToneWindows": len(matching),
                       "matchingToneFraction": len(matching) / len(windows) if windows else 0,
                       "peakWindowRms": max((item[0] for item in windows), default=0),
                       "peakWindowRmsRight": peak_right_rms,
                       "frequencyMeasurement": "Positive zero crossings per 100ms window; not a spectral identity test"})
    require_audio(len(windows) >= 5, "No sustained audible signal reached the virtual output sink")
    if frequency is None:
        require_audio(right_non_silent_windows >= 5,
                      "No sustained audible signal reached the right virtual output channel")
    if frequency is not None:
        require_audio(len(matching) >= 5 and len(matching) / len(windows) >= 0.8,
                      f"Captured output does not match the expected {frequency:g}Hz test tone")
        signal_energy = sum(value * value for value in mono)
        channel_error = sum((left - other) ** 2 for left, other in zip(mono, right))
        diagnostic["stereoChannelErrorEnergyFraction"] = channel_error / signal_energy
        require_audio(channel_error <= signal_energy * 0.01,
                      "Synthetic stereo capture differs between channels or one channel is missing")
    return dict(diagnostic, status="passed", virtualSinkOutputVerified=True,
                syntheticStereoAgreementVerified=frequency is not None)


def verify_reference_audio_duration(report: dict, audio_report: dict) -> None:
    """Require substantial stereo sink output; this does not prove PCM identity."""
    duration = report["sourceDurationSeconds"]
    require(audio_report.get("status") == "passed" and audio_report.get("virtualSinkOutputVerified") is True,
            "Full reference playback has no verified virtual-sink audio capture")
    for channel in ("Left", "Right"):
        non_silent = audio_report.get(f"nonSilentSeconds{channel}", 0)
        require(type(non_silent) in (int, float) and math.isfinite(non_silent)
                and non_silent >= duration * 0.8,
                f"Reference audio {channel.lower()} channel has insufficient full-song coverage "
                f"(requires at least 80% of source duration)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--audio-capture", type=Path, help="PCM16 stereo 48kHz recording of the real virtual sink")
    parser.add_argument("--require-audio", action="store_true", help="Fail unless Java Sound and sink capture pass")
    parser.add_argument("--reference", action="store_true", help="Validate reference checkpoints instead of synthetic colors/tone")
    args = parser.parse_args(argv)
    try:
        # Preserve the sink diagnostic even when runtime clock checks fail. A
        # successful PCM capture is only one gate and never overrides the rest.
        if args.audio_capture:
            try:
                audio_report = verify_audio(args.audio_capture, frequency=None if args.reference else 440.0)
            except (OSError, ValueError, wave.Error) as error:
                audio_report = {**getattr(error, "diagnostic", {}), "status": "failed",
                                "failure": str(error), "physicalAudioVerified": False,
                                "virtualSinkOutputVerified": False}
                (args.report.parent / "audio-output-report.json").write_text(
                        json.dumps(audio_report, indent=2) + "\n", encoding="utf-8")
                raise
            (args.report.parent / "audio-output-report.json").write_text(
                    json.dumps(audio_report, indent=2) + "\n", encoding="utf-8")
        report = verify_report(args.report, reference=args.reference)
        if args.require_audio:
            require(report.get("javaSoundClipOpened") is True and report.get("audioClockAdvanced") is True,
                    "Java Sound clip did not open and advance")
            require(report.get("audioWarning") is None, "Playback fell back to silent timing")
            require(not report.get("audioWarnings"), "Playback reported an audio failure before reload")
            require(args.audio_capture is not None, "--require-audio requires --audio-capture")
            if args.reference:
                try:
                    verify_reference_audio_duration(report, audio_report)
                except ValueError as error:
                    audio_report.update({"status": "failed", "failure": str(error),
                                         "fullReferenceDurationVerified": False})
                    (args.report.parent / "audio-output-report.json").write_text(
                            json.dumps(audio_report, indent=2) + "\n", encoding="utf-8")
                    raise
                audio_report["fullReferenceDurationVerified"] = True
                audio_report["sourceDurationSeconds"] = report["sourceDurationSeconds"]
                audio_report["minimumNonSilentFraction"] = 0.8
                (args.report.parent / "audio-output-report.json").write_text(
                        json.dumps(audio_report, indent=2) + "\n", encoding="utf-8")
        compared = sum(item["gpuPixelsCompared"] for item in report["checkpoints"])
        print(f"Cloud Minecraft verification passed: {len(report['assertions'])} assertions; "
              f"4 world screenshots; {compared} exact GPU pixel comparisons")
        if args.reference:
            full = report["fullPlayback"]
            print(f"Full uninterrupted reference playback: {full['elapsedSeconds']:.2f}s; "
                  f"{full['gpuComparisons']} periodic native GPU checks; "
                  f"{full['uniqueUploadedFrames']}/{full['frameCount']} source frames presented "
                  f"({full['skippedSourceFrames']} skipped; every-frame presentation is not claimed)")
        if args.audio_capture:
            print("Real virtual-sink PCM capture contains sustained stereo audio signal; "
                  "source PCM identity and physical speakers are not verified by this capture."
                  if args.reference else "Real virtual-sink PCM capture matched the 440Hz synthetic test tone; physical speakers are not tested.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, wave.Error) as error:
        print(f"Cloud Minecraft verification FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
