"""Small read-only client for Codex app-server account rate limits."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from glob import glob


class AppServerError(Exception):
    pass


def _reader(stream, output):
    try:
        for line in stream:
            try:
                output.put(json.loads(line))
            except ValueError:
                continue
    finally:
        output.put(None)


def _response(output, request_id, timeout):
    while True:
        try:
            message = output.get(timeout=timeout)
        except queue.Empty:
            raise AppServerError("Codex app-server timed out") from None
        if message is None:
            raise AppServerError("Codex app-server exited before replying")
        if message.get("id") != request_id:
            continue
        if message.get("error"):
            raise AppServerError(str(message["error"]))
        return message.get("result") or {}


def read_rate_limits(timeout=15):
    """Return the native ``account/rateLimits/read`` result.

    A fresh short-lived app-server uses Codex's own authenticated transport, so
    it is not affected by the Cloudflare challenge seen by plain urllib calls.
    """
    executable = None
    if os.name == "nt":
        # Prefer the desktop-app installation over an executable with the same
        # name injected earlier in PATH.
        root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin")
        candidates = glob(os.path.join(root, "*", "codex.exe"))
        if candidates:
            executable = max(candidates, key=os.path.getmtime)
    if not executable:
        executable = shutil.which("codex")
    if not executable:
        raise AppServerError("codex executable not found")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=flags,
        )
    except OSError as exc:
        raise AppServerError(str(exc)) from None

    output = queue.Queue()
    thread = threading.Thread(target=_reader, args=(proc.stdout, output), daemon=True)
    thread.start()

    def send(message):
        try:
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AppServerError(str(exc)) from None

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "token-monitor", "version": "1.0"},
        }})
        _response(output, 1, timeout)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read", "params": None})
        return _response(output, 2, timeout)
    finally:
        try:
            proc.stdin.close()
        except (OSError, AttributeError):
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
