"""Retrieve the canonical creator upload for ephemeral cloud verification only.

No account cookies, alternate proxies, or challenge bypasses are used. Access
failures are reported separately from conversion or fidelity-check failures.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

URL = "https://www.nicovideo.jp/watch/sm8628149"


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
            report["reason"] = result.stderr[-5000:]
    except subprocess.TimeoutExpired:
        report["reason"] = "Public source download exceeded 240 seconds; stopped without bypass attempts."
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
