"""Desktop pet: the "用量小貓" sticker + a usage card, always on top.

Visual spec comes from the Claude Design artboard "Desktop Pet.dc.html":
cream card (#fffef9) with a 3px dark-green (#2d4a3e) outline and an offset
shadow (#cfdac4); Codex rows in green (#a8d5a2), Claude rows in peach
(#f0b08a); Baloo 2 for headings, JetBrains Mono for numbers; the cat bobs.

Rendering: everything is drawn with GDI+ (anti-aliased, per-pixel alpha) into
a bitmap that is pushed to the window with UpdateLayeredWindow - see gdip.py.
Tk only provides the window and mouse events.  The process is per-monitor DPI
aware and scales every dimension by the real DPI.

States follow the artboard: idle 0-50 %, working 50-80 %, warning 80-95 %,
limit 95-100 %.  Each source shows its own state pill; the cat sticker shows
the worse of the two.  Stickers live in tokmon/assets/cat-<state>.png; a
missing one falls back to cat-idle.png, and if that is missing too the pet is
drawn with primitives so the widget still works.

Reads /api/snapshot from the local server (starts it if it is not running).
Writes its pid to %LOCALAPPDATA%\\TokenMonitor\\pet.pid so the dashboard can
show / start / stop it.
"""

from __future__ import annotations

import atexit
import ctypes
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser

from . import gdip
from .collector import load_config
from .procs import PET_PID_FILE, STATE_DIR, clear_pid, is_alive, read_pid, spawn_detached, write_pid

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FONT_DIR = os.path.join(ASSET_DIR, "fonts")
STATE_FILE = os.path.join(STATE_DIR, "pet.json")

# -- logical geometry at 96 dpi (artboard scaled ~0.53); multiplied by S at runtime
CAT_W = 170                        # sticker render width @ 1x
CARD_W, CARD_H = 268, 174          # card at rest
DETAIL_H = 82                      # extra card height when the details are open
BUBBLE_H = 96
CAT_X = 3
CARD_X = CAT_X + CAT_W + 10
CARD_Y = BUBBLE_H + 2
CAT_BOTTOM = CARD_Y + CARD_H + 4
W = CARD_X + CARD_W + 8
H = CARD_Y + CARD_H + DETAIL_H + 10
PET_CX = CAT_X + CAT_W // 2

# -- palette (from the artboard) ----------------------------------------------------
INK = "#2d4a3e"
CARD = "#fffef9"
SHADOW = "#cfdac4"
TRACK = "#eef1e9"
DIVIDER = "#e4ebdc"
MUTED = "#6f8175"
CODEX = "#a8d5a2"
CLAUDE = "#f0b08a"

SOURCES = (("codex", "Codex", CODEX), ("claude", "Claude Code", CLAUDE))
STATES = ("idle", "working", "warning", "limit")
THRESHOLDS = (50, 80, 95)           # state boundaries; alerts fire when crossing each
BUBBLE_SECONDS = 9

SAYINGS = {
    "idle": ["額度充足，先吃個冰淇淋。", "今天狀態不錯～", "慢慢來，還很夠用。"],
    "working": ["嗯，開始認真工作了。", "用了一半囉，留意一下。", "專心打字中…"],
    "warning": ["冰淇淋要融化了！慢一點！", "額度不多了，省著用。", "我有點緊張…"],
    "limit": ["冰淇淋吃完了… zzz", "額度見底，等重置吧。", "先睡一下，醒來就重置了。"],
    "sleep": ["兩個 CLI 都沒在跑，我先睡一下。", "沒有資料… zzz"],
}

HEAD_FONTS = ("Baloo 2", "Segoe UI")
MONO_FONTS = ("JetBrains Mono", "Consolas")
CJK_FONTS = ("Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI")


def state_of(p):
    if p is None:
        return "sleep"
    return "limit" if p >= 95 else "warning" if p >= 80 else "working" if p >= 50 else "idle"


