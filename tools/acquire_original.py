"""Retrieve the canonical creator upload for ephemeral cloud verification only.

No account cookies, alternate proxies, or challenge bypasses are used. Access
failures are reported separately from conversion or fidelity-check failures.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

URL = "https://www.nicovideo.jp/watch/sm8628149"


def summarize_download_failure(stderr: str | None, returncode: int | None = None) -> dict:
    """Classify diagnostics without retaining raw URLs, headers, paths or tokens.

    Redacting a fixed set of query parameters is insufficient: signed CDN URLs
    use changing parameter names. Only fixed category messages and bounded
    numeric facts cross from downloader diagnostics into durable reports.
    """
    original = stderr or ""
    text = (original[:16_384] + "\n" + original[-16_384:]).lower()
    match = re.search(r"\bhttp(?:\s+error|/[12](?:\.\d)?|\s+status)?\s*[:=]?\s*([1-5]\d{2})\b", text)
    status = int(match.group(1)) if match else None
    if "no module named yt_dlp" in text or "ffmpeg is not installed" in text or "ffprobe is not installed" in text:
        category, reason = "missing_dependency", "A required downloader or media dependency is unavailable."
    elif status == 429 or "too many requests" in text or "rate limit" in text:
        category, reason = "rate_limited", "The public source rate-limited this request; no bypass was attempted."
    elif status == 401 or any(value in text for value in ("sign in", "sign-in", "login required", "log in", "captcha", "not a bot")):
        category, reason = "authentication_or_challenge", "The public source requested authentication or a challenge; no credentials or bypass were used."
    elif status in (403, 451) or any(value in text for value in ("geo-restricted", "not available in your country", "access denied", "forbidden")):
        category, reason = "access_restricted", "The public source or media endpoint denied access; no bypass was attempted."
    elif status in (404, 410) or any(value in text for value in ("video unavailable", "video is unavailable", "private video", "has been removed", "has been deleted")):
        category, reason = "source_unavailable", "The requested public source or media resource was unavailable."
    elif status is not None and status >= 500:
        category, reason = "upstream_error", "The public source reported an upstream service error."
    elif any(value in text for value in ("timed out", "timeout", "name resolution", "network is unreachable", "connection reset", "connection refused", "certificate verify failed")):
        category, reason = "network_error", "The public request failed because of a connection, DNS, timeout or certificate error."
    elif any(value in text for value in ("requested format is not available", "unable to extract", "no video formats")):
        category, reason = "extractor_or_format_error", "The downloader could not extract the requested public metadata or media format."
    elif "no space left" in text or "permission denied" in text:
        category, reason = "local_storage_error", "The downloader could not write to its isolated working directory."
    else:
        category, reason = "download_failed", "The public-source downloader failed; raw diagnostics were omitted to avoid retaining sensitive URLs or credentials."
    result = {"failureCategory": category, "reason": reason, "rawDiagnosticsIncluded": False,
              "diagnosticCharacters": min(len(original), 10_000_000)}
    if status is not None:
        result["httpStatus"] = status
    if type(returncode) is int and -255 <= returncode <= 255:
        result["downloaderExitCode"] = returncode
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = {"sourceUrl": URL, "checkedAt": datetime.now(timezone.utc).isoformat(),
              "sourceRole": "Original creator's NicoVideo animation, not the user's YouTube reupload",
              "publicRedistributionPermissionEstablished": False, "mediaBundledInRelease": False,
              "youtubeReferenceEquivalenceVerified": False, "status": "access_unavailable"}
    command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
               "--socket-timeout", "15", "--retries", "0", "--fragment-retries", "0",
               "--extractor-retries", "0", "--write-info-json", "--format", "bv*+ba/b",
               "--merge-output-format", "mkv", "--output", str(args.work_dir.resolve() / "original.%(ext)s"),
               "--print", "after_move:filepath", URL]
    result = None
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=240)
        if result.returncode:
            report.update(summarize_download_failure(result.stderr, result.returncode))
    except subprocess.TimeoutExpired:
        report.update(failureCategory="request_timeout", rawDiagnosticsIncluded=False,
                      reason="Public source download exceeded 240 seconds; stopped without bypass attempts.")
    except OSError:
        report.update(failureCategory="downloader_unavailable", rawDiagnosticsIncluded=False,
                      reason="The configured downloader could not be started; raw operating-system diagnostics were omitted.")
    if result is None or result.returncode:
        (args.report_dir / "reference-acquisition.json").write_text(json.dumps(report, indent=2) + "\n")
        print("Original reference unavailable; explicit evidence recorded. Synthetic QA remains required.")
        return 0
    source = Path(result.stdout.strip().splitlines()[-1])
    if not source.is_file() or source.resolve().parent != args.work_dir.resolve():
        raise RuntimeError("Downloader did not produce the expected isolated local source")
    root = Path(__file__).resolve().parent
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    conversion = [sys.executable, str(root / "prepare_video.py"), str(source), "--output", str(args.archive),
                  "--source-url", URL]
    info = args.work_dir / "original.info.json"
    if info.is_file():
        conversion.extend(["--source-info", str(info)])
    subprocess.run(conversion, check=True, timeout=600)
    verification = args.report_dir / "original-source-comparison.json"
    subprocess.run([sys.executable, str(root / "verify_archive.py"), str(args.archive), "--source", str(source),
                    "--report", str(verification)], check=True, timeout=600)
    data = json.loads(verification.read_text())
    report.update(status="downloaded_converted_verified", comparisonReport=verification.name,
                  sourceSha256=data.get("sourceSha256", data.get("sourceComparison", {}).get("sourceSha256")))
    (args.report_dir / "reference-acquisition.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Canonical original downloaded, converted and exhaustively verified. Media remains ephemeral.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
