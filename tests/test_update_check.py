import json
import os
import importlib.util
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


APP_PATH = Path(__file__).resolve().parents[1] / "智能解压V1.0.py"
APP_SPEC = importlib.util.spec_from_file_location("smart_unzip_app", APP_PATH)
APP_MODULE = importlib.util.module_from_spec(APP_SPEC)
APP_SPEC.loader.exec_module(APP_MODULE)


class ReleaseVersionTests(unittest.TestCase):
    def test_parses_numeric_tags_with_optional_v_prefix(self):
        self.assertEqual(APP_MODULE.DecompressApp.parse_release_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(APP_MODULE.DecompressApp.parse_release_version("1.2.3"), (1, 2, 3))

    def test_normalizes_trailing_zero_components(self):
        self.assertEqual(APP_MODULE.DecompressApp.parse_release_version("v1.0.0"), (1,))
        self.assertEqual(APP_MODULE.DecompressApp.is_newer_release("1.0", "v1.0.0"), False)

    def test_detects_newer_equal_and_older_versions(self):
        compare = APP_MODULE.DecompressApp.is_newer_release
        self.assertTrue(compare("1.0", "v1.1"))
        self.assertFalse(compare("1.1", "v1.1"))
        self.assertFalse(compare("1.2", "v1.1"))

    def test_rejects_non_numeric_release_tags(self):
        parse = APP_MODULE.DecompressApp.parse_release_version
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("v1.1-beta"))
        self.assertIsNone(parse("1..2"))

    def test_release_page_must_be_https_on_the_expected_repository(self):
        validate = APP_MODULE.DecompressApp.is_valid_release_url
        self.assertTrue(validate("https://github.com/lehuyt/smart-unzip-workbench/releases/tag/v1.1"))
        self.assertFalse(validate("http://github.com/lehuyt/smart-unzip-workbench/releases/tag/v1.1"))
        self.assertFalse(validate("https://github.com.evil.example/lehuyt/smart-unzip-workbench/releases/tag/v1.1"))
        self.assertFalse(validate("https://example.com/releases/tag/v1.1"))


class UpdateCheckAsyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.qt_app = QApplication.instance() or QApplication([])

    def run_local_check(self, status=200, payload=None, raw_body=None, delay=0, timeout=10000):
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtNetwork import QNetworkAccessManager
        from PySide6.QtWidgets import QDialog, QLabel, QPushButton

        response_body = raw_body
        if response_body is None:
            response_body = json.dumps(payload or {}).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                try:
                    self.wfile.write(response_body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server_thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        dialog = QDialog()
        manager = QNetworkAccessManager(dialog)
        button = QPushButton("检查更新", dialog)
        status_label = QLabel("", dialog)
        checker = APP_MODULE.DecompressApp.__new__(APP_MODULE.DecompressApp)
        from PySide6.QtCore import QObject

        QObject.__init__(checker)
        checker.colors = {"muted": "#6F7975", "success": "#249B7F", "danger": "#D65353"}
        checker.LATEST_RELEASE_API = f"http://127.0.0.1:{server.server_port}/latest"
        checker.UPDATE_REQUEST_TIMEOUT_MS = timeout
        opened_urls = []
        checker.open_release_page = lambda url: opened_urls.append(url.toString()) or True

        loop = QEventLoop()
        manager.finished.connect(loop.quit)
        checker.check_for_updates(dialog, manager, button, status_label)
        self.assertFalse(button.isEnabled())
        QTimer.singleShot(2000, loop.quit)
        loop.exec()
        self.qt_app.processEvents()
        result = (status_label.text(), button.isEnabled(), opened_urls)
        dialog.close()
        return result

    def test_no_update_keeps_settings_responsive(self):
        status, enabled, opened = self.run_local_check(
            payload={
                "tag_name": "v1.1.0",
                "html_url": "https://github.com/lehuyt/smart-unzip-workbench/releases/tag/v1.1.0",
            }
        )
        self.assertIn("当前已是最新版本", status)
        self.assertTrue(enabled)
        self.assertEqual(opened, [])

    def test_newer_release_opens_github_page(self):
        url = "https://github.com/lehuyt/smart-unzip-workbench/releases/tag/v1.2"
        status, enabled, opened = self.run_local_check(
            payload={"tag_name": "v1.2", "html_url": url}
        )
        self.assertIn("发现新版本 v1.2", status)
        self.assertTrue(enabled)
        self.assertEqual(opened, [url])

    def test_api_rate_limit_and_malformed_response_show_errors(self):
        limited_status, limited_enabled, _ = self.run_local_check(status=403, payload={})
        self.assertIn("请求受限", limited_status)
        self.assertTrue(limited_enabled)

        malformed_status, malformed_enabled, _ = self.run_local_check(raw_body=b"not-json")
        self.assertIn("无法识别", malformed_status)
        self.assertTrue(malformed_enabled)

    def test_network_timeout_shows_retry_message(self):
        status, enabled, _ = self.run_local_check(delay=0.3, timeout=40)
        self.assertIn("检查失败", status)
        self.assertTrue(enabled)


if __name__ == "__main__":
    unittest.main()
