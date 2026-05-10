"""
rois are written in 1920x1080 numbers. they get scaled to the actual
window size at runtime, so the same numbers work no matter what
resolution you play at, as long as the in-game ui scale is the same.

roi format: (x, y, w, h). top-left origin. pixels.
"""
from dataclasses import dataclass, field
import numpy as np


REFERENCE_W = 1920
REFERENCE_H = 1080


@dataclass
class HSVRange:
    """color range. opencv uses h 0-180, s/v 0-255.
    set wrap=true for colors that wrap around the edge of the hue wheel
    (basically only red).
    """
    lower: tuple
    upper: tuple
    wrap: bool = False

    def as_arrays(self):
        return np.array(self.lower, dtype=np.uint8), np.array(self.upper, dtype=np.uint8)


@dataclass
class Config:
    # ---- finding the game window ----
    # part of the title to look for. change if your game is named
    # something else.
    window_title: str = "Neverness to Everness"
    fallback_fullscreen: bool = True  # if the window isn't found just grab the whole screen

    # ---- keys ----
    cast_key: str = "f"          # press to cast
    hook_key: str = "f"          # press to start the minigame
    reel_left_key: str = "a"     # held to push the slider left
    reel_right_key: str = "d"    # held to push the slider right
    panic_key: str = "f9"        # emergency stop
    pause_key: str = "f10"       # pause / resume

    # ---- where to look on screen (1920x1080 numbers) ----
    # the row of bubbles in the bottom-right (R Q E F). if all four are
    # there, we're idle and can cast.
    roi_action_bubbles: tuple = (1370, 905, 460, 75)
    # just the F bubble. it gets a blue ring around it when a fish bites.
    roi_hook_button: tuple = (1750, 905, 80, 75)
    # the fishing bar at the top. wide enough that the whole bar fits.
    # the two end circles bleed into it but that's handled below per
    # color:
    #   teal: pick the biggest matching chunk (the target rectangle is
    #     way bigger than any ring slice, so this works)
    #   yellow: cut off the leftmost/rightmost pixels (slider and ring
    #     slice are similar size so we have to chop them off by position)
    roi_reel_gauge: tuple = (770, 78, 400, 24)
    # how many pixels to chop off each end of the reel roi (in actual
    # screen pixels, not the 1920x1080 numbers above). only applied to
    # the yellow mask.
    reel_edge_mask_px: int = 35
    # the pink xp bar at the top of the catch dialog. one of two
    # signals we use for the catch screen.
    roi_catch_screen: tuple = (760, 80, 400, 80)
    # backup catch-screen signal: the white "Press empty area to close"
    # italic text near the bottom of the dialog. doesn't depend on the
    # user's xp, time of day, fish type, or catch grade. always there,
    # always white. the only thing that could false-fire is incidental
    # bright-white pixels in this strip during normal fishing, which
    # shouldn't happen at the player's feet on the dock.
    roi_catch_text: tuple = (660, 920, 600, 60)

    # ---- color ranges ----
    # quick guide:
    #   h: 0=red, 30=orange, 60=yellow, 90=green, 120=cyan, 150=blue,
    #      165=magenta. opencv uses 0-180.
    #   s: how vivid. low s = washed out / grayish. raise it to skip ui
    #      grays and beige water.
    #   v: how bright. raise it to skip dark stuff.
    # if the lighting changes (day/night), widen v. don't widen h or
    # you'll start matching random ui.

    # white icons inside the action bubbles. trying to match the dark
    # bubble background doesn't work because they're see-through.
    hsv_bubble_icon: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(0, 0, 220), upper=(180, 50, 255)
    ))

    # blue ring around the F bubble when a fish is on. keep s high so
    # the sky doesn't trigger it.
    hsv_hook_outline: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(95, 150, 180), upper=(120, 255, 255)
    ))

    # the moving teal section in the bar. tight on purpose, the sky
    # leans cyan-ish and if this gets any wider the whole sky lights up
    # and we get stuck thinking we're reeling on launch.
    hsv_target_zone: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(80, 130, 120), upper=(98, 255, 255)
    ))

    # the yellow slider. a bit looser than pure yellow so the soft edges
    # of the line still count, but tight enough that the sun and clouds
    # don't trigger it.
    hsv_slider: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(15, 100, 150), upper=(38, 255, 255)
    ))

    # pink fill on the catch dialog's xp bar.
    hsv_catch_xp_bar: HSVRange = field(default_factory=lambda: HSVRange(
        lower=(155, 150, 200), upper=(175, 255, 255)
    ))

    # ---- how many matching pixels count as "found" ----
    # idle: white pixels from all four bubble icons. one bubble is
    # roughly 50-80 pixels of white, so 200 needs more than just one.
    idle_min_pixels: int = 200
    hook_min_pixels: int = 60        # the blue ring is thin
    # different thresholds for entering vs staying in REELING. needs a
    # solid signal to enter, then a tiny one to stay. stops the state
    # from flickering when the target briefly slides off the edge.
    reel_target_min_pixels: int = 25
    reel_target_stay_min_pixels: int = 5
    reel_slider_min_pixels: int = 5
    # how much pink we need to see to call it a catch screen. low
    # because the xp bar's fill width depends on the user's xp level,
    # so low-level users have very little pink on screen. set above any
    # random pink noise (other states have 0). text detector below
    # backs this up if pink truly fails.
    catch_min_pixels: int = 50
    # how many white pixels in roi_catch_text mean the dismissal text
    # is showing. the text "Press empty area to close" is dozens of
    # characters with anti-aliased white edges, so the real count is
    # in the thousands. set conservatively to allow for font size
    # variations across resolutions.
    catch_text_min_pixels: int = 500

    # ---- reel controller ----
    # how close to the center counts as "good enough". fraction of the
    # roi width. smaller = stays closer to center but jitters more.
    reel_deadzone: float = 0.02
    reel_tick_seconds: float = 0.012     # how often the controller runs
    reel_timeout_seconds: float = 30.0   # give up if a reel never ends
    gauge_lost_grace: float = 0.6        # how long the bar can be gone before we call it done
    reel_log_seconds: float = 0.25       # how often to print the reel debug line

    # ---- closing the catch dialog ----
    # places to click to dismiss it, in 1920x1080 reference space (same
    # coordinate system as the rois). we try them in order, cycling on
    # each retry, in case the first one lands on something that doesn't
    # actually dismiss on someone's setup.
    # (37, 405) is the ref-space version of (50, 540) at 2560x1440,
    # which is what the dev tested with. the rest are spread-out
    # fallbacks in spots that look empty.
    catch_dismiss_points: tuple = (
        (37, 405),    # left side, mid-height
        (10, 700),    # bottom-left, below the friends list
        (1200, 950),  # bottom center
        (1850, 600),  # right side, mid-height
    )
    # the dialog takes about a second to fade out. wait long enough that
    # we don't immediately think we're still on the catch screen.
    catch_dismiss_delay: float = 1.5

    # ---- how often to check things ----
    cast_poll_seconds: float = 0.25
    hook_poll_seconds: float = 0.08
    hook_timeout_seconds: float = 60.0   # generous, fish can take a while
    catch_poll_seconds: float = 0.15
    catch_timeout_seconds: float = 8.0
    post_cast_min_delay: float = 0.6     # random wait after casting so it doesn't look robotic
    post_cast_max_delay: float = 1.4
    post_reel_min_delay: float = 1.2
    post_reel_max_delay: float = 2.4

    # ---- misc ----
    debug_mode: bool = False             # pop opencv windows showing what we see
    use_capture_thread: bool = True      # grab frames in the background
    diag_log_seconds: float = 1.0        # how often to print the state line
    # how the click is sent. explicit move + down + wait + up works on
    # unity games where pydirectinput.click() sometimes does nothing.
    click_hold_seconds: float = 0.06
