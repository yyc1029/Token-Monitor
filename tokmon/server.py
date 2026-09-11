"""Tiny stdlib HTTP server: serves the dashboard and /api/snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .collector import Collector, load_config
from .procs import pet_start, pet_status, pet_stop

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_handler(collector: Collector):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TokenMonitor/1.0"

        def log_message(self, fmt, *args):  # quiet by default
            if os.environ.get("TOKMON_DEBUG"):
                super().log_message(fmt, *args)

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status=200):
            return self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/snapshot":
                snap = collector.snapshot()
                snap["pet"] = pet_status()
                return self._json(snap)
            if path == "/api/health":
                return self._json({"ok": True, "last_poll": collector.last_poll})
            if path == "/api/pet":
                return self._json(pet_status())
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            # loopback-only server, but still refuse cross-site form posts
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                return self._send(403, b"forbidden", "text/plain")
            if path == "/api/pet/start":
                return self._json(pet_start())
            if path == "/api/pet/stop":
                return self._json(pet_stop())
            return self._send(404, b"not found", "text/plain")

        def _static(self, name, ctype):
            full = os.path.join(WEB_DIR, name)
            try:
                with open(full, "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            except OSError:
                return self._send(404, b"missing asset", "text/plain")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="Claude / Codex usage monitor")
    ap.add_argument("--config", default=os.path.join(PROJECT_DIR, "config.json"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    host = args.host or cfg["host"]
    port = args.port or int(cfg["port"])

    # On Windows SO_REUSEADDR lets a second instance bind the same port and the
    # two then share incoming connections; refuse instead so restarts are clean.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(collector := Collector(cfg)))
    except OSError as exc:
        print(f"Token Monitor is already running on {host}:{port} ({exc}); use stop.bat first.", file=sys.stderr)
        return 1
    collector.start()
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"Token Monitor listening on {url}", flush=True)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
