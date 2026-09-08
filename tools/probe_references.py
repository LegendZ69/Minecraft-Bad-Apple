"""Probe exact public references without cookies, proxies, or challenge bypasses."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.acquire_original import summarize_download_failure

REFERENCES = [
    ("original_animation", "https://www.nicovideo.jp/watch/sm8628149"),
    ("official_label", "https://www.youtube.com/watch?v=i41KoE0iMYU"),
    ("user_reference", "https://www.youtube.com/watch?v=FtutLA63Cp8"),
]


def public_metadata(data: dict, source_url: str) -> dict:
    """Keep bounded public fields, never raw media formats or signed CDN URLs."""
    if not isinstance(data, dict):
        raise ValueError("Metadata is not an object")
    selected = {"webpage_url": source_url}
    for key in ("id", "title", "uploader", "channel", "channel_id", "upload_date"):
        value = data.get(key)
        if isinstance(value, str):
            # A public text field should not become a back door for persisting a
            # downloader URL. Its exact title is not a media-identity assertion.
            value = re.sub(r"(?i)\b(?:https?|ftp)://\S+", "[URL omitted]", value)
            selected[key] = "".join(character for character in value if character >= " " and character != "\x7f")[:512]
    for key in ("duration", "width", "height", "fps"):
        value = data.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1_000_000:
            selected[key] = value
    channel_url = data.get("channel_url")
    if isinstance(channel_url, str):
        parsed = urlsplit(channel_url)
        if (parsed.scheme == "https" and parsed.netloc in ("www.youtube.com", "youtube.com")
                and not parsed.query and not parsed.fragment
                and re.fullmatch(r"/channel/[A-Za-z0-9_-]{1,128}", parsed.path)):
            selected["channel_url"] = channel_url
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    version = None
    try:
        result = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True, timeout=15)
        candidate = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}[A-Za-z0-9.+_-]{0,64}", candidate):
            version = candidate
    except (OSError, subprocess.TimeoutExpired):
        pass
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
                item.update(status="metadata_available", metadata=public_metadata(data, url))
            else:
                item.update(summarize_download_failure(result.stderr, result.returncode))
        except subprocess.TimeoutExpired:
            item.update(failureCategory="request_timeout", rawDiagnosticsIncluded=False,
                        reason="Public metadata request exceeded 90 seconds; stopped without bypass attempts.")
        except OSError:
            item.update(failureCategory="downloader_unavailable", rawDiagnosticsIncluded=False,
                        reason="The configured downloader could not be started; raw operating-system diagnostics were omitted.")
        except ValueError:
            item.update(failureCategory="invalid_metadata", rawDiagnosticsIncluded=False,
                        reason="The downloader returned invalid metadata; raw output was omitted.")
        report["references"].append(item)
        print(f"{label}: {item['status']}", flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
