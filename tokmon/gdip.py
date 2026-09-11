"""Minimal GDI+ (flat API) wrapper via ctypes - no third-party packages.

Draws anti-aliased shapes, images and text into a premultiplied-ARGB DIB and
pushes it to a window with UpdateLayeredWindow, giving true per-pixel alpha.
Only what the desktop pet needs is wrapped.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, Structure, byref, c_float, c_int, c_uint, c_void_p, c_wchar_p

gp = ctypes.windll.gdiplus
u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32

REAL = c_float
PixelFormat32bppPARGB = 0xE200B
UnitPixel = 2
SmoothingModeAntiAlias = 4
InterpolationModeHighQualityBicubic = 7
PixelOffsetModeHalf = 4
TextRenderingHintAntiAlias = 4
FontStyleRegular, FontStyleBold = 0, 1
StringFormatFlagsNoWrap = 0x1000
WS_EX_LAYERED = 0x80000
GWL_EXSTYLE = -20
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1


class RectF(Structure):
    _fields_ = [("x", REAL), ("y", REAL), ("w", REAL), ("h", REAL)]


class PointF(Structure):
    _fields_ = [("x", REAL), ("y", REAL)]


class _StartupInput(Structure):
    _fields_ = [("version", c_uint), ("callback", c_void_p), ("bg", c_int), ("codecs", c_int)]


class _BITMAPINFOHEADER(Structure):
    _fields_ = [("biSize", c_uint), ("biWidth", c_int), ("biHeight", c_int), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", c_uint), ("biSizeImage", c_uint),
                ("biXPelsPerMeter", c_int), ("biYPelsPerMeter", c_int), ("biClrUsed", c_uint), ("biClrImportant", c_uint)]


class _POINT(Structure):
    _fields_ = [("x", c_int), ("y", c_int)]


class _SIZE(Structure):
    _fields_ = [("cx", c_int), ("cy", c_int)]


class _BLENDFUNCTION(Structure):
    _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_ubyte)]


def _fn(name, *argtypes):
    f = getattr(gp, name)
    f.argtypes = argtypes
    f.restype = c_int
    return f


P = c_void_p
GdiplusStartup = _fn("GdiplusStartup", POINTER(c_void_p), POINTER(_StartupInput), c_void_p)
GdipCreateBitmapFromScan0 = _fn("GdipCreateBitmapFromScan0", c_int, c_int, c_int, c_int, c_void_p, POINTER(P))
GdipGetImageGraphicsContext = _fn("GdipGetImageGraphicsContext", P, POINTER(P))
GdipDeleteGraphics = _fn("GdipDeleteGraphics", P)
GdipDisposeImage = _fn("GdipDisposeImage", P)
GdipSetSmoothingMode = _fn("GdipSetSmoothingMode", P, c_int)
GdipSetInterpolationMode = _fn("GdipSetInterpolationMode", P, c_int)
GdipSetPixelOffsetMode = _fn("GdipSetPixelOffsetMode", P, c_int)
GdipSetTextRenderingHint = _fn("GdipSetTextRenderingHint", P, c_int)
GdipGraphicsClear = _fn("GdipGraphicsClear", P, c_uint)
GdipCreateSolidFill = _fn("GdipCreateSolidFill", c_uint, POINTER(P))
GdipDeleteBrush = _fn("GdipDeleteBrush", P)
GdipCreatePen1 = _fn("GdipCreatePen1", c_uint, REAL, c_int, POINTER(P))
GdipDeletePen = _fn("GdipDeletePen", P)
GdipCreatePath = _fn("GdipCreatePath", c_int, POINTER(P))
GdipDeletePath = _fn("GdipDeletePath", P)
GdipAddPathArc = _fn("GdipAddPathArc", P, REAL, REAL, REAL, REAL, REAL, REAL)
GdipAddPathLine = _fn("GdipAddPathLine", P, REAL, REAL, REAL, REAL)
GdipClosePathFigure = _fn("GdipClosePathFigure", P)
GdipFillPath = _fn("GdipFillPath", P, P, P)
GdipDrawPath = _fn("GdipDrawPath", P, P, P)
GdipFillEllipse = _fn("GdipFillEllipse", P, P, REAL, REAL, REAL, REAL)
GdipDrawEllipse = _fn("GdipDrawEllipse", P, P, REAL, REAL, REAL, REAL)
GdipFillPolygon2 = _fn("GdipFillPolygon2", P, P, POINTER(PointF), c_int)
GdipDrawPolygon = _fn("GdipDrawPolygon", P, P, POINTER(PointF), c_int)
GdipDrawLine = _fn("GdipDrawLine", P, P, REAL, REAL, REAL, REAL)
GdipDrawArc = _fn("GdipDrawArc", P, P, REAL, REAL, REAL, REAL, REAL, REAL)
GdipLoadImageFromFile = _fn("GdipLoadImageFromFile", c_wchar_p, POINTER(P))
GdipGetImageWidth = _fn("GdipGetImageWidth", P, POINTER(c_uint))
GdipGetImageHeight = _fn("GdipGetImageHeight", P, POINTER(c_uint))
GdipDrawImageRect = _fn("GdipDrawImageRect", P, P, REAL, REAL, REAL, REAL)
GdipNewPrivateFontCollection = _fn("GdipNewPrivateFontCollection", POINTER(P))
GdipPrivateAddFontFile = _fn("GdipPrivateAddFontFile", P, c_wchar_p)
GdipCreateFontFamilyFromName = _fn("GdipCreateFontFamilyFromName", c_wchar_p, P, POINTER(P))
GdipCreateFont = _fn("GdipCreateFont", P, REAL, c_int, c_int, POINTER(P))
GdipStringFormatGetGenericTypographic = _fn("GdipStringFormatGetGenericTypographic", POINTER(P))
GdipCreateStringFormat = _fn("GdipCreateStringFormat", c_int, ctypes.c_ushort, POINTER(P))
GdipSetStringFormatFlags = _fn("GdipSetStringFormatFlags", P, c_int)
GdipDrawString = _fn("GdipDrawString", P, c_wchar_p, c_int, P, POINTER(RectF), P, P)
GdipMeasureString = _fn("GdipMeasureString", P, c_wchar_p, c_int, P, POINTER(RectF), P, POINTER(RectF), POINTER(c_int), POINTER(c_int))

g32.CreateCompatibleDC.restype = c_void_p
g32.CreateCompatibleDC.argtypes = [c_void_p]
g32.CreateDIBSection.restype = c_void_p
g32.CreateDIBSection.argtypes = [c_void_p, POINTER(_BITMAPINFOHEADER), c_uint, POINTER(c_void_p), c_void_p, c_uint]
g32.SelectObject.restype = c_void_p
g32.SelectObject.argtypes = [c_void_p, c_void_p]
g32.DeleteObject.argtypes = [c_void_p]
g32.DeleteDC.argtypes = [c_void_p]
u32.GetDC.restype = c_void_p
u32.GetDC.argtypes = [c_void_p]
u32.ReleaseDC.argtypes = [c_void_p, c_void_p]
u32.UpdateLayeredWindow.argtypes = [c_void_p, c_void_p, POINTER(_POINT), POINTER(_SIZE), c_void_p, POINTER(_POINT), c_uint, POINTER(_BLENDFUNCTION), c_uint]
u32.UpdateLayeredWindow.restype = c_int
u32.GetWindowLongW.argtypes = [c_void_p, c_int]
u32.GetWindowLongW.restype = c_int
u32.SetWindowLongW.argtypes = [c_void_p, c_int, c_int]
u32.SetWindowLongW.restype = c_int
u32.GetAncestor.argtypes = [c_void_p, c_uint]
u32.GetAncestor.restype = c_void_p

_started = False


def startup():
    global _started
    if _started:
        return
    token = c_void_p()
    st = GdiplusStartup(byref(token), byref(_StartupInput(1, None, 0, 0)), None)
    if st != 0:
        raise OSError(f"GdiplusStartup failed: {st}")
    _started = True


def argb(color, alpha=255):
    """'#rrggbb' -> GDI+ ARGB DWORD."""
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return (alpha << 24) | (r << 16) | (g << 8) | b


def make_layered(hwnd):
    style = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if not style & WS_EX_LAYERED:
        u32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)


def toplevel_hwnd(child_hwnd):
    return u32.GetAncestor(child_hwnd, 2)          # GA_ROOT


class Fonts:
    """Private font collection + installed fallbacks, cached GpFont objects."""

    def __init__(self, font_dir):
        self.coll = c_void_p()
        GdipNewPrivateFontCollection(byref(self.coll))
        if os.path.isdir(font_dir):
            for name in sorted(os.listdir(font_dir)):
                if name.lower().endswith((".ttf", ".otf")):
                    GdipPrivateAddFontFile(self.coll, os.path.join(font_dir, name))
        self._families = {}
        self._fonts = {}

    def family(self, name):
        if name in self._families:
            return self._families[name]
        fam = c_void_p()
        if GdipCreateFontFamilyFromName(name, self.coll, byref(fam)) != 0 or not fam.value:
            fam = c_void_p()
            if GdipCreateFontFamilyFromName(name, None, byref(fam)) != 0 or not fam.value:
                fam = None
        self._families[name] = fam
        return fam

    def get(self, names, px, bold):
        """First family in `names` that exists; size in pixels."""
        key = (tuple(names), round(px, 2), bold)
        if key in self._fonts:
            return self._fonts[key]
        font = None
        for name in names:
            fam = self.family(name)
            if fam:
                f = c_void_p()
                if GdipCreateFont(fam, px, FontStyleBold if bold else FontStyleRegular, UnitPixel, byref(f)) == 0 and f.value:
                    font = f
                    break
        self._fonts[key] = font
        return font


class Surface:
    """A top-down 32bpp premultiplied DIB with a GDI+ Graphics drawing into it."""

    def __init__(self, w, h):
        startup()
        self.w, self.h = int(w), int(h)
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(bmi)
        bmi.biWidth, bmi.biHeight = self.w, -self.h
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        self.bits = c_void_p()
        screen = u32.GetDC(None)
        self.hdc = g32.CreateCompatibleDC(screen)
        self.hbm = g32.CreateDIBSection(screen, byref(bmi), 0, byref(self.bits), None, 0)
        u32.ReleaseDC(None, screen)
        self._old = g32.SelectObject(self.hdc, self.hbm)
        self.bitmap = c_void_p()
        GdipCreateBitmapFromScan0(self.w, self.h, self.w * 4, PixelFormat32bppPARGB, self.bits, byref(self.bitmap))
        self.g = c_void_p()
        GdipGetImageGraphicsContext(self.bitmap, byref(self.g))
        GdipSetSmoothingMode(self.g, SmoothingModeAntiAlias)
        GdipSetInterpolationMode(self.g, InterpolationModeHighQualityBicubic)
        GdipSetPixelOffsetMode(self.g, PixelOffsetModeHalf)
        GdipSetTextRenderingHint(self.g, TextRenderingHintAntiAlias)
        self._fmt = c_void_p()
        generic = c_void_p()
        GdipStringFormatGetGenericTypographic(byref(generic))
        self._fmt = generic
        self._brushes = {}
        self._pens = {}
        self._images = {}

    # -- resources ------------------------------------------------------------
    def brush(self, color, alpha=255):
        key = argb(color, alpha)
        b = self._brushes.get(key)
        if b is None:
            b = c_void_p()
            GdipCreateSolidFill(key, byref(b))
            self._brushes[key] = b
        return b

    def pen(self, color, width, alpha=255):
        key = (argb(color, alpha), round(width, 2))
        p = self._pens.get(key)
        if p is None:
            p = c_void_p()
            GdipCreatePen1(key[0], width, UnitPixel, byref(p))
            self._pens[key] = p
        return p

    def image(self, path):
        img = self._images.get(path)
        if img is None:
            img = c_void_p()
            if GdipLoadImageFromFile(path, byref(img)) != 0 or not img.value:
                img = None
            self._images[path] = img
        return img

    def image_size(self, path):
        img = self.image(path)
        if not img:
            return None
        w, h = c_uint(), c_uint()
        GdipGetImageWidth(img, byref(w))
        GdipGetImageHeight(img, byref(h))
        return w.value, h.value

    # -- drawing ----------------------------------------------------------------
    def clear(self):
        GdipGraphicsClear(self.g, 0)

    def _round_path(self, x0, y0, x1, y1, r):
        r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
        path = c_void_p()
        GdipCreatePath(0, byref(path))
        d = r * 2
        if r <= 0:
            GdipAddPathLine(path, x0, y0, x1, y0)
            GdipAddPathLine(path, x1, y0, x1, y1)
            GdipAddPathLine(path, x1, y1, x0, y1)
        else:
            GdipAddPathArc(path, x0, y0, d, d, 180, 90)
            GdipAddPathArc(path, x1 - d, y0, d, d, 270, 90)
            GdipAddPathArc(path, x1 - d, y1 - d, d, d, 0, 90)
            GdipAddPathArc(path, x0, y1 - d, d, d, 90, 90)
        GdipClosePathFigure(path)
        return path

    def round_rect(self, x0, y0, x1, y1, r, fill=None, outline=None, width=1.0):
        """Outline is drawn *inside* the box (inset by width/2) so edges stay crisp."""
        if fill:
            path = self._round_path(x0, y0, x1, y1, r)
            GdipFillPath(self.g, self.brush(fill), path)
            GdipDeletePath(path)
        if outline and width > 0:
            h = width / 2
            path = self._round_path(x0 + h, y0 + h, x1 - h, y1 - h, max(0, r - h))
            GdipDrawPath(self.g, self.pen(outline, width), path)
            GdipDeletePath(path)

    def ellipse(self, x0, y0, x1, y1, fill=None, outline=None, width=1.0):
        if fill:
            GdipFillEllipse(self.g, self.brush(fill), x0, y0, x1 - x0, y1 - y0)
        if outline and width > 0:
            h = width / 2
            GdipDrawEllipse(self.g, self.pen(outline, width), x0 + h, y0 + h, x1 - x0 - width, y1 - y0 - width)

    def polygon(self, points, fill=None, outline=None, width=1.0):
        n = len(points)
        arr = (PointF * n)(*[PointF(x, y) for x, y in points])
        if fill:
            GdipFillPolygon2(self.g, self.brush(fill), arr, n)
        if outline and width > 0:
            GdipDrawPolygon(self.g, self.pen(outline, width), arr, n)

    def line(self, x0, y0, x1, y1, color, width=1.0):
        GdipDrawLine(self.g, self.pen(color, width), x0, y0, x1, y1)

    def arc(self, x0, y0, x1, y1, start, sweep, color, width=1.0):
        """GDI+ angles: degrees clockwise from +x."""
        GdipDrawArc(self.g, self.pen(color, width), x0, y0, x1 - x0, y1 - y0, start, sweep)

    def draw_image(self, path, x, y, w, h):
        img = self.image(path)
        if img:
            GdipDrawImageRect(self.g, img, x, y, w, h)

    def measure(self, text, font):
        if not font or not text:
            return 0.0, 0.0
        layout = RectF(0, 0, 0, 0)
        bound = RectF()
        GdipMeasureString(self.g, text, -1, font, byref(layout), self._fmt, byref(bound), None, None)
        return bound.w, bound.h

    def text(self, x, y, text, font, color, anchor="nw"):
        """anchor: two letters from n/s/e/w or 'c' (Tk style)."""
        if not font or not text:
            return
        w, h = self.measure(text, font)
        if "e" in anchor:
            x -= w
        elif "w" not in anchor:
            x -= w / 2
        if "s" in anchor:
            y -= h
        elif "n" not in anchor:
            y -= h / 2
        rect = RectF(x, y, 0, 0)
        GdipDrawString(self.g, text, -1, font, byref(rect), self._fmt, self.brush(color))

    # -- output -----------------------------------------------------------------
    def push(self, hwnd):
        """Present the surface as the window's content (per-pixel alpha)."""
        size = _SIZE(self.w, self.h)
        src = _POINT(0, 0)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        return bool(u32.UpdateLayeredWindow(hwnd, None, None, byref(size), self.hdc, byref(src), 0, byref(blend), ULW_ALPHA))
