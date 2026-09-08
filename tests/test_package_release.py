"""Release packaging and immutability tests; no game or network required."""

import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.package_release import (
    CHECKPOINTS, ENTRYPOINT, REFERENCE_CHECKPOINTS, SCREENSHOT_SUFFIXES, ReleaseError, collect_evidence, package_release,
    validate_jar, verify_directory,
)


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="badapple-release-test-")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.source = self.root / "source"
        for name in ("README.md", "tools/prepare_video.py", "tools/verify_archive.py", "tools/generate_fixture.py",
                     "docs/releases/v1.1.0.md", "docs/releases/v1.0.0.md", "docs/REFERENCE_SOURCES.md", "CHANGELOG.md"):
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"Documented fixture: {name}\n", encoding="utf-8")
        self.jar = self.root / "input.jar"
        self.make_jar()
        self.output = self.root / "dist/v1.1.0"
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.smoke = {
            "schemaVersion": 1, "status": "passed", "minecraft": "1.21.1", "assertions": ["fixture checked"],
            "checkpoints": [{"name": name, "gpuExactMatch": True, "gpuPixelsCompared": 172800} for name in CHECKPOINTS],
        }
        self.write_smoke()
        for name in CHECKPOINTS:
            for suffix in SCREENSHOT_SUFFIXES:
                (self.evidence / (name + suffix)).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())

    def write_smoke(self):
        (self.evidence / "smoke-report.json").write_text(json.dumps(self.smoke), encoding="utf-8")

    def make_jar(self, version="1.1.0", *, extra=None, omit_main=False, overrides=None):
        metadata = {"id": "badapple", "version": version, "environment": "client",
                    "depends": {"minecraft": "1.21.1", "java": ">=21"}, "entrypoints": {"client": [ENTRYPOINT]}}
        metadata.update(overrides or {})
        with zipfile.ZipFile(self.jar, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps(metadata))
            if not omit_main:
                archive.writestr(ENTRYPOINT.replace(".", "/") + ".class", b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65))
            for name, data in (extra or {}).items():
                archive.writestr(name, data)

    def package(self, **overrides):
        args = dict(version="1.1.0", commit="a" * 40, jar=self.jar, output=self.output,
                    source_dir=self.source, evidence_dir=self.evidence)
        args.update(overrides)
        return package_release(**args)

    def test_complete_release_round_trip_and_bundle_contents(self):
        info = self.package()
        self.assertEqual(verify_directory(self.output)["assetsVerified"], 4)
        self.assertEqual(info["commit"], "a" * 40)
        self.assertFalse(info["sourceMediaIncluded"])
        self.assertEqual(info["verification"]["smokeStatus"], "passed")
        with zipfile.ZipFile(self.output / "minecraft-bad-apple-1.1.0-portable.zip") as bundle:
            self.assertIn("docs/REFERENCE_SOURCES.md", bundle.namelist())
            self.assertIn("docs/releases/v1.1.0.md", bundle.namelist())
            self.assertEqual(bundle.read("minecraft-bad-apple-1.1.0.jar"), self.jar.read_bytes())
            self.assertEqual(bundle.read("build-info.json"), (self.output / "build-info.json").read_bytes())
            self.assertEqual(bundle.namelist(), sorted(bundle.namelist()))
            self.assertTrue(all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in bundle.infolist()))

    def test_packaging_is_reproducible_with_source_date_epoch(self):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1788825600"}):
            self.package()
            second = self.root / "dist/repeat"
            self.package(output=second)
        for path in self.output.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes(), path.name)

    def test_existing_output_is_never_overwritten_even_if_empty(self):
        self.output.mkdir(parents=True)
        with self.assertRaisesRegex(ReleaseError, "already exists"):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_version_mismatch_is_rejected_before_creating_output(self):
        self.make_jar(version="1.0.0")
        with self.assertRaisesRegex(ReleaseError, "version does not match"):
            self.package()
        self.assertFalse(self.output.exists())

    def test_wrong_minecraft_or_environment_are_rejected(self):
        for override in ({"environment": "*"}, {"depends": {"minecraft": "1.21.2", "java": ">=21"}}):
            with self.subTest(override=override):
                self.make_jar(overrides=override)
                with self.assertRaises(ReleaseError):
                    self.package()

    def test_smoke_classes_and_resources_cannot_leak_into_production_jar(self):
        for name in ("dev/legend/badapple/smoke/CloudSmokeClient.class", "badapple_smoke.json", "smoke/fabric.mod.json", "foo/TimelineTest.class"):
            with self.subTest(name=name):
                self.make_jar(extra={name: b"test"})
                with self.assertRaisesRegex(ReleaseError, "leaks smoke/test"):
                    self.package()

    def test_main_class_must_exist_and_jar_must_be_valid(self):
        self.make_jar(omit_main=True)
        with self.assertRaisesRegex(ReleaseError, "main class is missing"):
            validate_jar(self.jar, "1.1.0")
        self.jar.write_bytes(b"not a JAR")
        with self.assertRaisesRegex(ReleaseError, "Invalid production JAR"):
            validate_jar(self.jar, "1.1.0")

    def test_changed_asset_fails_digest_verification(self):
        self.package()
        with (self.output / "minecraft-bad-apple-1.1.0.jar").open("ab") as handle:
            handle.write(b"corruption")
        with self.assertRaisesRegex(ReleaseError, "SHA-256 mismatch"):
            verify_directory(self.output)

    def test_missing_and_extra_release_files_are_rejected(self):
        self.package()
        extra = self.output / "unexpected.txt"
        extra.write_text("extra")
        with self.assertRaisesRegex(ReleaseError, "missing or extra"):
            verify_directory(self.output)
        extra.unlink()
        (self.output / "minecraft-bad-apple-1.1.0-portable.zip").unlink()
        with self.assertRaisesRegex(ReleaseError, "missing or extra"):
            verify_directory(self.output)

    def test_checksums_reject_traversal(self):
        self.package()
        (self.output / "SHA256SUMS").write_text("0" * 64 + "  ../outside.txt\n")
        with self.assertRaisesRegex(ReleaseError, "unsafe filename"):
            verify_directory(self.output)

    def test_missing_failed_or_inexact_smoke_blocks_new_release(self):
        for changes in ({"status": "failed"}, {"checkpoints": []}, {"assertions": []}):
            with self.subTest(changes=changes):
                original = dict(self.smoke)
                self.smoke.update(changes)
                self.write_smoke()
                with self.assertRaises(ReleaseError):
                    self.package()
                self.smoke = original
        self.smoke["checkpoints"][0]["gpuExactMatch"] = False
        self.write_smoke()
        with self.assertRaisesRegex(ReleaseError, "matching exactly"):
            self.package()

    def test_missing_screenshot_blocks_release(self):
        (self.evidence / (CHECKPOINTS[0] + "-gpu.png")).unlink()
        with self.assertRaisesRegex(ReleaseError, "screenshot missing"):
            self.package()

    def test_only_historical_baseline_can_skip_smoke(self):
        with self.assertRaisesRegex(ReleaseError, "historical 1.0.0"):
            self.package(no_smoke_required=True)
        self.make_jar(version="1.0.0")
        info = self.package(version="1.0.0", no_smoke_required=True, evidence_dir=None)
        self.assertEqual(info["verification"]["smokeStatus"], "not-run-historical-baseline")
        self.assertEqual(verify_directory(self.output)["assetsVerified"], 3)

    def test_evidence_allowlist_excludes_source_media_and_unselected_files(self):
        for name in ("source.mp4", "audio.wav", "archive.bapple", "credentials.txt", "random.png", "secret.json"):
            (self.evidence / name).write_bytes(b"never include this")
        reports = self.evidence / "reports"
        reports.mkdir()
        (reports / "full-length.json").write_text('{"status": "passed", "frames": 6570}')
        (reports / "video.mp4").write_bytes(b"never include this")
        self.package()
        with zipfile.ZipFile(self.output / "minecraft-bad-apple-1.1.0-evidence.zip") as evidence:
            self.assertEqual(set(evidence.namelist()), {"smoke-report.json", "reports/full-length.json"} |
                             {name + suffix for name in CHECKPOINTS for suffix in SCREENSHOT_SUFFIXES})

    def test_optional_reference_evidence_is_packaged_without_raw_audio(self):
        reference = self.evidence / "reference"
        reference.mkdir()
        (reference / "smoke-report.json").write_text('{"status":"passed","referenceMode":true}')
        (reference / "audio-output-report.json").write_text('{"virtualSinkOutputVerified":true}')
        (reference / "audio-capture.wav").write_bytes(b"not public")
        for name in REFERENCE_CHECKPOINTS:
            for suffix in SCREENSHOT_SUFFIXES:
                (reference / (name + suffix)).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        selected = collect_evidence(self.evidence)
        self.assertIn("reference/smoke-report.json", selected)
        self.assertIn("reference/audio-output-report.json", selected)
        self.assertIn("reference/01-reference-30-seconds-world.png", selected)
        self.assertNotIn("reference/audio-capture.wav", selected)

    def test_sensitive_report_names_content_and_symlinks_are_rejected(self):
        reports = self.evidence / "reports"
        reports.mkdir()
        sensitive = reports / "credentials.json"
        sensitive.write_text("{}")
        with self.assertRaisesRegex(ReleaseError, "sensitive-looking"):
            collect_evidence(self.evidence)
        sensitive.unlink()
        selected = reports / "build.json"
        selected.write_text('{"access_token": "abcdefghijk12345"}')
        with self.assertRaisesRegex(ReleaseError, "credential"):
            collect_evidence(self.evidence)
        selected.unlink()
        selected.symlink_to(self.jar)
        with self.assertRaisesRegex(ReleaseError, "symlink"):
            collect_evidence(self.evidence)

    def test_full_commit_sha_and_versioned_documentation_are_required(self):
        with self.assertRaisesRegex(ReleaseError, "full 40- or 64-character"):
            self.package(commit="abcdef")
        (self.source / "docs/releases/v1.1.0.md").unlink()
        with self.assertRaisesRegex(ReleaseError, "Versioned release notes"):
            self.package()

    def test_evidence_limits_and_invalid_timestamp_are_enforced(self):
        with mock.patch("tools.package_release.MAX_FILE_BYTES", 10):
            with self.assertRaisesRegex(ReleaseError, "size limits"):
                self.package()
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "9999999999"}):
            with self.assertRaisesRegex(ReleaseError, "between 1970 and 2099"):
                self.package()


if __name__ == "__main__":
    unittest.main()
