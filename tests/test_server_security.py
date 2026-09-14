import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from tokmon.server import make_handler


class FakeCollector:
    last_poll = 123

    def snapshot(self):
        return {"ok": True}


class ServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.pet_start = patch("tokmon.server.pet_start", return_value={"running": True})
        self.pet_stop = patch("tokmon.server.pet_stop", return_value={"running": False})
        self.pet_start.start()
        self.pet_stop.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeCollector()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.pet_stop.stop()
        self.pet_start.stop()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body
        conn.close()
        return result

    def test_security_headers_are_present(self):
        status, headers, _ = self.request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_cross_site_post_is_rejected(self):
        status, _, _ = self.request(
            "POST",
            "/api/pet/start",
            {"Host": f"127.0.0.1:{self.port}", "Origin": "http://localhost.evil.test"},
        )
        self.assertEqual(403, status)

    def test_same_origin_tailscale_proxy_post_is_allowed(self):
        status, _, _ = self.request(
            "POST",
            "/api/pet/stop",
            {"Host": "device.example.ts.net", "Origin": "https://device.example.ts.net"},
        )
        self.assertEqual(200, status)


if __name__ == "__main__":
    unittest.main()
