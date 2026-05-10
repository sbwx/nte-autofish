"""
Reactive state machine for the NTE auto-fisher.

Rather than progressing through cast → wait → reel → dismiss in strict order,
each tick we ask "what state is the game in *right now*?" by polling all four
detectors and acting on the highest-priority one that fires:

    CATCH_SCREEN  > REELING > HOOK_READY > IDLE > UNKNOWN

This way a flaky detector for one state doesn't break the others — if IDLE
ever misfires, the script will still react when the gauge or the catch dialog
appears, and pick up the next IDLE on its own.

External signals:
    panic   — hard stop, exits run().
    paused  — soft pause; loop idles until cleared.
"""
from __future__ import annotations

import enum
import random
import threading
import time
from typing import Optional

import cv2
import numpy as np
import pydirectinput

from config import Config
from vision import Vision

# pydirectinput's per-call sleep is unhelpful for the tight reel loop.
pydirectinput.PAUSE = 0.0


class State(enum.Enum):
    UNKNOWN = "unknown"
    IDLE = "idle"
    HOOK_READY = "hook_ready"
    REELING = "reeling"
    CATCH_SCREEN = "catch_screen"


class Fisher:
    def __init__(self, cfg: Config, vision: Vision):
        self.cfg = cfg
        self.vision = vision
        self.state = State.UNKNOWN
        self._panic = threading.Event()
        self._paused = threading.Event()
        # Independent flags — A and D can never be held simultaneously by
        # design, but tracking them separately makes _force_release_all simple.
        self._a_down = False
        self._d_down = False
        # Reel controller continuity across ticks.
        self._last_slider_x: Optional[float] = None
        self._last_target_span: Optional[tuple] = None
        # Counters / diagnostics.
        self._catches = 0
        self._misses = 0
        self._last_diag_log = 0.0
        self._last_reel_log = 0.0
        # When set, suppress action on lower-priority states until this time
        # (used to give the game a beat to repaint after we press a key).
        self._action_lockout_until = 0.0

    # ---- External controls ----

    def panic(self):
        self._panic.set()

    def toggle_pause(self):
        if self._paused.is_set():
            self._paused.clear()
            print("[fisher] resumed")
        else:
            self._paused.set()
            print("[fisher] paused")

    # ---- Input wrappers ----

    def _press(self, key: str):
        pydirectinput.press(key)

    def _hold_left(self):
        if not self._a_down:
            pydirectinput.keyDown(self.cfg.reel_left_key)
            self._a_down = True

    def _release_left(self):
        if self._a_down:
            pydirectinput.keyUp(self.cfg.reel_left_key)
            self._a_down = False

    def _hold_right(self):
        if not self._d_down:
            pydirectinput.keyDown(self.cfg.reel_right_key)
            self._d_down = True

    def _release_right(self):
        if self._d_down:
            pydirectinput.keyUp(self.cfg.reel_right_key)
            self._d_down = False

    def _release_reel_keys(self):
        self._release_left()
        self._release_right()

    def _force_release_all(self):
        for key, attr in (
            (self.cfg.reel_left_key, "_a_down"),
            (self.cfg.reel_right_key, "_d_down"),
        ):
            if getattr(self, attr):
                try:
                    pydirectinput.keyUp(key)
                except Exception:
                    pass
                setattr(self, attr, False)

    def _click_window(self, wx: int, wy: int):
        """Move + mouseDown + small hold + mouseUp at a window-relative point.

        Some Unity-based titles ignore pydirectinput.click() because it doesn't
        produce a real cursor movement event before the click. The explicit
        moveTo + mouseDown/Up sequence below is more reliable.
        """
        sx, sy = self.vision.window_to_screen(wx, wy)
        try:
            pydirectinput.moveTo(sx, sy)
            time.sleep(0.03)
            pydirectinput.mouseDown(button="left")
            time.sleep(self.cfg.click_hold_seconds)
            pydirectinput.mouseUp(button="left")
        except Exception as e:
            print(f"[click] failed at ({sx},{sy}): {e}")

    # ---- Helpers ----

    def _sleep_interruptible(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while True:
            if self._panic.is_set():
                return False
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.05))

    def _wait_unpaused(self) -> bool:
        while self._paused.is_set():
            if not self._sleep_interruptible(0.1):
                return False
        return not self._panic.is_set()

    def _mask_count(self, roi_ref: tuple, hsv_range, debug_name: Optional[str] = None) -> int:
        roi = self.vision.grab_roi(roi_ref)
        if roi.size == 0:
            return 0
        mask = self.vision.hsv_mask(roi, hsv_range)
        if debug_name and self.cfg.debug_mode:
            self.vision.show_debug(debug_name, mask)
        return int(cv2.countNonZero(mask))

    # ---- State detection ----

    def _detect_state(self) -> tuple:
        """Return (state, diagnostics dict). Detector order = priority."""
        diag = {}

        # CATCH first — when the dialog is up it covers most of the screen
        # and overlaps with everything else. A positive catch detection
        # means we should not act on any other ROI's noise.
        catch_px = self._mask_count(
            self.cfg.roi_catch_screen, self.cfg.hsv_catch_xp_bar, "catch_screen"
        )
        diag["catch"] = catch_px
        if catch_px >= self.cfg.catch_min_pixels:
            return State.CATCH_SCREEN, diag

        # REELING — teal target segment in the bar is unique to the minigame.
        # Asymmetric hysteresis: high bar to ENTER, low bar to STAY. Stops
        # the state from bouncing reeling↔unknown when the target briefly
        # slides off the ROI edge.
        reel_px = self._mask_count(
            self.cfg.roi_reel_gauge, self.cfg.hsv_target_zone, "reel_target"
        )
        diag["reel_target"] = reel_px
        threshold = (
            self.cfg.reel_target_stay_min_pixels
            if self.state == State.REELING
            else self.cfg.reel_target_min_pixels
        )
        if reel_px >= threshold:
            return State.REELING, diag

        # HOOK_READY — bright blue ring around F bubble. Check before IDLE
        # because the F bubble is also part of the action-bubble row.
        hook_px = self._mask_count(
            self.cfg.roi_hook_button, self.cfg.hsv_hook_outline, "hook_outline"
        )
        diag["hook"] = hook_px
        if hook_px >= self.cfg.hook_min_pixels:
            return State.HOOK_READY, diag

        # IDLE — multiple white icons across the action-bubble row.
        idle_px = self._mask_count(
            self.cfg.roi_action_bubbles, self.cfg.hsv_bubble_icon, "bubble_icons"
        )
        diag["idle"] = idle_px
        if idle_px >= self.cfg.idle_min_pixels:
            return State.IDLE, diag

        return State.UNKNOWN, diag

    def _maybe_log_diagnostics(self, detected: State, diag: dict):
        now = time.monotonic()
        if now - self._last_diag_log < self.cfg.diag_log_seconds:
            return
        self._last_diag_log = now
        parts = " ".join(f"{k}={v}" for k, v in diag.items())
        held = ("A" if self._a_down else "-") + ("D" if self._d_down else "-")
        print(f"[diag] state={detected.value} keys={held} {parts}")

    # ---- Per-state actions ----

    def _do_idle(self):
        print(f"[idle] action bubbles visible — pressing {self.cfg.cast_key}")
        self._press(self.cfg.cast_key)
        self._action_lockout_until = time.monotonic() + random.uniform(
            self.cfg.post_cast_min_delay, self.cfg.post_cast_max_delay
        )

    def _do_hook_ready(self):
        print(f"[hook] blue ring detected — pressing {self.cfg.hook_key}")
        self._press(self.cfg.hook_key)
        # Give the game time to bring up the minigame UI before the next tick.
        self._action_lockout_until = time.monotonic() + 0.6

    def _do_reel_tick(self):
        roi = self.vision.grab_roi(self.cfg.roi_reel_gauge)
        if roi.size == 0:
            return
        target_mask = self.vision.hsv_mask(roi, self.cfg.hsv_target_zone)
        slider_mask = self.vision.hsv_mask(roi, self.cfg.hsv_slider)

        target_span = self.vision.horizontal_extent(target_mask)
        slider_centroid = self.vision.centroid(slider_mask)

        target_pixels = int(cv2.countNonZero(target_mask))
        slider_pixels = int(cv2.countNonZero(slider_mask))

        # Reject suspiciously wide target spans — if the teal mask matches
        # most of the ROI we're picking up noise (sky/water bleed), not the
        # actual segment.
        if target_span is not None:
            span_w = target_span[1] - target_span[0]
            if span_w > self.cfg.reel_target_max_span_ratio * roi.shape[1]:
                target_span = None

        if target_span is not None:
            self._last_target_span = target_span
        if slider_centroid is not None and slider_pixels >= self.cfg.reel_slider_min_pixels:
            self._last_slider_x = slider_centroid[0]

        span = target_span or self._last_target_span
        slider_x = (
            slider_centroid[0]
            if (slider_centroid is not None and slider_pixels >= self.cfg.reel_slider_min_pixels)
            else self._last_slider_x
        )

        decision = "no-data"
        if span is not None and slider_x is not None:
            decision = self._apply_controller(roi.shape[1], span, slider_x)

        self._maybe_log_reel(roi.shape[1], target_pixels, slider_pixels,
                             span, slider_x, decision)

        if self.cfg.debug_mode:
            self._render_debug(roi, target_mask, slider_mask, span, slider_x)

    def _maybe_log_reel(self, roi_w, target_pixels, slider_pixels,
                        span, slider_x, decision):
        now = time.monotonic()
        if now - self._last_reel_log < self.cfg.reel_log_seconds:
            return
        self._last_reel_log = now
        held = ("A" if self._a_down else "-") + ("D" if self._d_down else "-")
        span_s = f"{span[0]}-{span[1]}" if span else "None"
        sx_s = f"{slider_x:.0f}" if slider_x is not None else "None"
        print(f"[reel] roi_w={roi_w} target_px={target_pixels} slider_px={slider_pixels}"
              f" span={span_s} slider_x={sx_s} keys={held} -> {decision}")

    def _apply_controller(self, roi_w: int, target_span: tuple, slider_x: float) -> str:
        """Aim the slider at the *center* of the target zone.

        Pushing toward the band edges (the previous behaviour) leaves the
        slider resting against the inside edge of the band — and as the
        band drifts, that edge becomes the outside, so the slider is
        constantly almost-out. Pushing toward the center keeps the slider
        in the middle, where it has maximum margin in both directions.
        """
        t_start, t_end = target_span
        target_center = (t_start + t_end) / 2.0
        dz = self.cfg.reel_deadzone * roi_w
        if slider_x < target_center - dz:
            self._release_left()
            self._hold_right()
            return f"push-right(D) center={target_center:.0f}"
        elif slider_x > target_center + dz:
            self._release_right()
            self._hold_left()
            return f"push-left(A) center={target_center:.0f}"
        else:
            self._release_reel_keys()
            return f"centered(release) center={target_center:.0f}"

    def _do_catch_screen(self):
        cx, cy = self.cfg.catch_dismiss_point
        print(f"[catch] dismissing — click @ window({cx},{cy})")
        self._click_window(cx, cy)
        # Don't poll again immediately; give the dialog a moment to close.
        self._action_lockout_until = time.monotonic() + self.cfg.catch_dismiss_delay

    # ---- Reel debug rendering ----

    def _render_debug(self, roi, target_mask, slider_mask, span, slider_x):
        vis = roi.copy()
        if span is not None:
            cv2.rectangle(vis, (span[0], 0), (span[1], vis.shape[0] - 1),
                          (0, 255, 0), 1)
        if slider_x is not None:
            cv2.line(vis, (int(slider_x), 0), (int(slider_x), vis.shape[0] - 1),
                     (0, 255, 255), 2)
        held = ("A" if self._a_down else "-") + ("D" if self._d_down else "-")
        cv2.putText(vis, held, (4, 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1, cv2.LINE_AA)
        masks = cv2.cvtColor(cv2.bitwise_or(target_mask, slider_mask),
                             cv2.COLOR_GRAY2BGR)
        h = max(vis.shape[0], masks.shape[0])
        pad_v = np.zeros((h, vis.shape[1], 3), dtype=np.uint8); pad_v[:vis.shape[0]] = vis
        pad_m = np.zeros((h, masks.shape[1], 3), dtype=np.uint8); pad_m[:masks.shape[0]] = masks
        self.vision.show_debug("reel_gauge", np.hstack([pad_v, pad_m]))

    # ---- Main loop ----

    def _on_state_transition(self, prev: State, new: State):
        if prev == new:
            return
        # Always lift reel keys when leaving REELING.
        if prev == State.REELING:
            self._release_reel_keys()
        # Don't clear _last_slider_x / _last_target_span here — keep them as
        # a warm cache. If the next reel starts within a few seconds (very
        # common, since brief detection dips can briefly drop us out of
        # REELING and right back in), the controller has fallback values
        # for the first few ticks until fresh detections arrive. Stale
        # values get overwritten on the first successful detection.
        if new == State.CATCH_SCREEN and prev == State.REELING:
            self._catches += 1
        print(f"[state] {prev.value} -> {new.value}")

    def run(self):
        print("[fisher] starting. F9=panic stop, F10=pause/resume.")
        try:
            while not self._panic.is_set():
                if not self._wait_unpaused():
                    break
                tick_start = time.monotonic()

                detected, diag = self._detect_state()
                self._maybe_log_diagnostics(detected, diag)

                if detected != self.state:
                    self._on_state_transition(self.state, detected)
                    self.state = detected

                # REELING and (sometimes) CATCH_SCREEN run regardless of
                # lockout — they're real states the game is in, not actions
                # we just took. IDLE and HOOK_READY presses honor lockout so
                # we don't double-press while the game repaints.
                respect_lockout = detected in (State.IDLE, State.HOOK_READY)
                in_lockout = tick_start < self._action_lockout_until

                if detected == State.REELING:
                    self._do_reel_tick()
                    elapsed = time.monotonic() - tick_start
                    remain = self.cfg.reel_tick_seconds - elapsed
                    if remain > 0:
                        time.sleep(remain)
                    continue

                if detected == State.CATCH_SCREEN:
                    if not in_lockout:
                        self._do_catch_screen()
                    self._sleep_interruptible(self.cfg.catch_poll_seconds)
                    continue

                if detected == State.HOOK_READY:
                    if not in_lockout:
                        self._do_hook_ready()
                    self._sleep_interruptible(self.cfg.hook_poll_seconds)
                    continue

                if detected == State.IDLE:
                    if not in_lockout:
                        self._do_idle()
                    self._sleep_interruptible(self.cfg.cast_poll_seconds)
                    continue

                # UNKNOWN — slow poll, do nothing.
                self._sleep_interruptible(0.15)
        finally:
            self._force_release_all()
            print(f"[fisher] stopped. caught={self._catches} missed={self._misses}")
