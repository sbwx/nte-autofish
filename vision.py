"""
screen capture + image checks.

stateless apart from the capture thread. fisher asks for a fresh frame
and runs whatever check it wants on whatever bit of the screen it wants.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import mss
import numpy as np

try:
    import pygetwindow as gw
    HAVE_GW = True
except Exception:
    HAVE_GW = False

from config import Config, HSVRange, REFERENCE_W, REFERENCE_H


@dataclass
class WindowBox:
    left: int
    top: int
    width: int
    height: int

    def scale_roi(self, roi_ref: tuple) -> tuple:
        """take an roi written in 1920x1080 numbers and rescale it to
        wherever the window actually is."""
        x, y, w, h = roi_ref
        sx = self.width / REFERENCE_W
        sy = self.height / REFERENCE_H
        return (
            self.left + int(round(x * sx)),
            self.top + int(round(y * sy)),
            max(1, int(round(w * sx))),
            max(1, int(round(h * sy))),
        )


def find_game_window(title_substr: str) -> Optional[WindowBox]:
    if not HAVE_GW:
        return None
    needle = title_substr.lower()
    for w in gw.getAllWindows():
        if w.title and needle in w.title.lower() and w.width > 100 and w.height > 100:
            return WindowBox(w.left, w.top, w.width, w.height)
    return None


def primary_monitor_box() -> WindowBox:
    with mss.mss() as sct:
        m = sct.monitors[1]
        return WindowBox(m["left"], m["top"], m["width"], m["height"])


class _CaptureThread(threading.Thread):
    """grabs frames in the background, only keeps the newest one.
    old frames are thrown away on purpose, we never want to look at
    anything but the most recent thing on screen.
    """
    def __init__(self, monitor: dict):
        super().__init__(daemon=True)
        self._monitor = monitor
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._stop = threading.Event()

    def run(self):
        with mss.mss() as sct:
            while not self._stop.is_set():
                raw = np.asarray(sct.grab(self._monitor))
                bgr = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                with self._lock:
                    self._frame = bgr

    def latest(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self):
        self._stop.set()


class Vision:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.window = self._locate_window()
        self._sct = mss.mss()
        self._thread: Optional[_CaptureThread] = None
        if cfg.use_capture_thread:
            self._start_thread()

    def _locate_window(self) -> WindowBox:
        wb = find_game_window(self.cfg.window_title)
        if wb is not None:
            print(f"[vision] found window '{self.cfg.window_title}' at "
                  f"{wb.left},{wb.top} {wb.width}x{wb.height}")
            return wb
        if self.cfg.fallback_fullscreen:
            wb = primary_monitor_box()
            print(f"[vision] window not found, using primary monitor "
                  f"{wb.width}x{wb.height}")
            return wb
        raise RuntimeError(f"Game window '{self.cfg.window_title}' not found")

    def _start_thread(self):
        mon = {
            "left": self.window.left,
            "top": self.window.top,
            "width": self.window.width,
            "height": self.window.height,
        }
        self._thread = _CaptureThread(mon)
        self._thread.start()
        # wait a tiny bit so the first call gets a frame back
        for _ in range(50):
            if self._thread.latest() is not None:
                break
            time.sleep(0.01)

    def relocate(self) -> None:
        """find the window again. call this if the game got moved or resized."""
        if self._thread:
            self._thread.stop()
            self._thread = None
        self.window = self._locate_window()
        if self.cfg.use_capture_thread:
            self._start_thread()

    # ---- capture ----

    def grab_full(self) -> np.ndarray:
        if self._thread is not None:
            f = self._thread.latest()
            if f is not None:
                return f
        # backup: just grab the window directly right now
        mon = {
            "left": self.window.left,
            "top": self.window.top,
            "width": self.window.width,
            "height": self.window.height,
        }
        raw = np.asarray(self._sct.grab(mon))
        return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

    def window_to_screen(self, x: int, y: int) -> tuple:
        """turn a point inside the window into a point on the whole screen."""
        return (self.window.left + int(x), self.window.top + int(y))

    def grab_roi(self, roi_ref: tuple) -> np.ndarray:
        """cut out a chunk of the latest frame using a 1920x1080-style roi."""
        full = self.grab_full()
        x, y, w, h = self.window.scale_roi(roi_ref)
        # convert from screen coords to coords inside this frame
        x0 = max(0, x - self.window.left)
        y0 = max(0, y - self.window.top)
        x1 = min(full.shape[1], x0 + w)
        y1 = min(full.shape[0], y0 + h)
        return full[y0:y1, x0:x1]

    # ---- image checks ----

    @staticmethod
    def hsv_mask(bgr: np.ndarray, rng: HSVRange) -> np.ndarray:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        lower, upper = rng.as_arrays()
        if rng.wrap:
            # red lives at both ends of the hue wheel, so we have to
            # match two ranges and OR them together
            low_a = np.array([0, lower[1], lower[2]], dtype=np.uint8)
            high_a = np.array([upper[0], upper[1], upper[2]], dtype=np.uint8)
            low_b = np.array([180 - upper[0], lower[1], lower[2]], dtype=np.uint8)
            high_b = np.array([180, upper[1], upper[2]], dtype=np.uint8)
            mask = cv2.bitwise_or(cv2.inRange(hsv, low_a, high_a),
                                  cv2.inRange(hsv, low_b, high_b))
        else:
            mask = cv2.inRange(hsv, lower, upper)
        # smooth out tiny noise pixels without eating real stuff
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        return mask

    @staticmethod
    def centroid(mask: np.ndarray) -> Optional[tuple]:
        """middle point of all matching pixels. none if nothing matched."""
        m = cv2.moments(mask)
        if m["m00"] < 1:
            return None
        return (m["m10"] / m["m00"], m["m01"] / m["m00"])

    @staticmethod
    def horizontal_extent(mask: np.ndarray) -> Optional[tuple]:
        """leftmost and rightmost matching column."""
        cols = np.where(mask.any(axis=0))[0]
        if cols.size == 0:
            return None
        return (int(cols[0]), int(cols[-1]))

    @staticmethod
    def largest_blob_extent(mask: np.ndarray):
        """left and right edges, plus pixel count, of the biggest matching
        chunk. returns (none, 0) if nothing matched.
        useful when there might be more than one matching thing and you
        only want the biggest (e.g. ignoring small bits of color from
        the icons next to the bar).
        """
        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n_labels <= 1:
            return None, 0
        areas = stats[1:, cv2.CC_STAT_AREA]
        idx = 1 + int(np.argmax(areas))
        x = int(stats[idx, cv2.CC_STAT_LEFT])
        w = int(stats[idx, cv2.CC_STAT_WIDTH])
        return (x, x + w - 1), int(stats[idx, cv2.CC_STAT_AREA])

    @staticmethod
    def largest_blob_centroid(mask: np.ndarray):
        """middle point and pixel count of the biggest matching chunk.
        returns (none, 0) if nothing matched. won't get pulled off by
        small bits of noise the way a global average would.
        """
        n_labels, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n_labels <= 1:
            return None, 0
        areas = stats[1:, cv2.CC_STAT_AREA]
        idx = 1 + int(np.argmax(areas))
        cx, cy = centroids[idx]
        return (float(cx), float(cy)), int(stats[idx, cv2.CC_STAT_AREA])

    def color_present(self, bgr: np.ndarray, rng: HSVRange, min_pixels: int) -> bool:
        mask = self.hsv_mask(bgr, rng)
        return int(cv2.countNonZero(mask)) >= min_pixels

    # ---- debug ----

    def show_debug(self, name: str, img: np.ndarray):
        if not self.cfg.debug_mode:
            return
        cv2.imshow(name, img)
        cv2.waitKey(1)

    def close(self):
        if self._thread:
            self._thread.stop()
        try:
            self._sct.close()
        except Exception:
            pass
        if self.cfg.debug_mode:
            cv2.destroyAllWindows()
