"""Offline release publishing state-machine tests with a mocked GitHub CLI."""

import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.package_release import CHECKPOINTS, ENTRYPOINT, SCREENSHOT_SUFFIXES, package_release
from tools.publish_release import API_ROOT, PublicationError, REPOSITORY, gh, publish_release


class FakeGitHub:
    def __init__(self, source, notes, commit, version):
        self.files = {path.name: path.read_bytes() for path in source.iterdir()}
        self.notes = notes
        self.commit = commit
        self.tag = "v" + version
        self.release = None
        self.uploaded = {}
        self.tag_sha = None
        self.annotated = False
        self.calls = []
        self.failure = None
        self.corrupt_download = False
        self.corrupt_after_publication = False
        self.draft_by_tag_404 = False

    def existing(self, *, draft=False, files=None):
        self.uploaded = dict(self.files if files is None else files)
        self.release = {"id": 42, "tag_name": self.tag, "target_commitish": self.commit,
                        "draft": draft, "prerelease": False, "body": self.notes,
                        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{self.tag}"}
        self.tag_sha = self.commit

    def response(self):
        result = copy.deepcopy(self.release)
        result["assets"] = [{"name": name, "state": "uploaded"} for name in sorted(self.uploaded)]
        return result

    def run(self, command, **kwargs):
        self.calls.append(command)
        assert command[0] == "gh"
        assert "--clobber" not in command
        assert "auth" not in command
        if self.failure:
            return subprocess.CompletedProcess(command, 1, "", self.failure)
        result = ""
        missing = False
        if command[1] == "api":
            endpoint = command[2]
            if endpoint == API_ROOT:
                result = {"full_name": REPOSITORY, "permissions": {"push": True}}
            elif endpoint == f"{API_ROOT}/commits/{self.commit}":
                result = {"sha": self.commit}
            elif endpoint == f"{API_ROOT}/git/ref/tags/{self.tag}":
                missing = self.tag_sha is None
                result = {"ref": "refs/tags/" + self.tag,
                          "object": {"type": "tag" if self.annotated else "commit", "sha": "b" * 40 if self.annotated else self.tag_sha}}
            elif endpoint == f"{API_ROOT}/git/tags/" + "b" * 40:
                result = {"object": {"type": "commit", "sha": self.tag_sha}}
            elif endpoint == f"{API_ROOT}/releases/tags/{self.tag}":
                missing = self.release is None or (self.draft_by_tag_404 and self.release["draft"])
                result = self.response() if self.release else {}
            elif endpoint.startswith(f"{API_ROOT}/releases?per_page=100&page="):
                result = [self.response()] if self.release else []
            else:
                raise AssertionError(f"Unexpected API endpoint: {endpoint}")
            result = json.dumps(result)
        elif command[1:3] == ["release", "create"]:
            assert self.release is None
            assert "--draft" in command
            assert command[command.index("--target") + 1] == self.commit
            self.existing(draft=True, files={})
            self.tag_sha = None  # GitHub can defer tag creation until publication.
        elif command[1:3] == ["release", "upload"]:
            assert self.release["draft"]
            repo_index = command.index("--repo")
            for name in command[repo_index + 2:]:
                path = Path(name)
                assert path.name not in self.uploaded
                self.uploaded[path.name] = path.read_bytes()
        elif command[1:3] == ["release", "download"]:
            directory = Path(command[command.index("--dir") + 1])
            for name, data in self.uploaded.items():
                if self.corrupt_download or (self.corrupt_after_publication and not self.release["draft"]):
                    data += b"corrupt"
                (directory / name).write_bytes(data)
        elif command[1:3] == ["release", "edit"]:
            assert "--draft=false" in command
            assert self.release["draft"]
            self.release["draft"] = False
            self.tag_sha = self.commit
        else:
            raise AssertionError(f"Unexpected GitHub command: {command[:4]}")
        return subprocess.CompletedProcess(command, 1 if missing else 0, result,
                                           "gh: Not Found (HTTP 404)" if missing else "")


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="badapple-publish-test-")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.commit = "a" * 40
        self.version = "1.1.0"
        self.make_release()

    def make_release(self, version="1.1.0"):
        self.version = version
        source = self.root / ("source-" + version)
        for name in ("README.md", "tools/prepare_video.py", "tools/verify_archive.py", "tools/generate_fixture.py",
                     f"docs/releases/v{version}.md"):
            file = source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"# v{version}\nVerification complete; physical speakers were not tested.\n")
        self.notes = source / f"docs/releases/v{version}.md"
        jar = self.root / f"input-{version}.jar"
        metadata = {"id": "badapple", "version": version, "environment": "client",
                    "depends": {"minecraft": "1.21.1", "java": ">=21"}, "entrypoints": {"client": [ENTRYPOINT]}}
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps(metadata))
            archive.writestr(ENTRYPOINT.replace(".", "/") + ".class", b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65))
        evidence = source / "evidence"
        evidence.mkdir()
        smoke = {"schemaVersion": 1, "status": "passed", "minecraft": "1.21.1", "assertions": ["checked"],
                 "checkpoints": [{"name": name, "gpuExactMatch": True, "gpuPixelsCompared": 172800} for name in CHECKPOINTS]}
        (evidence / "smoke-report.json").write_text(json.dumps(smoke))
        for name in CHECKPOINTS:
            for suffix in SCREENSHOT_SUFFIXES:
                (evidence / (name + suffix)).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        self.directory = self.root / ("dist-" + version)
        package_release(version=version, commit=self.commit, jar=jar, output=self.directory,
                        source_dir=source, evidence_dir=evidence)
        self.report = self.root / ("publication-" + version + ".json")
        self.github = FakeGitHub(self.directory, self.notes.read_text(), self.commit, version)

    def publish(self):
        with mock.patch("tools.publish_release.subprocess.run", side_effect=self.github.run):
            return publish_release(directory=self.directory, notes=self.notes, report=self.report)

    def mutations(self):
        return [command for command in self.github.calls if command[1:3] in
                (["release", "create"], ["release", "upload"], ["release", "edit"])]

    def test_new_release_is_verified_as_draft_then_published_and_redownloaded(self):
        result = self.publish()
        self.assertEqual(result["status"], "published_verified")
        self.assertEqual(result["commit"], self.commit)
        self.assertEqual(json.loads(self.report.read_text()), result)
        actions = [command[2] for command in self.github.calls if command[1] == "release"]
        self.assertEqual(actions, ["create", "upload", "download", "edit", "download"])
        self.assertEqual(len(result["assets"]), 5)
        self.assertTrue(all("--latest" in command for command in self.mutations() if command[2] == "edit"))
        self.assertTrue(result["publishedNotesMatchRequested"])

    def test_baseline_is_never_marked_latest(self):
        self.make_release("1.0.0")
        self.publish()
        self.assertTrue(all("--latest=false" in command for command in self.mutations() if command[2] == "edit"))

    def test_published_release_is_only_verified_and_never_edited(self):
        self.github.existing()
        result = self.publish()
        self.assertTrue(result["alreadyPublished"])
        self.assertEqual(self.mutations(), [])

    def test_published_release_different_bytes_or_missing_assets_are_refused(self):
        for mode in ("corrupt", "missing", "extra"):
            with self.subTest(mode=mode):
                self.github.existing()
                if mode == "corrupt":
                    self.github.uploaded["SHA256SUMS"] += b"corrupt"
                elif mode == "missing":
                    self.github.uploaded.pop("SHA256SUMS")
                else:
                    self.github.uploaded["unexpected.txt"] = b"extra"
                with self.assertRaises(PublicationError):
                    self.publish()
                self.assertEqual(self.mutations(), [])

    def test_existing_draft_resumes_only_missing_assets_without_clobber(self):
        self.github.existing(draft=True, files={"SHA256SUMS": self.github.files["SHA256SUMS"]})
        self.github.draft_by_tag_404 = True
        self.publish()
        actions = [command[2] for command in self.github.calls if command[1] == "release"]
        self.assertEqual(actions, ["download", "upload", "download", "edit", "download"])
        upload = next(command for command in self.github.calls if command[1:3] == ["release", "upload"])
        self.assertFalse(any(argument.endswith("/SHA256SUMS") for argument in upload))

    def test_existing_draft_notes_target_or_asset_mismatch_prevents_mutation(self):
        for mode in ("notes", "target", "asset"):
            with self.subTest(mode=mode):
                self.github.existing(draft=True)
                if mode == "notes":
                    self.github.release["body"] = "Different notes"
                elif mode == "target":
                    self.github.release["target_commitish"] = "c" * 40
                else:
                    self.github.uploaded["SHA256SUMS"] = b"Different bytes"
                with self.assertRaises(PublicationError):
                    self.publish()
                self.assertEqual(self.mutations(), [])

    def test_wrong_tag_commit_is_never_moved(self):
        self.github.existing()
        self.github.tag_sha = "c" * 40
        with self.assertRaisesRegex(PublicationError, "different commit"):
            self.publish()
        self.assertEqual(self.mutations(), [])

    def test_annotated_tag_is_peeled_to_its_commit(self):
        self.github.existing()
        self.github.annotated = True
        self.assertEqual(self.publish()["commit"], self.commit)

    def test_failed_draft_download_never_publishes(self):
        self.github.corrupt_download = True
        with self.assertRaisesRegex(PublicationError, "SHA-256 differs"):
            self.publish()
        self.assertTrue(self.github.release["draft"])
        self.assertFalse(self.report.exists())
        self.assertFalse(any(command[2] == "edit" for command in self.mutations()))

    def test_failed_published_redownload_does_not_claim_success(self):
        self.github.corrupt_after_publication = True
        with self.assertRaisesRegex(PublicationError, "SHA-256 differs"):
            self.publish()
        self.assertFalse(self.github.release["draft"])
        self.assertFalse(self.report.exists())

    def test_permissions_and_auth_failures_never_trigger_creation(self):
        for message in ("gh: Forbidden (HTTP 403)", "gh: Bad credentials (HTTP 401)", "authentication required", "release not found"):
            with self.subTest(message=message):
                self.github.failure = message
                with self.assertRaisesRegex(PublicationError, "not missing releases"):
                    self.publish()
                self.assertEqual(self.mutations(), [])

    def test_only_explicit_http404_can_be_returned_as_missing(self):
        with mock.patch("tools.publish_release.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "release not found")):
            with self.assertRaises(PublicationError):
                gh(["api", "example"], allow_missing=True)
        with mock.patch("tools.publish_release.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")):
            self.assertIsNone(gh(["api", "example"], allow_missing=True))

    def test_pending_notes_and_reports_inside_immutable_directory_are_refused(self):
        self.notes.write_text("**Release verification status: pending.**")
        with self.assertRaisesRegex(PublicationError, "pending verification"):
            self.publish()
        self.notes.write_text("Complete.")
        self.report = self.directory / "publication.json"
        with self.assertRaisesRegex(PublicationError, "outside the immutable"):
            self.publish()
        self.assertEqual(self.github.calls, [])

    def test_existing_report_is_not_overwritten(self):
        self.report.write_text("original")
        with self.assertRaisesRegex(PublicationError, "already exists"):
            self.publish()
        self.assertEqual(self.report.read_text(), "original")
        self.assertEqual(self.github.calls, [])


if __name__ == "__main__":
    unittest.main()
