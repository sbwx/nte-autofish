"""
Static configuration for the NTE auto-fisher.

ROIs are authored against a 1920x1080 reference. At runtime the Vision class
scales them to the actual game-window resolution so the script works at any
size as long as the in-game UI scale is unchanged.

ROI format: (x, y, w, h) — top-left origin, in pixels.
"""
from dataclasses import dataclass, field
import numpy as np


REFERENCE_W = 1920
REFERENCE_H = 1080


@dataclass
class HSVRange:
    """An inclusive HSV range. OpenCV uses H in [0,180], S/V in [0,255].

    For colors that wrap around the hue boundary (pure red), set `wrap=True`
    and the Vision class will OR two masks at H=0 and H=180.
    """
    lower: tuple
    upper: tuple
    wrap: bool = False

    def as_arrays(self):
        return np.array(self.lower, dtype=np.uint8), np.array(self.upper, dtype=np.uint8)


@dataclass
class Config:
    # ---- Window targeting ----
    # Substring matched against window titles (case-insensitive). NTE's window
    # title is region-dependent; adjust if your build uses a different name.
    window_title: str = "Neverness to Everness"
    fallback_fullscreen: bool = True  # if window not found, capture primary monitor

    # ---- Keybinds ----
    cast_key: str = "f"          # press to cast from IDLE
    hook_key: str = "f"          # press to start the minigame from HOOK_READY
    reel_left_key: str = "a"     # held to push the slider left
    reel_right_key: str = "d"    # held to push the slider right
    panic_key: str = "f9"        # global hard-stop hotkey
    pause_key: str = "f10"       # toggle pause/resume

    # ---- ROIs in 1920x1080 reference space ----
    # Bottom-right action-bubble row (R, Q, E, F). Used to detect IDLE: if the
    # row is fully populated with dark bubble shapes we're free to cast.
    roi_action_bubbles: tuple = (1370, 905, 460, 75)
    # Just the F bubble. Used to detect HOOK_READY by the bright blue ring
    # the game draws around it once a fish is on the line.
    roi_hook_button: tuple = (1750, 905, 80, 75)
    # Top-center fishing bar — narrowed to just the bar region (excludes the
    # fish-stamina and line-tension end-cap icons so their colors don't bleed
    # into the target/slider masks).
    roi_reel_gauge: tuple = (770, 60, 380, 50)
    # Pink/magenta XP bar that appears at the very top of the catch dialog.
    # Most distinctive non-blurred element on the catch screen.
    roi_catch_screen: tuple = (760, 80, 400, 80)

    # ---- HSV thresholds ----
    # Tuning notes:
    #   H (hue): the color itself. 0=red, 30=orange, 60=yellow, 90=green,
    #            120=cyan, 150=blue, 165=magenta. (OpenCV scale: 0-180)
    #   S (sat): how vivid. Low S = washed-out / grayish. Pull S>=80 to reject
    #            UI grays and beige water tones.
    #   V (val): brightness. Pull V>=120 to reject dark backgrounds.
    # If the game changes lighting (day/night, weather), widen V — never widen
    # H, since that picks up unrelated UI colors.

    # White icons inside the action bubbles (R/Q/E/F glyphs). Trying to match
    # the *dark* bubble background is unreliable because the bubbles are
    # semi-transparent over the game scene. The white icons are stable.
    hsv_bubble_icon: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(0, 0, 220), upper=(180, 50, 255)
    ))

    # Bright blue outline around the F bubble when a fish is on the line.
    # Slightly cyan-leaning; keep S high to reject sky/water.
    hsv_hook_outline: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(95, 150, 180), upper=(120, 255, 255)
    ))

    # Teal/cyan-green moving segment in the reeling gauge.
    hsv_target_zone: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(80, 100, 150), upper=(95, 255, 255)
    ))

    # Yellow vertical slider line in the reeling gauge. Saturated, very bright.
    hsv_slider: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(22, 150, 200), upper=(35, 255, 255)
    ))

    # Pink/magenta XP fill on the catch dialog's level bar.
    hsv_catch_xp_bar: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(155, 150, 200), upper=(175, 255, 255)
    ))

    # ---- Detection thresholds ----
    # IDLE: white from all four bubble icons. One bubble is ~50–80px of white,
    # so 200 requires meaningfully more than a single bubble's worth.
    idle_min_pixels: int = 200
    hook_min_pixels: int = 60        # thin blue ring is sparse
    reel_target_min_pixels: int = 25 # min teal pixels to call it REELING
    reel_slider_min_pixels: int = 15 # min yellow pixels for slider centroid
    catch_min_pixels: int = 200      # bright XP bar fill

    # ---- Reel controller ----
    # Hysteresis deadzone in normalized gauge units (0..1). The slider must
    # cross past target_start - dz / target_end + dz before the controller
    # flips state. Larger => calmer. Smaller => tighter tracking.
    reel_deadzone: float = 0.06
    reel_tick_seconds: float = 0.012     # loop period inside the minigame
    reel_timeout_seconds: float = 30.0   # bail out if a reel never finishes
    gauge_lost_grace: float = 0.6        # gauge must be gone this long => done

    # ---- Catch dismissal ----
    # Window-relative coords for the click that closes the catch dialog. The
    # top-left of the play area is reliably empty in all four reference shots.
    catch_dismiss_point: tuple = (100, 100)
    catch_max_dismiss_attempts: int = 3
    catch_dismiss_delay: float = 0.4

    # ---- State timing ----
    cast_poll_seconds: float = 0.25
    hook_poll_seconds: float = 0.08
    hook_timeout_seconds: float = 60.0   # generous — fish can take a while
    catch_poll_seconds: float = 0.15
    catch_timeout_seconds: float = 8.0   # safety net only; usually < 2s
    post_cast_min_delay: float = 0.6     # human-ish jitter window after cast
    post_cast_max_delay: float = 1.4
    post_reel_min_delay: float = 1.2     # delay between catch and next cast
    post_reel_max_delay: float = 2.4

    # ---- Misc ----
    debug_mode: bool = False             # show OpenCV debug windows
    use_capture_thread: bool = True      # background frame grabber
    diag_log_seconds: float = 1.0        # how often to log mask pixel counts
    # Mouse click cadence — explicit move + down + delay + up is more reliable
    # than pydirectinput.click() against some Unity titles.
    click_hold_seconds: float = 0.06
