#!/usr/bin/env python3
"""Create and verify immutable, checksummed Minecraft Bad Apple release assets.

Uses only the Python standard library. Source media is never packaged. ZIP
members have fixed timestamps and ordering. Set SOURCE_DATE_EPOCH to a commit
timestamp for byte-for-byte repeatability of the generated provenance metadata.
Checksums detect corruption, not the authenticity of an unsigned release.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import sys
import tempfile
import zipfile


class ReleaseError(Exception):
    """Release inputs or immutable output failed validation."""


MINECRAFT = "1.21.1"
ENTRYPOINT = "dev.legend.badapple.client.BadAppleClient"
CHECKPOINTS = (
    "01-first-frame", "02-seek-frame-90",
    "03-native-one-pixel-per-block", "04-reloaded-frame-60",
)
REFERENCE_CHECKPOINTS = (
    "01-reference-30-seconds", "02-reference-60-seconds",
    "03-reference-native-120-seconds", "04-reference-reloaded-60-seconds",
)
SCREENSHOT_SUFFIXES = ("-gpu.png", "-world.png")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_FILES = 256
ROOT_REPORTS = {
    "smoke-report.json", "verification-report.json", "full-length-report.json",
    "fixture-verification.json", "reference-probe.json", "reference-report.json",
    "cloud-smoke-verification.json", "runtime-audio-report.json", "audio-output-report.json",
    "python-tests.txt", "java-tests.txt", "build.log", "smoke-client.log",
}
REPORT_SUFFIXES = {".json", ".xml", ".txt"}
DENIED_NAME = re.compile(r"(?:secret|credential|cookie|password|token|private|oauth|\.env)", re.I)
SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-proj-[A-Za-z0-9_-]{20,})|"
    r"\bAuthorization\s*[:=]\s*['\"]?(?:Bearer|Basic)\s+\S+|"
    r"['\"]?(?:access[_-]?token|refresh[_-]?token|api[_-]?key|password|client[_-]?secret)['\"]?"
    r"\s*[:=]\s*['\"]?(?!null\b|false\b|REDACTED\b|<redacted>)[A-Za-z0-9_./+%-]{8,}",
    re.I,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_version(version: str) -> None:
    require(bool(re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?", version)),
            "Version must be a semantic version without a leading v.")


def read_json(data: bytes, name: str) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"Invalid JSON in {name}: {exc}") from exc
    require(isinstance(value, dict), f"{name} must contain a JSON object.")
    return value


def validate_jar(jar: Path, version: str) -> dict:
    require(jar.is_file() and not jar.is_symlink(), f"Production JAR is not a regular file: {jar}")
    require(jar.stat().st_size <= 128 * 1024 * 1024, "Production JAR is unexpectedly large.")
    try:
        with zipfile.ZipFile(jar) as archive:
            names = archive.namelist()
            require(len(names) == len(set(names)), "Production JAR contains duplicate entries.")
            require(all(not name.startswith("/") and ".." not in PurePosixPath(name).parts and "\\" not in name
                        for name in names), "Production JAR contains unsafe paths.")
            require(sum(info.file_size for info in archive.infolist()) <= 128 * 1024 * 1024,
                    "Production JAR uncompressed size is unexpectedly large.")
            leaked = [name for name in names if any(part in {"smoke", "test", "tests"}
                      for part in PurePosixPath(name.lower()).parts)
                      or "cloudsmoke" in name.lower() or "badapple_smoke" in name.lower()
                      or name.lower().endswith(("test.class", "tests.class"))]
            require(not leaked, f"Production JAR leaks smoke/test harness files: {leaked[:4]}")
            require("fabric.mod.json" in names, "Production JAR has no fabric.mod.json.")
            require(archive.getinfo("fabric.mod.json").file_size <= 64 * 1024, "Fabric metadata is too large.")
            metadata = read_json(archive.read("fabric.mod.json"), "fabric.mod.json")
            require(metadata.get("id") == "badapple", "Unexpected Fabric mod id.")
            require(metadata.get("version") == version, "Fabric mod version does not match release version.")
            require(metadata.get("environment") == "client", "Fabric environment must be client.")
            depends = metadata.get("depends", {})
            require(isinstance(depends, dict) and depends.get("minecraft") == MINECRAFT,
                    f"Fabric Minecraft dependency must be exactly {MINECRAFT}.")
            require(depends.get("java") == ">=21", "Fabric Java dependency must be >=21.")
            entrypoints = metadata.get("entrypoints", {})
            require(isinstance(entrypoints, dict) and entrypoints.get("client") == [ENTRYPOINT],
                    "Fabric client entrypoint differs from the production entrypoint.")
            main_class = ENTRYPOINT.replace(".", "/") + ".class"
            require(main_class in names, "Production client main class is missing.")
            with archive.open(main_class) as handle:
                header = handle.read(8)
            require(len(header) == 8 and header[:4] == b"\xca\xfe\xba\xbe"
                    and struct.unpack(">H", header[6:8])[0] == 65,
                    "Production client main class must be valid Java 21 bytecode.")
            require(archive.testzip() is None, "Production JAR has a corrupt entry.")
            return metadata
    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise ReleaseError(f"Invalid production JAR: {exc}") from exc


def collect_evidence(root: Path | None) -> dict[str, bytes]:
    if root is None:
        return {}
    require(root.is_dir() and not root.is_symlink(), f"Evidence directory is missing or a symlink: {root}")
    selected = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        name = relative.as_posix()
        is_screenshot = (len(relative.parts) == 1 or relative.parts[:-1] == ("screenshots",)) and path.name in {
            checkpoint + suffix for checkpoint in CHECKPOINTS for suffix in SCREENSHOT_SUFFIXES
        }
        is_screenshot |= relative.parts[:-1] == ("reference",) and path.name in {
            checkpoint + suffix for checkpoint in REFERENCE_CHECKPOINTS for suffix in SCREENSHOT_SUFFIXES
        }
        is_screenshot |= relative.parts[:-1] == ("production",) and path.name in {
            checkpoint + suffix for checkpoint in CHECKPOINTS for suffix in SCREENSHOT_SUFFIXES
        }
        is_report = (len(relative.parts) == 1 and path.name in ROOT_REPORTS) or (
            len(relative.parts) == 2 and relative.parts[0] == "reports" and path.suffix.lower() in REPORT_SUFFIXES
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", path.name))
        )
        is_report |= relative.parts[:-1] in (("reference",), ("production",)) and path.name in {
            "smoke-report.json", "audio-output-report.json",
        }
        if not (is_screenshot or is_report):
            continue
        require(not path.is_symlink() and all(not parent.is_symlink() for parent in path.parents
                    if parent != root.parent), f"Evidence may not use symlinks: {name}")
        require(path.is_file(), f"Evidence is not a regular file: {name}")
        require(not DENIED_NAME.search(name), f"Refusing a sensitive-looking evidence filename: {name}")
        size = path.stat().st_size
        total += size
        require(size <= MAX_FILE_BYTES and total <= MAX_EVIDENCE_BYTES, "Evidence exceeds size limits.")
        require(len(selected) < MAX_EVIDENCE_FILES, "Evidence exceeds file-count limit.")
        data = path.read_bytes()
        if is_screenshot:
            require(data.startswith(b"\x89PNG\r\n\x1a\n"), f"Screenshot is not a PNG: {name}")
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReleaseError(f"Evidence report must be UTF-8 text: {name}") from exc
            require(not SECRET_CONTENT.search(text), f"Evidence may contain a credential; redact it before packaging: {name}")
        selected[name] = data
    return selected


def validate_smoke(evidence: dict[str, bytes]) -> dict:
    require("smoke-report.json" in evidence, "Passing smoke-report.json is required for this release.")
    report = read_json(evidence["smoke-report.json"], "smoke-report.json")
    require(report.get("schemaVersion") == 1 and report.get("status") == "passed"
            and report.get("minecraft") == MINECRAFT, "Smoke report must be a passed Minecraft 1.21.1 schema-1 report.")
    require(isinstance(report.get("assertions"), list) and bool(report["assertions"]), "Smoke report has no assertions.")
    checkpoints = report.get("checkpoints", [])
    require(isinstance(checkpoints, list) and len(checkpoints) == len(CHECKPOINTS)
            and all(isinstance(checkpoint, dict) for checkpoint in checkpoints), "Smoke report must contain four checkpoints.")
    require({checkpoint.get("name") for checkpoint in checkpoints} == set(CHECKPOINTS), "Smoke checkpoint names differ.")
    for checkpoint in checkpoints:
        require(checkpoint.get("gpuExactMatch") is True and checkpoint.get("gpuPixelsCompared") == 480 * 360,
                "Every smoke checkpoint must report all 172800 GPU pixels matching exactly.")
        for suffix in SCREENSHOT_SUFFIXES:
            name = checkpoint["name"] + suffix
            require(name in evidence or "screenshots/" + name in evidence, f"Smoke screenshot missing: {name}")
    return report


def validate_loaded_production_jar(runtime: dict, jar: Path, version: str, context: str) -> dict:
    """Shared identity gate for synthetic and full-reference production runs."""
    require(runtime.get("runtimeMode") == "production" and runtime.get("developmentEnvironment") is False
            and runtime.get("runtimeNamespace") == "intermediary",
            f"{context} must run without development mode in the intermediary namespace.")
    require(runtime.get("loadedModVersion") == version, f"{context} loaded a different mod version.")
    jar_digest = sha256(jar)
    require(runtime.get("loadedModJarSha256") == jar_digest and runtime.get("expectedModJarSha256") == jar_digest,
            f"{context} did not load the exact release JAR SHA-256.")
    return {"releaseJarBound": True, "runtimeMode": "production", "developmentEnvironment": False,
            "runtimeNamespace": "intermediary", "loadedModVersion": version, "loadedModJarSha256": jar_digest}


def validate_original(evidence: dict[str, bytes], evidence_dir: Path | None, *,
                      production_jar: Path | None = None, version: str | None = None) -> dict:
    """Fail closed unless acquisition, source pixels, full runtime and sink agree."""
    required = ("reports/reference-acquisition.json", "reports/original-source-comparison.json",
                "reference/smoke-report.json", "reference/audio-output-report.json")
    for name in required:
        require(name in evidence, f"Original-reference release evidence is required: {name}")
    require(evidence_dir is not None, "Original-reference evidence directory is required.")
    acquisition, comparison, runtime, audio = (read_json(evidence[name], name) for name in required)
    if production_jar is not None:
        require(isinstance(version, str), "Original-reference production binding requires a release version.")
        runtime_binding = validate_loaded_production_jar(runtime, production_jar, version, "Original-reference production runtime")
        runtime_binding["scope"] = "Uninterrupted full-original playback on the exact release JAR in a non-development intermediary runtime."
    else:
        runtime_binding = {"releaseJarBound": False,
                           "scope": "Full-original runtime verified without requiring production-mode release-JAR identity."}
    original_url = "https://www.nicovideo.jp/watch/sm8628149"
    require(acquisition.get("status") == "downloaded_converted_verified"
            and acquisition.get("sourceUrl") == original_url,
            "Original creator reference was not successfully acquired and verified.")
    require(acquisition.get("comparisonReport") == "original-source-comparison.json",
            "Original acquisition points to a different comparison report.")
    require(comparison.get("reportVersion") == 1 and comparison.get("ok") is True,
            "Original source comparison is missing a successful schema-1 result.")
    source = comparison.get("sourceComparison", {})
    provenance = comparison.get("provenance", {})
    video = comparison.get("video", {})
    require(all(isinstance(value, dict) for value in (source, provenance, video)),
            "Original comparison source, provenance or video fields are malformed.")
    require(source.get("status") == "passed" and all(source.get(field) is True for field in (
        "allDecodedRgbFramesEqual", "allRelativeTimestampsEqual", "durationEqual",
        "normalizedPcmEqual", "sourceSha256MatchesManifest")),
        "Original source must match every decoded RGB frame, timestamp, duration and normalized PCM sample.")
    source_digest = source.get("sourceSha256")
    archive_digest = comparison.get("archiveSha256")
    require(isinstance(source_digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", source_digest))
            and acquisition.get("sourceSha256") == source_digest
            and provenance.get("recordedSourceSha256") == source_digest,
            "Original acquisition, comparison and archive-manifest source hashes must match.")
    require(provenance.get("recordedSourceUrl") == original_url,
            "Original archive manifest names a different reference URL.")
    require(isinstance(archive_digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", archive_digest))
            and runtime.get("archiveSha256") == archive_digest,
            "Original runtime archive SHA-256 does not match the exhaustively compared archive.")
    for field in ("width", "height", "frameCount", "durationMicros"):
        require(type(video.get(field)) is int and video[field] > 0,
                f"Original comparison has invalid video {field}.")
    require(source.get("framesCompared") == video["frameCount"],
            "Original source comparison did not cover every frame.")
    require(runtime.get("sourceWidth") == video["width"] and runtime.get("sourceHeight") == video["height"]
            and runtime.get("sourceFrameCount") == video["frameCount"],
            "Original runtime dimensions or frame count differ from the compared archive.")
    duration = video["durationMicros"] / 1_000_000
    runtime_duration = runtime.get("sourceDurationSeconds")
    require(type(runtime_duration) in (int, float) and math.isfinite(runtime_duration)
            and abs(runtime_duration - duration) <= 0.00001,
            "Original runtime duration differs from the compared archive.")
    for name in REFERENCE_CHECKPOINTS:
        for suffix in SCREENSHOT_SUFFIXES:
            relative = "reference/" + name + suffix
            require(relative in evidence, f"Original reference screenshot missing: {relative}")
            require((evidence_dir / relative).read_bytes() == evidence[relative],
                    "Original reference screenshot changed during evidence collection.")
    runtime_path = evidence_dir / "reference/smoke-report.json"
    require(runtime_path.read_bytes() == evidence["reference/smoke-report.json"],
            "Original runtime report changed during evidence collection.")
    # Use the same independent runtime gate as CI, on the actual evidence path.
    # Its reference path includes exhaustive checkpoints and the uninterrupted
    # fullPlayback report; unit tests of that verifier own its detailed schema.
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from tools.verify_cloud_smoke import verify_report
        verified = verify_report(runtime_path, reference=True)
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError("Original Minecraft reference runtime/full-playback verification failed.") from exc
    require(verified == runtime, "Original runtime report changed during verification.")
    full = runtime.get("fullPlayback")
    require(isinstance(full, dict) and full.get("status") == "passed" and full.get("uninterrupted") is True,
            "Original reference requires successful uninterrupted full-duration Minecraft playback.")
    require(runtime.get("javaSoundClipOpened") is True and runtime.get("audioClockAdvanced") is True
            and runtime.get("audioWarning") is None and not runtime.get("audioWarnings"),
            "Original Minecraft playback audio failed or fell back to a silent clock.")
    require(audio.get("status") == "passed" and audio.get("virtualSinkOutputVerified") is True
            and audio.get("sampleRate") == 48000 and audio.get("channels") == 2
            and audio.get("physicalAudioVerified") is False,
            "Original reference requires a passed real 48-kHz stereo virtual-sink output report.")
    captured = audio.get("capturedSeconds")
    require(type(captured) in (int, float) and math.isfinite(captured) and captured >= duration,
            "Original audio capture does not cover the complete source duration.")
    for field in ("nonSilentSecondsLeft", "nonSilentSecondsRight"):
        seconds = audio.get(field)
        require(type(seconds) in (int, float) and math.isfinite(seconds)
                and 0.8 * duration <= seconds <= captured + 0.1,
                "Original reference must reach both stereo output channels for at least 80% of its duration.")
    return {"status": "passed", "sourceUrl": original_url, "sourceSha256": source_digest,
            "archiveSha256": archive_digest, "frameCount": video["frameCount"],
            "width": video["width"], "height": video["height"], "durationSeconds": duration,
            "fullMinecraftPlaybackVerified": True, "stereoVirtualSinkOutputVerified": True,
            "runtime": runtime_binding,
            "youtubeReferenceEquivalenceVerified": False, "physicalAudioVerified": False}


def validate_production(evidence: dict[str, bytes], evidence_dir: Path | None,
                        jar: Path, version: str) -> dict:
    """Bind actual non-development intermediary runtime evidence to this JAR."""
    report_name = "production/smoke-report.json"
    audio_name = "production/audio-output-report.json"
    require(report_name in evidence and audio_name in evidence and evidence_dir is not None,
            "Production release-JAR smoke and audio evidence are required.")
    runtime = read_json(evidence[report_name], report_name)
    audio = read_json(evidence[audio_name], audio_name)
    binding = validate_loaded_production_jar(runtime, jar, version, "Production smoke")
    jar_digest = binding["loadedModJarSha256"]
    for name in CHECKPOINTS:
        for suffix in SCREENSHOT_SUFFIXES:
            relative = "production/" + name + suffix
            require(relative in evidence, f"Production release-JAR screenshot missing: {relative}")
            require((evidence_dir / relative).read_bytes() == evidence[relative],
                    "Production screenshot changed during evidence collection.")
    runtime_path = evidence_dir / report_name
    require(runtime_path.read_bytes() == evidence[report_name],
            "Production runtime report changed during evidence collection.")
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from tools.verify_cloud_smoke import verify_report
        verified = verify_report(runtime_path, reference=False)
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError("Production release-JAR runtime verification failed.") from exc
    require(verified == runtime, "Production runtime report changed during verification.")
    require(runtime.get("javaSoundClipOpened") is True and runtime.get("audioClockAdvanced") is True
            and runtime.get("audioWarning") is None and not runtime.get("audioWarnings"),
            "Production release-JAR audio failed or fell back to a silent clock.")
    require(audio.get("status") == "passed" and audio.get("virtualSinkOutputVerified") is True
            and audio.get("sampleRate") == 48000 and audio.get("channels") == 2
            and audio.get("physicalAudioVerified") is False and audio.get("syntheticStereoAgreementVerified") is True
            and audio.get("expectedToneHz") == 440,
            "Production release-JAR requires passed stereo 440-Hz virtual-sink output evidence.")
    matching, non_silent = audio.get("matchingToneWindows"), audio.get("nonSilentWindows")
    require(type(matching) is int and type(non_silent) is int and 5 <= matching <= non_silent
            and matching / non_silent >= 0.8,
            "Production release-JAR audio lacks sustained matching test-tone output.")
    captured = audio.get("capturedSeconds")
    require(type(captured) in (int, float) and math.isfinite(captured) and captured >= 6,
            "Production audio capture does not cover the uninterrupted six-second smoke fixture.")
    return {"status": "passed", "runtimeMode": "production", "developmentEnvironment": False,
            "runtimeNamespace": "intermediary", "loadedModVersion": version,
            "loadedModJarSha256": jar_digest, "stereoVirtualSinkOutputVerified": True,
            "physicalAudioVerified": False,
            "scope": "Exact release JAR in a non-development intermediary runtime; synthetic audiovisual fixture and command/screen smoke checks."}


def zip_deterministic(destination: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(members.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def timestamp() -> str:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        value = datetime.now(timezone.utc)
    else:
        require(bool(re.fullmatch(r"\d{1,10}", raw)), "SOURCE_DATE_EPOCH must be an integer timestamp.")
        epoch = int(raw)
        require(0 <= epoch <= 4_102_444_799, "SOURCE_DATE_EPOCH must be between 1970 and 2099.")
        value = datetime.fromtimestamp(epoch, timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def package_release(*, version: str, commit: str, jar: Path, output: Path,
                    evidence_dir: Path | None = None, no_smoke_required: bool = False,
                    source_dir: Path | None = None, require_original: bool = False,
                    require_production: bool = False) -> dict:
    valid_version(version)
    require(bool(re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit)), "Commit must be a full 40- or 64-character hexadecimal Git SHA.")
    require(not no_smoke_required or version == "1.0.0", "--no-smoke-required is only allowed for the historical 1.0.0 baseline.")
    require(not output.exists() and not output.is_symlink(), f"Output already exists; immutable releases cannot be overwritten: {output}")
    source = source_dir or Path(__file__).resolve().parents[1]
    validate_jar(jar, version)
    evidence = collect_evidence(evidence_dir)
    smoke = None if no_smoke_required else validate_smoke(evidence)
    original = (validate_original(evidence, evidence_dir, production_jar=jar if require_production else None,
                                  version=version) if require_original else None)
    production = validate_production(evidence, evidence_dir, jar, version) if require_production else None
    files = ["README.md", "tools/prepare_video.py", "tools/verify_archive.py", "tools/generate_fixture.py"]
    for name in files:
        require((source / name).is_file() and not (source / name).is_symlink(), f"Portable bundle source is missing: {name}")
    optional = ["tools/package_release.py", "tools/publish_release.py", "tools/verify_cloud_smoke.py", "tools/probe_references.py", "tools/acquire_original.py",
                "tests/test_package_release.py", "tests/test_publish_release.py", "tests/test_acquire_original.py",
                "tests/test_prepare_video.py", "tests/test_verify_archive.py", "tests/test_cloud_smoke_verifier.py",
                "docs/REFERENCE_SOURCES.md", "docs/REFERENCE.md", "docs/TESTING.md", "CHANGELOG.md"]
    release_notes = f"docs/releases/v{version}.md"
    require((source / release_notes).is_file(), f"Versioned release notes are required: {release_notes}")
    files += [release_notes]
    files += [name for name in optional if (source / name).is_file()]
    for name in files:
        require(not (source / name).is_symlink(), f"Portable source may not be a symlink: {name}")
    portable = {name: (source / name).read_bytes() for name in files}
    jar_name = f"minecraft-bad-apple-{version}.jar"
    bundle_name = f"minecraft-bad-apple-{version}-portable.zip"
    evidence_name = f"minecraft-bad-apple-{version}-evidence.zip"
    assets = ["build-info.json", jar_name, bundle_name]
    if evidence:
        assets.append(evidence_name)
    metadata = {
        "schemaVersion": 1, "project": "Minecraft Bad Apple", "version": version,
        "commit": commit.lower(), "repository": "https://github.com/LegendZ69/Minecraft-Bad-Apple",
        "commitScope": "Production JAR source revision; separately packaged documentation and tools are identified by portableSources digests.",
        "generatedUtc": timestamp(), "minecraft": MINECRAFT, "java": 21, "gradle": "8.8",
        "productionJar": {"name": jar_name, "sha256": sha256(jar)},
        "releaseAssets": sorted(assets), "sourceMediaIncluded": False,
        "verification": {"smokeStatus": "passed" if smoke else "not-run-historical-baseline",
                         "originalRequired": require_original, "originalReference": original,
                         "productionRequired": require_production, "productionRuntime": production,
                         "physicalAudioVerified": False,
                         "scope": ("Original-source acquisition, exhaustive local-source comparison, uninterrupted Minecraft playback and stereo virtual-sink output verified; user YouTube equivalence and physical speakers remain unverified."
                                   if original else "Synthetic cloud evidence does not establish reference-media provenance or physical audio fidelity.")},
        "evidence": [{"path": name, "sha256": digest_bytes(data), "bytes": len(data)}
                     for name, data in sorted(evidence.items())],
        "portableSources": [{"path": name, "sha256": digest_bytes(data)} for name, data in sorted(portable.items())],
        "determinism": {"zipTimestamp": "1980-01-01T00:00:00Z", "sourceDateEpoch": os.environ.get("SOURCE_DATE_EPOCH")},
    }
    info = (json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output.parent) as temporary:
        stage = Path(temporary)
        shutil.copyfile(jar, stage / jar_name)
        require(sha256(stage / jar_name) == metadata["productionJar"]["sha256"], "Production JAR changed while packaging.")
        (stage / "build-info.json").write_bytes(info)
        portable[jar_name] = (stage / jar_name).read_bytes()
        portable["build-info.json"] = info
        zip_deterministic(stage / bundle_name, portable)
        if evidence:
            zip_deterministic(stage / evidence_name, evidence)
        sums = "".join(f"{sha256(stage / name)}  {name}\n" for name in sorted(assets))
        (stage / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")
        verify_directory(stage)
        # Reserve the output exclusively; hard links cannot overwrite a racing file.
        output.mkdir(exist_ok=False)
        for path in sorted(stage.iterdir()):
            os.link(path, output / path.name)
    return metadata


def verify_directory(directory: Path) -> dict:
    require(directory.is_dir() and not directory.is_symlink(), "Release directory is missing or a symlink.")
    sums_file = directory / "SHA256SUMS"
    require(sums_file.is_file() and not sums_file.is_symlink(), "Missing regular SHA256SUMS file.")
    require(sums_file.stat().st_size <= 16 * 1024, "SHA256SUMS is oversized.")
    expected = {}
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        require(match is not None, "Malformed SHA256SUMS line or unsafe filename.")
        digest, name = match.groups()
        require(name != "SHA256SUMS" and name not in expected, "Duplicate or self-referencing SHA256SUMS entry.")
        expected[name] = digest
    require("build-info.json" in expected, "SHA256SUMS does not include build-info.json.")
    actual = {path.name for path in directory.iterdir()}
    require(actual == set(expected) | {"SHA256SUMS"}, "Release directory has missing or extra immutable release files.")
    for name, digest in expected.items():
        path = directory / name
        require(path.is_file() and not path.is_symlink(), f"Release asset is not a regular file: {name}")
        require(sha256(path) == digest, f"SHA-256 mismatch: {name}")
    metadata = read_json((directory / "build-info.json").read_bytes(), "build-info.json")
    version = metadata.get("version")
    require(isinstance(version, str), "Missing release version in build-info.json.")
    valid_version(version)
    require(metadata.get("schemaVersion") == 1 and metadata.get("minecraft") == MINECRAFT,
            "Invalid release metadata schema or Minecraft version.")
    require(metadata.get("releaseAssets") == sorted(expected), "Provenance release asset list differs from SHA256SUMS.")
    jar_name = f"minecraft-bad-apple-{version}.jar"
    required_assets = {"build-info.json", jar_name, f"minecraft-bad-apple-{version}-portable.zip"}
    allowed_assets = required_assets | {f"minecraft-bad-apple-{version}-evidence.zip"}
    require(required_assets <= set(expected) <= allowed_assets, "Release has missing or unexpected versioned asset names.")
    require(jar_name in expected, "Versioned production JAR is missing.")
    require(metadata.get("productionJar") == {"name": jar_name, "sha256": expected[jar_name]}, "Production JAR provenance digest differs.")
    validate_jar(directory / jar_name, version)
    return {"status": "passed", "version": version, "commit": metadata.get("commit"), "assetsVerified": len(expected)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--commit")
    parser.add_argument("--jar", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--no-smoke-required", action="store_true", help="historical 1.0.0 baseline only")
    parser.add_argument("--require-original", action="store_true", help="require acquired original, exhaustive source comparison, full Minecraft playback and stereo output evidence")
    parser.add_argument("--require-production", action="store_true", help="require exact release-JAR smoke and stereo output in a non-development intermediary runtime")
    parser.add_argument("--verify-directory", type=Path, help="verify all existing release assets without modifying them")
    args = parser.parse_args()
    try:
        if args.verify_directory:
            require(not any([args.version, args.commit, args.jar, args.output, args.evidence_dir, args.no_smoke_required,
                             args.require_original, args.require_production]),
                    "--verify-directory cannot be combined with packaging options.")
            result = verify_directory(args.verify_directory)
        else:
            require(all([args.version, args.commit, args.jar, args.output]), "Packaging requires --version, --commit, --jar, and --output.")
            result = package_release(version=args.version, commit=args.commit, jar=args.jar, output=args.output,
                                     evidence_dir=args.evidence_dir, no_smoke_required=args.no_smoke_required,
                                     require_original=args.require_original, require_production=args.require_production)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
