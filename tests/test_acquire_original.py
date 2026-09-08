"""Public reference reports must not persist transient downloader credentials."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.acquire_original import main as acquire_main, summarize_download_failure
from tools.probe_references import main as probe_main, public_metadata


class ReferenceDiagnosticTests(unittest.TestCase):
    def test_signed_urls_headers_cookie_values_and_paths_are_not_retained(self):
        raw = ("ERROR: HTTP Error 403: Forbidden https://cdn.example/video?Signature=SECRET_VALUE&Expires=123 "
               "Authorization: Bearer TOP_SECRET Cookie: session=COOKIE_SECRET /tmp/private/FILENAME_SECRET")
        result = summarize_download_failure(raw, 1)
        output = json.dumps(result)
        self.assertEqual(result["httpStatus"], 403)
        self.assertEqual(result["failureCategory"], "access_restricted")
        self.assertFalse(result["rawDiagnosticsIncluded"])
        for value in ("SECRET", "https://", "cdn.example", "Authorization", "/tmp/private"):
            self.assertNotIn(value, output)

    def test_fixed_categories_and_numeric_bounds(self):
        examples = {
            "No module named yt_dlp": "missing_dependency",
            "HTTP Error 429 Too Many Requests": "rate_limited",
            "Sign in to confirm you are not a bot": "authentication_or_challenge",
            "HTTP/1.1 404 Not Found": "source_unavailable",
            "HTTP Error 503": "upstream_error",
            "Connection timed out": "network_error",
            "Requested format is not available": "extractor_or_format_error",
            "No space left on device": "local_storage_error",
            "Unknown failure SECRET_VALUE": "download_failed",
        }
        for diagnostic, category in examples.items():
            with self.subTest(diagnostic=diagnostic):
                result = summarize_download_failure(diagnostic, 99999)
                self.assertEqual(result["failureCategory"], category)
                self.assertNotIn("downloaderExitCode", result)
                self.assertNotIn("SECRET_VALUE", json.dumps(result))

    def test_acquisition_failure_report_uses_safe_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments = ["acquire_original.py", "--work-dir", str(root / "work"), "--archive", str(root / "movie.bapple"),
                         "--report-dir", str(root / "reports")]
            failure = subprocess.CompletedProcess([], 1, "", "HTTP Error 403 https://cdn.invalid/?secret=PRIVATE_SIGNATURE")
            with mock.patch("sys.argv", arguments), mock.patch("tools.acquire_original.subprocess.run", return_value=failure), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(acquire_main(), 0)
            report = (root / "reports/reference-acquisition.json").read_text()
            self.assertNotIn("PRIVATE_SIGNATURE", report)
            self.assertNotIn("cdn.invalid", report)
            self.assertEqual(json.loads(report)["failureCategory"], "access_restricted")

    def test_metadata_filter_keeps_canonical_urls_and_excludes_signed_urls(self):
        result = public_metadata({"title": "Public title", "width": 512, "height": 384, "duration": 219.1,
                                  "webpage_url": "https://cdn.invalid/?signature=SECRET",
                                  "channel_url": "https://www.youtube.com/channel/UCPublic?token=SECRET",
                                  "formats": [{"url": "https://cdn.invalid/SECRET"}], "cookies": "SECRET"},
                                 "https://www.nicovideo.jp/watch/sm8628149")
        self.assertEqual(result["width"], 512)
        self.assertEqual(result["webpage_url"], "https://www.nicovideo.jp/watch/sm8628149")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("channel_url", result)

    def test_probe_does_not_serialize_exception_strings_or_raw_downloader_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "reference-probe.json"
            responses = [subprocess.CompletedProcess([], 0, "2026.09.01\n", ""),
                         subprocess.CompletedProcess([], 1, "", "HTTP Error 403 https://cdn.invalid/?token=SECRET_ONE"),
                         OSError("Cannot open /secret/SECRET_TWO"),
                         subprocess.CompletedProcess([], 0, "not json SECRET_THREE", "")]
            with mock.patch("sys.argv", ["probe_references.py", "--report", str(report_path)]), \
                    mock.patch("tools.probe_references.subprocess.run", side_effect=responses), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                probe_main()
            encoded = report_path.read_text()
            report = json.loads(encoded)
            self.assertNotIn("SECRET", encoded)
            self.assertEqual([item["failureCategory"] for item in report["references"]],
                             ["access_restricted", "downloader_unavailable", "invalid_metadata"])


if __name__ == "__main__":
    unittest.main()
