"""
Capture + image processing for the NTE auto-fisher.

Vision is intentionally stateless aside from the capture thread: the Fisher
state machine asks for a fresh frame and runs detectors on a chosen ROI.
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
        """Map an ROI authored in 1920x1080 space into this window's pixel space."""
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
    """Background grabber that keeps the latest frame in a single slot.

    We deliberately drop frames — the state machine only ever wants the most
    recent one. A queue would let the consumer fall behind during slow ticks.
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
        # Spin briefly so the first .latest() returns a frame.
        for _ in range(50):
            if self._thread.latest() is not None:
                break
            time.sleep(0.01)

    def relocate(self) -> None:
        """Re-find the game window. Call if the user moved/resized the game."""
        if self._thread:
            self._thread.stop()
            self._thread = None
        self.window = self._locate_window()
        if self.cfg.use_capture_thread:
            self._start_thread()

    # ---- Capture ----

    def grab_full(self) -> np.ndarray:
        if self._thread is not None:
            f = self._thread.latest()
            if f is not None:
                return f
        # Fallback: synchronous grab of the whole window.
        mon = {
            "left": self.window.left,
            "top": self.window.top,
            "width": self.window.width,
            "height": self.window.height,
        }
        raw = np.asarray(self._sct.grab(mon))
        return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

    def window_to_screen(self, x: int, y: int) -> tuple:
        """Convert window-relative pixel coords to absolute screen coords."""
        return (self.window.left + int(x), self.window.top + int(y))

    def grab_roi(self, roi_ref: tuple) -> np.ndarray:
        """Crop the latest full frame to a reference-space ROI."""
        full = self.grab_full()
        x, y, w, h = self.window.scale_roi(roi_ref)
        # Convert from screen-space to frame-space (frame is window-local).
        x0 = max(0, x - self.window.left)
        y0 = max(0, y - self.window.top)
        x1 = min(full.shape[1], x0 + w)
        y1 = min(full.shape[0], y0 + h)
        return full[y0:y1, x0:x1]

    # ---- Detectors ----

    @staticmethod
    def hsv_mask(bgr: np.ndarray, rng: HSVRange) -> np.ndarray:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        lower, upper = rng.as_arrays()
        if rng.wrap:
            # Hue wraps at 180 (e.g. red lives near 0 and near 180). We treat
            # the supplied range as the low band [0..upper_h] and mirror it to
            # the high band [180-upper_h .. 180], reusing S/V on both sides.
            low_a = np.array([0, lower[1], lower[2]], dtype=np.uint8)
            high_a = np.array([upper[0], upper[1], upper[2]], dtype=np.uint8)
            low_b = np.array([180 - upper[0], lower[1], lower[2]], dtype=np.uint8)
            high_b = np.array([180, upper[1], upper[2]], dtype=np.uint8)
            mask = cv2.bitwise_or(cv2.inRange(hsv, low_a, high_a),
                                  cv2.inRange(hsv, low_b, high_b))
        else:
            mask = cv2.inRange(hsv, lower, upper)
        # Light morphology to kill compression speckle without eating the target.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        return mask

    @staticmethod
    def centroid(mask: np.ndarray) -> Optional[tuple]:
        """Return (cx, cy) of the largest contiguous blob in `mask`, or None."""
        m = cv2.moments(mask)
        if m["m00"] < 1:
            return None
        return (m["m10"] / m["m00"], m["m01"] / m["m00"])

    @staticmethod
    def horizontal_extent(mask: np.ndarray) -> Optional[tuple]:
        """Return (x_start, x_end) — leftmost and rightmost lit columns.

        Used to find the target zone's horizontal span on the gauge.
        """
        cols = np.where(mask.any(axis=0))[0]
        if cols.size == 0:
            return None
        return (int(cols[0]), int(cols[-1]))

    def color_present(self, bgr: np.ndarray, rng: HSVRange, min_pixels: int) -> bool:
        mask = self.hsv_mask(bgr, rng)
        return int(cv2.countNonZero(mask)) >= min_pixels

    # ---- Debug ----

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