def fmt_reset(sec):
    if sec is None:
        return "no data"
    if sec <= 0:
        return "reset now"
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"reset {d}d {h}h"
    if h:
        return f"reset {h}h {m:02d}m"
    return f"reset {m}m"


def fmt_dur(sec):
    if sec is None:
        return "—"
    if sec <= 0:
        return "已重置"
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}天{h}時"
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_ago(sec):
    if sec is None:
        return "—"
    if sec < 60:
        return f"{int(sec)}秒前"
    if sec < 3600:
        return f"{int(sec // 60)}分前"
    return f"{sec / 3600:.1f}時前"


def has_cjk(text):
    return any(0x2E80 <= ord(ch) <= 0x9FFF or 0xFF00 <= ord(ch) <= 0xFFEF for ch in text)


def toast(title, body):
    """Windows toast via PowerShell/WinRT; silently does nothing if unavailable."""
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@"
<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{body}</text></binding></visual></toast>
"@)
$t = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def enable_dpi_awareness():
    """Must run before Tk is created. Returns the scale factor (1.0 at 96 dpi)."""
    if sys.platform != "win32":
        return 1.0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)        # per-monitor
    except (OSError, AttributeError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except (OSError, AttributeError):
        return 1.0


class Pet:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.snap = None
        self.snap_lock = threading.Lock()
        self.fail_count = 0
        self.server_spawned_at = 0.0
        self.muted = False
        self.view = "full"          # full | cat | card
        self.details = False
        self.bubble_text = None
        self.bubble_until = 0.0
        self.last_idx = {}          # (source, window) -> threshold index reached
        self.last_reset = {}        # (source, window) -> resets_at seen
        self.next_chatter = time.time() + 120
        self.frame = 0
        self._toggle_rect = (0, 0, 0, 0)

        self.S = enable_dpi_awareness()
        s = self.S
        self.root = tk.Tk()
        self.W, self.H = round(W * s), round(H * s)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(self._load_geometry())
        # a canvas only to receive mouse events; all pixels come from the GDI+ surface
        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.root.update_idletasks()
        self.hwnd = gdip.toplevel_hwnd(self.canvas.winfo_id())
        self.surface = gdip.Surface(self.W, self.H)
        self.fonts = gdip.Fonts(FONT_DIR)

        # font sizes in points at 1x; GDI+ wants pixels
        self.f_title = ("head", 12, True)
        self.f_section = ("head", 11, True)
        self.f_bubble = ("head", 9, True)
        self.f_label = ("mono", 7, True)
        self.f_pct = ("mono", 8, True)
        self.f_pill = ("mono", 7, True)
        self.f_detail = ("mono", 7, False)
        self.f_detail_b = ("mono", 7, True)

        self.stickers = {st: os.path.join(ASSET_DIR, f"cat-{st}.png") for st in STATES
                         if os.path.isfile(os.path.join(ASSET_DIR, f"cat-{st}.png"))}
        self._sticker_size = {}

        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_dashboard())
        self.canvas.bind("<Button-3>", self._menu)
        self._drag = None
        self._moved = False
        self._press_xy = (0, 0)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="開啟儀表板", command=self.open_dashboard)
        self.details_var = tk.BooleanVar(value=self.details)
        self.menu.add_checkbutton(label="顯示詳細資訊", variable=self.details_var, command=self.toggle_details)
        self.menu.add_separator()
        self.view_var = tk.StringVar(value=self.view)
        for label, value in (("完整（貓 + 用量卡）", "full"), ("只留貓", "cat"), ("只留用量卡", "card")):
            self.menu.add_radiobutton(label=label, value=value, variable=self.view_var,
                                      command=lambda v=value: self.set_view(v))
        self.menu.add_separator()
        self.muted_var = tk.BooleanVar(value=self.muted)
        self.menu.add_checkbutton(label="靜音提醒", variable=self.muted_var, command=self.toggle_mute)
        self.menu.add_separator()
        self.menu.add_command(label="結束", command=self.quit)

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(50, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        if os.environ.get("TOKMON_PET_SAY"):          # layout testing hook
            self.root.after(3000, lambda: self.say(os.environ["TOKMON_PET_SAY"].replace("\\n", "\n"), 60))
        if os.environ.get("TOKMON_PET_DETAILS"):      # layout testing hook
            self.details = True
            self.details_var.set(True)
        if os.environ.get("TOKMON_PET_VIEW") in ("full", "cat", "card"):   # layout testing hook
            self.view = os.environ["TOKMON_PET_VIEW"]
            self.view_var.set(self.view)

    # -- fonts / text --------------------------------------------------------------------
    def _font(self, spec, cjk=False):
        kind, pt, bold = spec
        px = pt * 96 / 72 * self.S
        names = CJK_FONTS if cjk else HEAD_FONTS if kind == "head" else MONO_FONTS
        return self.fonts.get(names, px, bold)

    def _text(self, x, y, text, spec, color, anchor="nw"):
        self.surface.text(x, y, text, self._font(spec, has_cjk(text)), color, anchor)

    def _measure(self, text, spec):
        return self.surface.measure(text, self._font(spec, has_cjk(text)))

    # -- persistence -------------------------------------------------------------------
    def _load_geometry(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                st = json.load(fh)
            self.muted = bool(st.get("muted"))
            self.view = st.get("view") or ("cat" if st.get("compact") else "full")
            if self.view not in ("full", "cat", "card"):
                self.view = "full"
            self.details = bool(st.get("details"))
            x, y = int(st["x"]), int(st["y"])
        except (OSError, ValueError, KeyError):
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x, y = sw - self.W - round(24 * self.S), sh - self.H - round(70 * self.S)
        return f"{self.W}x{self.H}+{x}+{y}"

    def _save(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                           "muted": self.muted, "view": self.view, "details": self.details}, fh)
        except OSError:
            pass

    # -- data ----------------------------------------------------------------------------
    def _poll_loop(self):
        while True:
            try:
                with urllib.request.urlopen(self.base_url + "/api/snapshot", timeout=3) as r:
                    data = json.load(r)
                with self.snap_lock:
                    self.snap = data
                self.fail_count = 0
            except Exception:
                self.fail_count += 1
                if self.fail_count >= 2 and time.time() - self.server_spawned_at > 30:
                    self.server_spawned_at = time.time()
                    try:
                        spawn_detached("tokmon.server")
                    except OSError:
                        pass
            time.sleep(2)

    def _limits(self, src):
        with self.snap_lock:
            snap = self.snap
        return ((snap or {}).get(src) or {}).get("limits") or {}

    def _windows(self):
        """(source, label, key, window_label, window_dict, fetched_at) for the four meters."""
        with self.snap_lock:
            snap = self.snap
        if not snap:
            return []
        out = []
        for src, label, _ in SOURCES:
            lim = (snap.get(src) or {}).get("limits") or {}
            for key, wl in (("five_hour", "5h"), ("seven_day", "week")):
                out.append((src, label, key, wl, lim.get(key), lim.get("fetched_at")))
        return out

    def _source_pct(self, src):
        lim = self._limits(src)
        pcts = [lim[k]["used_percent"] for k in ("five_hour", "seven_day") if lim.get(k) and lim[k].get("used_percent") is not None]
        return max(pcts) if pcts else None

    def _worst(self):
        pcts = [p for p in (self._source_pct(src) for src, _, _ in SOURCES) if p is not None]
        return max(pcts) if pcts else None

    # -- alerts ----------------------------------------------------------------------------
    def _check_alerts(self, now):
        for src, label, key, wl, w, _ in self._windows():
            if not w or w.get("used_percent") is None:
                continue
            p = float(w["used_percent"])
            k = (src, key)
            idx = sum(1 for t in THRESHOLDS if p >= t)
            prev = self.last_idx.get(k)
            if prev is None:
                self.last_idx[k] = idx
            elif idx > prev:
                self.last_idx[k] = idx
                self._alert(f"{label} {wl} 額度已用 {p:.0f}%",
                            f"重置倒數 {fmt_dur((w.get('resets_at') or 0) - now)}", important=idx >= 2)
            elif idx < prev:
                self.last_idx[k] = idx
            ra = w.get("resets_at")
            prev_ra = self.last_reset.get(k)
            if prev_ra and ra and ra > prev_ra + 60 and p < 50:
                self._alert(f"{label} {wl} 額度已重置", "冰淇淋補貨了，可以放心用。", important=False)
            if ra:
                self.last_reset[k] = ra

    def _alert(self, title, body, important):
        self.say(f"{title}\n{body}")
        if important and not self.muted:
            toast(title, body)

    def say(self, text, seconds=BUBBLE_SECONDS):
        self.bubble_text = text
        self.bubble_until = time.time() + seconds

    # -- interaction -----------------------------------------------------------------------
    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        self._moved = False
        self._press_xy = (e.x, e.y)

    def _drag_move(self, e):
        if self._drag:
            self._moved = True
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, e):
        if self._drag and not self._moved:
            x0, y0, x1, y1 = self._toggle_rect
            px, py = self._press_xy
            if x0 <= px <= x1 and y0 <= py <= y1:
                self.toggle_details()
        self._drag = None
        self._save()

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def open_dashboard(self):
        webbrowser.open(self.base_url + "/")

    def toggle_details(self):
        self.details = not self.details
        self.details_var.set(self.details)
        self._save()

    def _card_x(self):
        """Card origin (logical px): pinned left when the cat is hidden."""
        return CAT_X if self.view == "card" else CARD_X

    def set_view(self, view):
        if view == self.view:
            return
        # shift the window so the card stays where it is on screen
        dx = round((CARD_X - CAT_X) * self.S)
        if view == "card":
            self.root.geometry(f"+{self.root.winfo_x() + dx}+{self.root.winfo_y()}")
        elif self.view == "card":
            self.root.geometry(f"+{self.root.winfo_x() - dx}+{self.root.winfo_y()}")
        self.view = view
        self.view_var.set(view)
        self._save()

    def toggle_mute(self):
        self.muted = not self.muted
        self.muted_var.set(self.muted)
        self._save()
        self.say("提醒已靜音。" if self.muted else "提醒已開啟。", 3)

    def quit(self):
        self._save()
        clear_pid(PET_PID_FILE, only_if=os.getpid())
        self.root.destroy()

    # -- drawing -----------------------------------------------------------------------------
    def _tick(self):
        now = time.time()
        self.frame += 1
        if self.frame % 10 == 0:
            self._check_alerts(now)
        if now >= self.next_chatter and self.bubble_until < now:
            self.next_chatter = now + random.uniform(600, 1200)
            self.say(random.choice(SAYINGS[state_of(self._worst())]), 6)
        self._draw(now)
        self.root.after(100, self._tick)

    def _draw(self, now):
        sf = self.surface
        sf.clear()
        s = self.S
        stale = self.snap is None or self.fail_count >= 2
        state = "sleep" if stale else state_of(self._worst())

        # bob: artboard is translateY 0 -> -8px over 3.8 s, scaled down
        bob = -(math.sin(now * 2 * math.pi / 3.8) * 0.5 + 0.5) * 4 * s
        if self.view != "card":
            self._draw_cat(state, CAT_BOTTOM * s + bob, now)
        if self.view != "cat":
            self._draw_card(now, stale)
        if self.bubble_text and now < self.bubble_until:
            self._draw_bubble(self.bubble_text)

        gdip.make_layered(self.hwnd)      # Tk may rewrite the ex-style on attribute changes
        sf.push(self.hwnd)

    def _draw_cat(self, state, bottom, now):
        sf = self.surface
        s = self.S
        path = self.stickers.get(state if state != "sleep" else "limit") or self.stickers.get("idle")
        size = sf.image_size(path) if path else None
        if size:
            w = CAT_W * s
            h = w * size[1] / size[0]
            top = bottom - h
            sf.draw_image(path, CAT_X * s, top, w, h)
        else:
            top = self._draw_fallback_cat(state, bottom)
        right = (CAT_X + CAT_W) * s
        # overlays for states that have no sticker of their own
        if state == "warning" and "warning" not in self.stickers:
            for i in range(2):
                sy = top + 30 * s + ((now * 22 + i * 9) % 16) * s
                sx = right - (34 - i * 10) * s
                sf.ellipse(sx, sy, sx + 6 * s, sy + 9 * s, fill="#86b6ef", outline=INK, width=max(1.0, s))
        if state in ("limit", "sleep") and (state == "sleep" or "limit" not in self.stickers):
            for i, pt in enumerate((9, 11, 13)):
                phase = (now * 0.8 + i * 0.33) % 1
                sf.text(right - (30 - i * 9) * s, top + (24 - phase * 22 - i * 6) * s, "z",
                        self._font(("head", pt, True)), INK, "c")

    def _draw_fallback_cat(self, state, bottom):
        """Simple drawn cat used only when no sticker file is available."""
        sf = self.surface
        s = self.S
        cx, r = PET_CX * s, 52 * s
        cy = bottom - r - 6 * s
        lw = max(1.0, 2 * s)
        fur = "#f4d9b8"
        sf.polygon([(cx - 40 * s, cy - 20 * s), (cx - 30 * s, cy - 62 * s), (cx - 8 * s, cy - 40 * s)], fill=fur, outline=INK, width=lw)
        sf.polygon([(cx + 40 * s, cy - 20 * s), (cx + 30 * s, cy - 62 * s), (cx + 8 * s, cy - 40 * s)], fill=fur, outline=INK, width=lw)
        sf.ellipse(cx - r, cy - r, cx + r, cy + r, fill=fur, outline=INK, width=lw)
        for ex in (cx - 18 * s, cx + 18 * s):
            if state in ("limit", "sleep"):
                sf.arc(ex - 8 * s, cy - 12 * s, ex + 8 * s, cy, 20, 140, INK, lw)        # closed, smiling eye
            else:
                sf.ellipse(ex - 5 * s, cy - 10 * s, ex + 5 * s, cy + 2 * s, fill=INK)
        sf.polygon([(cx - 4 * s, cy + 8 * s), (cx + 4 * s, cy + 8 * s), (cx, cy + 13 * s)], fill=INK)
        sf.arc(cx - 12 * s, cy + 6 * s, cx, cy + 20 * s, 10, 160, INK, lw)
        sf.arc(cx, cy + 6 * s, cx + 12 * s, cy + 20 * s, 10, 160, INK, lw)
        return cy - 62 * s

    def _pill(self, x_right, y, text, fg, bg):
        """Outlined capsule (artboard 'reset 2h 14m' style), right-aligned at x_right."""
        s = self.S
        tw, _ = self._measure(text, self.f_pill)
        px1, px0 = x_right, x_right - tw - 14 * s
        self.surface.round_rect(px0, y - 1 * s, px1, y + 15 * s, 8 * s, fill=bg, outline=SHADOW, width=2 * s)
        self._text((px0 + px1) / 2, y + 7 * s, text, self.f_pill, fg, "c")
        return px0

    def _draw_card(self, now, stale):
        sf = self.surface
        s = self.S
        x0, y0 = self._card_x() * s, CARD_Y * s
        x1 = x0 + CARD_W * s
        y1 = y0 + (CARD_H + (DETAIL_H if self.details else 0)) * s
        b = 2 * s                                  # outline thickness
        off = 4 * s                                # shadow thickness
        # shadow shares the card's top-left corner and thickens right/bottom,
        # so no corner arc ever pokes out past the card outline
        sf.round_rect(x0, y0, x1 + off, y1 + off, 16 * s, fill=SHADOW)
        sf.round_rect(x0, y0, x1, y1, 16 * s, fill=CARD, outline=INK, width=b)

        pad = 14 * s
        y = y0 + 13 * s
        # title + disclosure triangle (click target covers the whole title row)
        self._text(x0 + pad, y + 7 * s, "用量", self.f_title, INK, "w")
        tw, _ = self._measure("用量", self.f_title)
        tx, ty = x0 + pad + tw + 9 * s, y + 7 * s
        if self.details:
            pts = [(tx - 4 * s, ty - 2 * s), (tx + 4 * s, ty - 2 * s), (tx, ty + 3 * s)]
        else:
            pts = [(tx - 2 * s, ty - 4 * s), (tx + 3 * s, ty), (tx - 2 * s, ty + 4 * s)]
        sf.polygon(pts, fill=INK)
        self._toggle_rect = (x0, y - 6 * s, tx + 20 * s, y + 20 * s)
        if stale:
            self._pill(x1 - pad, y, "connecting…", MUTED, CARD)
        y += (14 + 12) * s

        for i, (src, label, color) in enumerate(SOURCES):
            lim = self._limits(src)
            iy = y
            # icon: rounded square with a dot (Codex) or a diamond (Claude)
            ix = x0 + pad
            sf.round_rect(ix, iy, ix + 15 * s, iy + 15 * s, 5 * s, fill=color, outline=INK, width=b)
            if src == "codex":
                sf.ellipse(ix + 5 * s, iy + 5 * s, ix + 10 * s, iy + 10 * s, fill=INK)
            else:
                sf.polygon([(ix + 7.5 * s, iy + 4 * s), (ix + 11 * s, iy + 7.5 * s),
                            (ix + 7.5 * s, iy + 11 * s), (ix + 4 * s, iy + 7.5 * s)], fill=INK)
            self._text(ix + 21 * s, iy + 7.5 * s, label, self.f_section, INK, "w")
            # right side: this source's own 5h reset countdown
            if not stale:
                w5 = lim.get("five_hour")
                ra = w5.get("resets_at") if w5 else None
                self._pill(x1 - pad, iy, fmt_reset(ra - now if ra else None), MUTED, CARD)
            y += (15 + 7) * s
            for key, wl in (("five_hour", "5h"), ("seven_day", "week")):
                w = lim.get(key)
                p = None if not w else w.get("used_percent")
                self._text(x0 + pad, y + 5 * s, wl, self.f_label, MUTED, "w")
                bx0, bx1 = x0 + pad + 26 * s, x1 - pad - 32 * s
                bh = 10 * s
                sf.round_rect(bx0, y, bx1, y + bh, bh / 2, fill=TRACK, outline=INK, width=b)
                if p is not None:
                    fw = (bx1 - bx0 - 2 * b) * max(0.0, min(100.0, float(p))) / 100
                    if fw >= 3 * s:
                        sf.round_rect(bx0 + b, y + b, bx0 + b + fw, y + bh - b, bh / 2 - b, fill=color)
                        sf.round_rect(bx0 + fw, y + b, bx0 + b + fw, y + bh - b, 0, fill=INK)
                    txt = f"{float(p):.0f}%"
                else:
                    txt = "—"
                self._text(x1 - pad, y + 5 * s, txt, self.f_pct, INK, "e")
                y += (10 + 6) * s
            if i == 0:
                y += (12 - 6) * s
                sf.round_rect(x0 + pad, y, x1 - pad, y + 2 * s, 1 * s, fill=DIVIDER)
                y += (2 + 12) * s

        if self.details:
            y += (12 - 6) * s
            sf.round_rect(x0 + pad, y, x1 - pad, y + 2 * s, 1 * s, fill=DIVIDER)
            y += (2 + 8) * s
            self._draw_details(x0 + pad, y, x1 - pad, now, stale)

    def _draw_details(self, x, y, x_right, now, stale):
        """Expanded block: per-source resets, plan, credits, data freshness."""
        s = self.S
        lh = 12 * s
        if stale:
            self._text(x, y, "正在連線到後端…", self.f_detail, MUTED)
            return
        with self.snap_lock:
            snap = self.snap or {}
        for src, label, _ in SOURCES:
            lim = self._limits(src)
            origin = "即時" if lim.get("source") == "live" else "快取"
            age_s = (now - lim["fetched_at"]) if lim.get("fetched_at") else None
            age = fmt_ago(age_s) if age_s is not None else "—"
            plan = (lim.get("plan") or "?").replace("_", " ")
            # same rule as the dashboard: old is old, whatever the reason
            stale_note = "  ·  可能過時" if lim.get("stale", age_s is None or age_s > 1800) else ""
            self._text(x, y, f"{label}  ·  {plan}  ·  {origin} {age}{stale_note}", self.f_detail_b, INK)
            y += lh
            parts = []
            for key, wl in (("five_hour", "5h"), ("seven_day", "week")):
                w = lim.get(key)
                ra = w.get("resets_at") if w else None
                parts.append(f"{wl} 重置 {fmt_dur(ra - now) if ra else '—'}")
            self._text(x + 8 * s, y, "  ·  ".join(parts), self.f_detail, MUTED)
            y += lh
            extra = lim.get("extra_usage") or {}
            if extra.get("enabled") and extra.get("utilization") is not None:
                dp = extra.get("decimal_places") or 2
                div = 10 ** dp
                money = lambda v: "—" if v is None else f"${v / div:.{dp}f}"
                self._text(x + 8 * s, y, f"credits {float(extra['utilization']):.0f}%  ·  "
                           f"{money(extra.get('used_credits'))} / {money(extra.get('monthly_limit'))} 每月",
                           self.f_detail, MUTED)
                y += lh
            y += 4 * s

    def _wrap(self, text, spec, max_w):
        lines = []
        for para in text.split("\n"):
            cur = ""
            for ch in para:
                if self._measure(cur + ch, spec)[0] > max_w and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            lines.append(cur)
        return lines

    def _draw_bubble(self, text):
        sf = self.surface
        s = self.S
        pad = 8 * s
        lines = self._wrap(text, self.f_bubble, self.W - 2 * pad - 20 * s)
        lh = self._measure("用量Ag", self.f_bubble)[1]
        tw = max(self._measure(ln, self.f_bubble)[0] for ln in lines)
        th = lh * len(lines)
        x0 = 6 * s
        # sit just above the cat; if the text is taller than the reserved strip,
        # grow downward over the cat instead of clipping the last line at the top
        y0 = max(2 * s, (BUBBLE_H - 6) * s - th - 2 * pad)
        y1 = y0 + th + 2 * pad
        b = 2 * s
        # tail points at the cat, or at the card's title when the cat is hidden
        cx = (self._card_x() + 40) * s if self.view == "card" else PET_CX * s
        sf.round_rect(x0, y0, x0 + tw + 2 * pad + 3 * s, y1 + 3 * s, 12 * s, fill=SHADOW)
        sf.round_rect(x0, y0, x0 + tw + 2 * pad, y1, 12 * s, fill=CARD, outline=INK, width=b)
        sf.polygon([(cx - 9 * s, y1 - b), (cx + 9 * s, y1 - b), (cx, y1 + 9 * s)], fill=INK)
        sf.polygon([(cx - 6 * s, y1 - b - 1), (cx + 6 * s, y1 - b - 1), (cx, y1 + 5 * s)], fill=CARD)
        for i, ln in enumerate(lines):
            self._text(x0 + pad, y0 + pad + i * lh, ln, self.f_bubble, INK)

    def run(self):
        self.root.mainloop()


def main(argv=None):
    existing = read_pid(PET_PID_FILE)
    if existing and existing != os.getpid() and is_alive(existing):
        print(f"Token Monitor pet is already running (pid {existing}).", file=sys.stderr)
        return 0
    write_pid(PET_PID_FILE, os.getpid())
    atexit.register(clear_pid, PET_PID_FILE, os.getpid())
    cfg = load_config(os.path.join(PROJECT_DIR, "config.json"))
    Pet(f"http://{cfg['host']}:{cfg['port']}").run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
