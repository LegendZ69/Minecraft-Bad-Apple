#!/usr/bin/env python3
"""Publish a verified release through the already-authenticated GitHub CLI.

New releases stay drafts until every uploaded asset has been downloaded and
checked. Published releases are immutable to this script: reruns only verify
their tag and exact asset bytes. Failed draft uploads can resume without ever
overwriting an existing asset. Authentication is delegated to gh; this program
does not read, discover, print, or transform credentials.

CLI contract: https://cli.github.com/manual/gh_release_create
              https://cli.github.com/manual/gh_release_edit
              https://cli.github.com/manual/gh_release_upload
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.package_release import ReleaseError, digest_bytes, sha256, verify_directory


REPOSITORY = "LegendZ69/Minecraft-Bad-Apple"
API_ROOT = f"repos/{REPOSITORY}"
SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class PublicationError(Exception):
    """Publishing stopped without destructive recovery or replacement."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PublicationError(message)


def gh(arguments: list[str], *, allow_missing: bool = False) -> str | None:
    """Do not log command output or environment; only genuine HTTP 404 is absent."""
    try:
        result = subprocess.run(["gh", *arguments], capture_output=True, text=True,
                                check=False, timeout=300)
    except FileNotFoundError as exc:
        raise PublicationError("GitHub CLI (gh) is required and must already be authenticated.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PublicationError("GitHub operation timed out; inspect the draft before retrying.") from exc
    if result.returncode:
        code = re.search(r"\(HTTP ([1-5][0-9]{2})\)", result.stderr or "")
        if allow_missing and code and code.group(1) == "404":
            return None
        detail = f"HTTP {code.group(1)}" if code else f"exit {result.returncode}"
        # Raw diagnostics can contain authentication information. Preserve only
        # the status; auth/permission errors must never trigger creation retries.
        raise PublicationError(f"GitHub operation failed ({detail}); authentication and permission errors are not missing releases.")
    return result.stdout


def api(endpoint: str, *, allow_missing: bool = False):
    output = gh(["api", endpoint, "--hostname", "github.com", "-H", "Accept: application/vnd.github+json",
                 "-H", "X-GitHub-Api-Version: 2022-11-28"], allow_missing=allow_missing)
    if output is None:
        return None
    try:
        return json.loads(output)
    except (TypeError, ValueError) as exc:
        raise PublicationError("GitHub returned malformed JSON.") from exc


def find_release(tag: str) -> dict | None:
    release = api(f"{API_ROOT}/releases/tags/{tag}", allow_missing=True)
    if release is not None:
        require(isinstance(release, dict), "GitHub release response is not an object.")
        return release
    # Some API/CLI versions do not resolve unpublished drafts by tag. List
    # accessible drafts after an actual 404; do not mistake one for a new release.
    for page in range(1, 21):
        releases = api(f"{API_ROOT}/releases?per_page=100&page={page}")
        require(isinstance(releases, list), "GitHub release list is malformed.")
        matches = [item for item in releases if isinstance(item, dict) and item.get("tag_name") == tag]
        require(len(matches) <= 1, "Multiple releases use the requested tag; manual inspection is required.")
        if matches:
            return matches[0]
        if len(releases) < 100:
            return None
    raise PublicationError("Release discovery exceeded its bound; no release was created.")


def tag_commit(tag: str, *, allow_missing: bool = False) -> str | None:
    reference = api(f"{API_ROOT}/git/ref/tags/{tag}", allow_missing=allow_missing)
    if reference is None:
        return None
    require(isinstance(reference, dict) and reference.get("ref") == f"refs/tags/{tag}", "GitHub returned a different tag reference.")
    obj = reference.get("object")
    visited = set()
    for _ in range(9):
        require(isinstance(obj, dict), "Tag reference has no Git object.")
        digest = obj.get("sha")
        require(isinstance(digest, str) and bool(SHA_PATTERN.fullmatch(digest)), "Tag object SHA is invalid.")
        digest = digest.lower()
        require(digest not in visited, "Annotated tag cycle detected.")
        visited.add(digest)
        if obj.get("type") == "commit":
            return digest
        require(obj.get("type") == "tag", "Release tag does not resolve to a commit.")
        annotated = api(f"{API_ROOT}/git/tags/{digest}")
        require(isinstance(annotated, dict), "Annotated tag response is malformed.")
        obj = annotated.get("object")
    raise PublicationError("Annotated tag nesting is too deep.")


def check_tag(tag: str, commit: str, *, allow_missing: bool = False) -> None:
    actual = tag_commit(tag, allow_missing=allow_missing)
    require(actual is None and allow_missing or actual == commit, "Release tag points to a different commit; it will not be moved.")


def notes_text(path: Path) -> str:
    require(path.is_file() and not path.is_symlink(), "Release notes must be a regular file.")
    require(path.stat().st_size <= 1024 * 1024, "Release notes are oversized.")
    notes = path.read_text(encoding="utf-8")
    require(bool(notes.strip()), "Release notes are empty.")
    require(not re.search(r"(?im)(?:verification.{0,80}\bpending\b|\bpending\b.{0,80}verification|\|\s*pending\s*\|)", notes),
            "Release notes still contain a pending verification status.")
    return notes


def normalize_notes(value: str) -> str:
    return value.replace("\r\n", "\n").rstrip("\n")


def check_release(release: dict, tag: str, commit: str, notes: str, *, require_draft: bool | None = None) -> None:
    require(release.get("tag_name") == tag, "Release tag name differs from the requested version.")
    require(type(release.get("draft")) is bool, "Release draft state is missing.")
    require(release.get("prerelease") is False, "A prerelease cannot stand in for this stable version.")
    if require_draft is not None:
        require(release["draft"] is require_draft, "Release draft state changed unexpectedly.")
    if release["draft"]:
        require(release.get("target_commitish") == commit, "Existing draft targets a different commit; it will not be changed.")
        require(isinstance(release.get("body"), str) and normalize_notes(release["body"]) == normalize_notes(notes),
                "Existing draft notes differ; they will not be overwritten.")


def remote_assets(release: dict, local_digests: dict[str, str], *, complete: bool) -> set[str]:
    assets = release.get("assets")
    require(isinstance(assets, list), "Release asset list is missing.")
    names = set()
    for asset in assets:
        require(isinstance(asset, dict), "Release asset metadata is malformed.")
        name = asset.get("name")
        require(isinstance(name, str) and name in local_digests and name not in names,
                "Remote release has an unexpected or duplicate asset; nothing will be overwritten.")
        require(asset.get("state") == "uploaded", "Remote asset upload is incomplete; manual inspection is required.")
        names.add(name)
        if asset.get("digest"):
            require(asset["digest"] == "sha256:" + local_digests[name], f"Remote asset digest differs: {name}")
    if complete:
        require(names == set(local_digests), "Published or completed draft release has missing assets.")
    return names


def download_verify(tag: str, local: Path, digests: dict[str, str], *, selected: set[str] | None = None) -> None:
    """Download into an isolated empty directory; never use --clobber."""
    with tempfile.TemporaryDirectory(prefix="badapple-release-download-") as temporary:
        downloaded = Path(temporary)
        gh(["release", "download", tag, "--repo", REPOSITORY, "--dir", str(downloaded)])
        expected = set(digests) if selected is None else selected
        require({path.name for path in downloaded.iterdir()} == expected, "Downloaded asset set differs from the remote/local release.")
        for name in sorted(expected):
            file = downloaded / name
            require(file.is_file() and not file.is_symlink(), f"Downloaded asset is not a regular file: {name}")
            require(sha256(file) == digests[name], f"Downloaded asset SHA-256 differs: {name}")
        if selected is None:
            verify_directory(downloaded)
            require((downloaded / "SHA256SUMS").read_bytes() == (local / "SHA256SUMS").read_bytes(),
                    "Downloaded SHA256SUMS differs from the local immutable release.")


def publish_release(*, directory: Path, notes: Path, report: Path) -> dict:
    verified = verify_directory(directory)
    version, commit = verified["version"], verified["commit"]
    require(isinstance(commit, str) and bool(SHA_PATTERN.fullmatch(commit)), "Build provenance must contain a full Git commit SHA.")
    commit = commit.lower()
    tag = "v" + version
    body = notes_text(notes)
    require(not report.exists() and not report.is_symlink(), "Publication report already exists; choose a new report path.")
    require(directory.resolve() not in report.resolve().parents, "Publication report must be outside the immutable release directory.")
    repo = api(API_ROOT)
    require(isinstance(repo, dict) and repo.get("full_name", "").lower() == REPOSITORY.lower(), "GitHub repository identity does not match.")
    target = api(f"{API_ROOT}/commits/{commit}")
    require(isinstance(target, dict) and target.get("sha", "").lower() == commit, "The exact source commit is not available on GitHub.")
    check_tag(tag, commit, allow_missing=True)
    existing = find_release(tag)
    previously_published = existing is not None and existing.get("draft") is False
    if existing is not None:
        check_release(existing, tag, commit, body)
    # Repository permissions.push is not an installation token's release
    # capability. The release API requires Contents:write and enforces any
    # additional workflow restrictions using the already-configured credentials:
    # https://docs.github.com/en/rest/releases/releases#create-a-release
    # Let that endpoint decide; gh() still stops on every mutation failure.

    with tempfile.TemporaryDirectory(prefix="badapple-publish-snapshot-") as temporary:
        snapshot = Path(temporary) / "assets"
        snapshot.mkdir()
        for path in sorted(directory.iterdir()):
            shutil.copyfile(path, snapshot / path.name)
        require(verify_directory(snapshot) == verified, "Release changed during publication snapshot.")
        digests = {path.name: sha256(path) for path in sorted(snapshot.iterdir())}
        snapshot_notes = Path(temporary) / "release-notes.md"
        snapshot_notes.write_text(body, encoding="utf-8", newline="\n")

        if existing is None:
            # Latest status is assigned only when publishing, never to a draft.
            gh(["release", "create", tag, "--repo", REPOSITORY, "--draft", "--target", commit,
                "--title", f"Minecraft Bad Apple {tag}", "--notes-file", str(snapshot_notes)])
            existing = find_release(tag)
            require(existing is not None, "Created draft cannot be found; stopped before uploading.")
            check_release(existing, tag, commit, body, require_draft=True)

        if previously_published:
            remote_assets(existing, digests, complete=True)
            check_tag(tag, commit)
            download_verify(tag, snapshot, digests)
        else:
            present = remote_assets(existing, digests, complete=False)
            if present:
                download_verify(tag, snapshot, digests, selected=present)
            missing = sorted(set(digests) - present)
            if missing:
                gh(["release", "upload", tag, "--repo", REPOSITORY,
                    *[str(snapshot / name) for name in missing]])
            staged = find_release(tag)
            require(staged is not None, "Draft disappeared during upload.")
            check_release(staged, tag, commit, body, require_draft=True)
            remote_assets(staged, digests, complete=True)
            download_verify(tag, snapshot, digests)
            check_tag(tag, commit, allow_missing=True)
            # A concurrent edit must not change the verified draft's identity.
            staged_again = find_release(tag)
            require(staged_again is not None and staged_again.get("id") == staged.get("id"), "Draft identity changed before publication.")
            check_release(staged_again, tag, commit, body, require_draft=True)
            remote_assets(staged_again, digests, complete=True)
            gh(["release", "edit", tag, "--repo", REPOSITORY, "--draft=false",
                "--latest=false" if version == "1.0.0" else "--latest"])

        published = find_release(tag)
        require(published is not None, "Published release cannot be found.")
        check_release(published, tag, commit, body, require_draft=False)
        remote_assets(published, digests, complete=True)
        check_tag(tag, commit)
        download_verify(tag, snapshot, digests)
        url = published.get("html_url")
        expected_url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
        require(url == expected_url, "Published release URL differs from the expected repository/tag.")
        published_body = published.get("body")
        require(isinstance(published_body, str), "Published release notes are missing.")
        notes_match = normalize_notes(published_body) == normalize_notes(body)
        if not previously_published:
            require(notes_match, "Published notes changed after draft verification; they will not be overwritten.")
        result = {
            "schemaVersion": 1, "status": "published_verified", "repository": REPOSITORY,
            "version": version, "tag": tag, "commit": commit, "url": url,
            "verifiedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "alreadyPublished": previously_published,
            "draftAssetsDownloadedAndVerifiedBeforePublishing": not previously_published,
            "publishedAssetsDownloadedAndVerified": True,
            "assets": [{"name": name, "sha256": digest} for name, digest in sorted(digests.items())],
            "requestedNotesSha256": sha256(snapshot_notes),
            "publishedNotesSha256": digest_bytes(published_body.encode("utf-8")),
            "publishedNotesMatchRequested": notes_match,
        }
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = publish_release(directory=args.directory, notes=args.notes, report=args.report)
    except (PublicationError, ReleaseError, OSError, ValueError) as exc:
        print(f"Release publication stopped: {exc}", file=sys.stderr)
        print("No release assets were overwritten or deleted; inspect any existing draft before retrying.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
