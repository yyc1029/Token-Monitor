"""Incremental JSONL tailing for append-only transcript files."""

from __future__ import annotations

import json
import os
import time


class JsonlTailer:
    """Tracks a byte offset per file and yields newly appended JSON objects.

    Handles the two things that actually happen on Windows with these logs:
    a file grows (read the tail) or a file is replaced/truncated (start over).
    Partial trailing lines are buffered until the writer finishes them.
    """

    def __init__(self, roots, suffix=".jsonl", max_age_days=None):
        self.roots = [r for r in roots]
        self.suffix = suffix
        self.max_age_days = max_age_days
        self._offsets: dict[str, int] = {}
        self._partial: dict[str, str] = {}

    def discover(self):
        cutoff = None
        if self.max_age_days is not None:
            cutoff = time.time() - self.max_age_days * 86400
        found = []
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if not name.endswith(self.suffix):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    # Never skip a file we are already mid-way through.
                    if cutoff and st.st_mtime < cutoff and path not in self._offsets:
                        continue
                    found.append((path, st.st_size, st.st_mtime))
        return found

    def poll(self):
        """Yield (path, mtime, obj) for every object appended since last poll."""
        for path, size, mtime in self.discover():
            offset = self._offsets.get(path, 0)
            if size < offset:  # truncated or replaced
                offset = 0
                self._partial.pop(path, None)
            if size == offset:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read(size - offset)
            except OSError:
                continue
            self._offsets[path] = offset + len(chunk)
            text = self._partial.pop(path, "") + chunk.decode("utf-8", "replace")
            lines = text.split("\n")
            if not text.endswith("\n"):
                self._partial[path] = lines.pop()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                yield path, mtime, obj

    def seed_to_end(self):
        """Mark every current file as fully read (used for a cold fast start)."""
        for path, size, _ in self.discover():
            self._offsets[path] = size
