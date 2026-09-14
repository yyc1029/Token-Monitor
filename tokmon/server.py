"""Tiny stdlib HTTP server: serves the dashboard and /api/snapshot."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .collector import Collector, load_config
from .discord_bot import DiscordService
from .procs import pet_start, pet_status, pet_stop

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TokenMonitor", "server.log")


def setup_logging():
    """pythonw has no console, so the only record of why a live query failed is
    this file: 3 x 512 KB, rotated. TOKMON_DEBUG=1 also mirrors it to stderr."""
    root = logging.getLogger("tokmon")
    root.setLevel(logging.DEBUG if os.environ.get("TOKMON_DEBUG") else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if os.environ.get("TOKMON_DEBUG"):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


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
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )
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
            if path == "/api/history":
                hist = collector.history.export()
                hist["now"] = collector.last_poll
                hist["error"] = collector.history_error
                return self._json(hist)
            if path == "/api/health":
                return self._json({"ok": True, "last_poll": collector.last_poll})
            if path == "/api/pet":
                return self._json(pet_status())
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            # The service is loopback-only and may be reached through a
            # Tailscale reverse proxy. Allow exact same-origin requests for
            # either route, and reject lookalikes such as localhost.evil.test.
            origin = self.headers.get("Origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != self.headers.get("Host", "").lower():
                        return self._send(403, b"forbidden", "text/plain")
                except ValueError:
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

    setup_logging()
    log = logging.getLogger("tokmon.server")
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
        log.warning("port %s:%s busy, not starting (%s)", host, port, exc)
        return 1
    log.info("starting on %s:%s (live intervals: %s)", host, port, collector.live_interval)
    collector.start()
    discord = DiscordService(collector, cfg.get("discord"))
    discord.start()
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
        log.info("stopping")
        discord.stop()
        collector.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
