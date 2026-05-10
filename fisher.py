"""
every tick we look at the screen and figure out which of these we're in:

    catch_screen > reeling > hook_ready > idle > unknown

then we do whatever that state needs. priority is left to right above,
so e.g. if the catch screen is up we don't try to detect anything else.

if one of the checks is broken, the others still work. the script will
just skip whatever's broken and pick up at the next thing it can see.

global signals:
    panic   stop everything and exit
    paused  freeze the loop until unpaused
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

from config import Config, REFERENCE_W, REFERENCE_H
from vision import Vision

# pydirectinput sleeps for a tiny moment between calls by default which
# is bad for the fast reel loop, so turn it off
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
        # A and D are tracked separately so we can lift them one at a
        # time on exit
        self._a_down = False
        self._d_down = False
        # remember the last known slider/target spot, used as a fallback
        # if the current frame can't see them
        self._last_slider_x: Optional[float] = None
        self._last_target_span: Optional[tuple] = None
        # counters
        self._catches = 0
        self._misses = 0
        self._last_diag_log = 0.0
        self._last_reel_log = 0.0
        # after pressing a key, ignore the next few state checks for
        # this long. stops us from double-pressing while the game is
        # still drawing the new screen.
        self._action_lockout_until = 0.0
        # which dismiss point to try next on the catch screen. cycles
        # through cfg.catch_dismiss_points, resets when we leave the
        # catch screen state.
        self._catch_dismiss_attempt = 0

    # ---- outside controls ----

    def panic(self):
        self._panic.set()

    def toggle_pause(self):
        if self._paused.is_set():
            self._paused.clear()
            print("[fisher] resumed")
        else:
            self._paused.set()
            print("[fisher] paused")

    # ---- key/mouse stuff ----

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
        """move the mouse, hold the button briefly, let go.
        the explicit move/down/up sequence is more reliable than
        pydirectinput.click() against some unity games.
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

    # ---- helpers ----

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

    def _apply_edge_mask(self, mask: np.ndarray) -> None:
        """zero out the leftmost/rightmost few pixels.
        the bar has icons on either end with the same colors as the
        slider, this just chops them off so they don't confuse us.
        """
        edge = self.cfg.reel_edge_mask_px
        if edge > 0 and mask.shape[1] > 2 * edge:
            mask[:, :edge] = 0
            mask[:, -edge:] = 0

    def _catch_text_count(self) -> int:
        """count white pixels in the dismissal-text strip at the bottom
        of the catch dialog. independent of xp level and time of day,
        so this works even when the pink xp bar is tiny (low-level
        users) or when the sky is dark (night fishing)."""
        return self._mask_count(
            self.cfg.roi_catch_text, self.cfg.hsv_bubble_icon, "catch_text"
        )

    def _reel_target_count(self, debug_name: Optional[str] = None) -> int:
        """count only the biggest teal chunk. the right-side icon also
        has some teal but it's tiny next to the actual target rectangle,
        so picking the biggest chunk ignores it."""
        roi = self.vision.grab_roi(self.cfg.roi_reel_gauge)
        if roi.size == 0:
            return 0
        mask = self.vision.hsv_mask(roi, self.cfg.hsv_target_zone)
        if debug_name and self.cfg.debug_mode:
            self.vision.show_debug(debug_name, mask)
        _, area = self.vision.largest_blob_extent(mask)
        return area

    # ---- figuring out what state we're in ----

    def _detect_state(self) -> tuple:
        """returns (state, dict of how many pixels each check saw)."""
        diag = {}

        # check the catch screen first. when it's up it covers most of
        # the screen so we don't want to act on anything else.
        # two independent signals, either one fires:
        #   1. pink xp bar pixels (varies a lot with the user's xp level)
        #   2. white dismissal text (always there regardless of xp / time
        #      of day / fish type / grade)
        catch_px = self._mask_count(
            self.cfg.roi_catch_screen, self.cfg.hsv_catch_xp_bar, "catch_screen"
        )
        catch_text_px = self._catch_text_count()
        diag["catch"] = catch_px
        diag["catch_text"] = catch_text_px
        if (catch_px >= self.cfg.catch_min_pixels
                or catch_text_px >= self.cfg.catch_text_min_pixels):
            return State.CATCH_SCREEN, diag

        # reeling. teal target only shows up during the minigame.
        # different thresholds for entering vs staying so we don't
        # bounce in and out of state when the target slides off the edge
        # for a frame.
        reel_px = self._reel_target_count("reel_target")
        diag["reel_target"] = reel_px
        threshold = (
            self.cfg.reel_target_stay_min_pixels
            if self.state == State.REELING
            else self.cfg.reel_target_min_pixels
        )
        if reel_px >= threshold:
            return State.REELING, diag

        # check for the blue ring before checking idle, because the F
        # bubble is part of the bubble row too.
        hook_px = self._mask_count(
            self.cfg.roi_hook_button, self.cfg.hsv_hook_outline, "hook_outline"
        )
        diag["hook"] = hook_px
        if hook_px >= self.cfg.hook_min_pixels:
            return State.HOOK_READY, diag

        # idle: enough white pixels in the bubble row to mean all the
        # icons are there
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

    # ---- what to do in each state ----

    def _do_idle(self):
        print(f"[idle] action bubbles visible, pressing {self.cfg.cast_key}")
        self._press(self.cfg.cast_key)
        self._action_lockout_until = time.monotonic() + random.uniform(
            self.cfg.post_cast_min_delay, self.cfg.post_cast_max_delay
        )

    def _do_hook_ready(self):
        print(f"[hook] blue ring detected, pressing {self.cfg.hook_key}")
        self._press(self.cfg.hook_key)
        # give the game a moment to bring up the minigame
        self._action_lockout_until = time.monotonic() + 0.6

    def _do_reel_tick(self):
        roi = self.vision.grab_roi(self.cfg.roi_reel_gauge)
        if roi.size == 0:
            return
        target_mask = self.vision.hsv_mask(roi, self.cfg.hsv_target_zone)
        slider_mask = self.vision.hsv_mask(roi, self.cfg.hsv_slider)

        # for the slider, chop off the edges where the yellow icon ring
        # would be
        self._apply_edge_mask(slider_mask)
        # for the target, don't chop. instead we pick the biggest teal
        # chunk below, which ignores the small teal bits on the icons.
        # not chopping is what lets the target reach the actual edges of
        # the bar instead of being cut short.

        target_span, target_pixels = self.vision.largest_blob_extent(target_mask)
        slider_centroid, slider_pixels = self.vision.largest_blob_centroid(slider_mask)

        if target_span is not None:
            self._last_target_span = target_span
        if slider_centroid is not None and slider_pixels >= self.cfg.reel_slider_min_pixels:
            self._last_slider_x = slider_centroid[0]

        # use what we just saw, or fall back to the last known position
        # if this frame couldn't see anything
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
        """push the slider toward the middle of the teal target.
        if we just push toward the band edges, the slider sits on the
        inside of the edge, and as the band moves that edge becomes the
        outside and we're suddenly out. aiming for the middle gives us
        room on both sides.
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
        # cycle through the fallback points so if the first one doesn't
        # actually dismiss the dialog we'll try a different spot next time
        points = self.cfg.catch_dismiss_points
        cx_ref, cy_ref = points[self._catch_dismiss_attempt % len(points)]
        # scale 1920x1080 ref coords to the actual window size, same way
        # rois are scaled
        w = self.vision.window
        cx = int(cx_ref * w.width / REFERENCE_W)
        cy = int(cy_ref * w.height / REFERENCE_H)
        print(f"[catch] dismissing #{self._catch_dismiss_attempt + 1}, "
              f"click @ window({cx},{cy})")
        self._click_window(cx, cy)
        self._catch_dismiss_attempt += 1
        # wait a sec for the dialog to close
        self._action_lockout_until = time.monotonic() + self.cfg.catch_dismiss_delay

    # ---- debug drawing for the reel ----

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

    # ---- main loop ----

    def _on_state_transition(self, prev: State, new: State):
        if prev == new:
            return
        # always let go of A and D when we leave reeling
        if prev == State.REELING:
            self._release_reel_keys()
        # we deliberately don't wipe _last_slider_x or _last_target_span
        # here. they're used as a fallback so brief detection blips
        # don't leave the controller blank for a tick or two when we
        # come back. stale values get overwritten as soon as a real
        # detection succeeds.
        if new == State.CATCH_SCREEN and prev == State.REELING:
            self._catches += 1
        # reset the dismiss-attempt counter when we leave the catch
        # screen so the next catch starts fresh from the first point
        if prev == State.CATCH_SCREEN and new != State.CATCH_SCREEN:
            self._catch_dismiss_attempt = 0
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

                in_lockout = tick_start < self._action_lockout_until

                # reeling runs every tick no matter what. the game is
                # driving the bar, not us, and the controller needs to
                # see every frame.
                if detected == State.REELING:
                    self._do_reel_tick()
                    elapsed = time.monotonic() - tick_start
                    remain = self.cfg.reel_tick_seconds - elapsed
                    if remain > 0:
                        time.sleep(remain)
                    continue

                # the click respects the lockout so we don't spam clicks
                # while the dialog is still fading
                if detected == State.CATCH_SCREEN:
                    if not in_lockout:
                        self._do_catch_screen()
                    self._sleep_interruptible(self.cfg.catch_poll_seconds)
                    continue

                # idle and hook also respect the lockout so we don't
                # double-press while the game is still drawing
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

                # nothing detected. wait a bit and try again.
                self._sleep_interruptible(0.15)
        finally:
            self._force_release_all()
            print(f"[fisher] stopped. caught={self._catches} missed={self._misses}")
