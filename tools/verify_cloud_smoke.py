#!/usr/bin/env python3
"""Fail closed unless an actual Minecraft smoke run produced complete evidence."""
from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
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
    return report


def verify_audio(path: Path, frequency: float | None = 440.0) -> dict:
    """Verify a captured real virtual sink, not a copied source/archive WAV.

    Minecraft's master volume is muted in the isolated game options; Java Sound
    remains outside that mixer. Therefore the known test tone should dominate
    non-silent 100ms windows despite start/stop/seek discontinuities.
    """
    with wave.open(str(path), "rb") as stream:
        require(stream.getsampwidth() == 2 and stream.getnchannels() == 2 and stream.getframerate() == 48000,
                "Capture must be PCM16 stereo 48kHz from the PulseAudio monitor")
        require(stream.getcomptype() == "NONE", "Capture must be uncompressed PCM")
        samples = array("h", stream.readframes(stream.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    mono = samples[::2]
    right = samples[1::2]
    require(len(mono) >= 48000, "Less than one second of captured sink output")
    windows = []
    for offset in range(0, len(mono) - 4800 + 1, 4800):
        values = mono[offset:offset + 4800]
        rms = math.sqrt(sum(value * value for value in values) / len(values))
        if rms < 100:
            continue
        crossings = sum(a <= 0 < b for a, b in zip(values, values[1:]))
        measured = crossings * 10.0
        windows.append((rms, measured))
    require(len(windows) >= 5, "No sustained audible signal reached the virtual output sink")
    matching = [item for item in windows if frequency is None or abs(item[1] - frequency) <= 20]
    if frequency is not None:
        require(len(matching) >= 5 and len(matching) / len(windows) >= 0.8,
                f"Captured output does not match the expected {frequency:g}Hz test tone")
        signal_energy = sum(value * value for value in mono)
        channel_error = sum((left - other) ** 2 for left, other in zip(mono, right))
        require(channel_error <= signal_energy * 0.01,
                "Synthetic stereo capture differs between channels or one channel is missing")
    return {"status": "passed", "virtualSinkOutputVerified": True, "physicalAudioVerified": False,
            "sampleRate": 48000, "channels": 2, "capturedSeconds": len(mono) / 48000,
            "nonSilentWindows": len(windows), "matchingToneWindows": len(matching),
            "expectedToneHz": frequency, "peakWindowRms": max(item[0] for item in windows),
            "syntheticStereoAgreementVerified": frequency is not None}


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
                audio_report = {"status": "failed", "failure": str(error), "physicalAudioVerified": False}
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
        compared = sum(item["gpuPixelsCompared"] for item in report["checkpoints"])
        print(f"Cloud Minecraft verification passed: {len(report['assertions'])} assertions; "
              f"4 world screenshots; {compared} exact GPU pixel comparisons")
        if args.audio_capture:
            print("Real virtual-sink PCM capture contains reference audio; physical speakers are not tested."
                  if args.reference else "Real virtual-sink PCM capture matched the 440Hz synthetic test tone; physical speakers are not tested.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, wave.Error) as error:
        print(f"Cloud Minecraft verification FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
