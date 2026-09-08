"""Probe exact public references without cookies, proxies, or challenge bypasses."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

REFERENCES = [
    ("original_animation", "https://www.nicovideo.jp/watch/sm8628149"),
    ("official_label", "https://www.youtube.com/watch?v=i41KoE0iMYU"),
    ("user_reference", "https://www.youtube.com/watch?v=FtutLA63Cp8"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    version = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    report = {"checkedAt": datetime.now(timezone.utc).isoformat(), "downloaderVersion": version,
              "mediaDownloaded": False, "frameIdentityVerified": False, "references": []}
    for label, url in REFERENCES:
        item = {"name": label, "url": url, "status": "unavailable"}
        command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
                   "--socket-timeout", "15", "--retries", "0", "--extractor-retries", "0",
                   "--skip-download", "--dump-single-json", "--no-warnings", url]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                item.update(status="metadata_available", metadata={key: data.get(key) for key in
                    ("id", "title", "uploader", "channel", "channel_id", "channel_url", "upload_date", "duration", "width", "height", "fps", "webpage_url")})
            else:
                item["reason"] = result.stderr[-5000:]
        except subprocess.TimeoutExpired:
            item["reason"] = "Public metadata request exceeded 90 seconds; stopped without bypass attempts."
        except (OSError, ValueError) as error:
            item["reason"] = str(error)
        report["references"].append(item)
        print(f"{label}: {item['status']}", flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
