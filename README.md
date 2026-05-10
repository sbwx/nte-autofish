# nte-autofish

Auto fishing script for *Neverness to Everness* using OpenCV and mss.

## What it does

A small state machine watches the game window and drives the fishing flow
through NTE's four real states:

1. **IDLE** — the action-bubble row at bottom-right (R / Q / E / F) is fully
   visible. Script presses **F** to cast.
2. **HOOK_READY** — only the F bubble remains, ringed in bright blue. Script
   presses **F** again to start the minigame.
3. **REELING** — top-center fishing bar shows a yellow vertical slider that
   must stay inside a moving teal target segment. Script runs a two-key
   hysteresis controller: hold **D** to push the slider right when it drifts
   past the left edge of the band, hold **A** to push it left when it drifts
   past the right edge, release both inside the band.
4. **CATCH_SCREEN** — fish-result dialog with the pink XP bar at the top.
   Script clicks an empty corner to dismiss it.
5. **COOLDOWN** — randomized delay before the next IDLE poll.

Each cast cycle therefore sends **two F presses** (cast, then engage), some
A/D holds during the minigame, and one mouse click to close the result.

## Install

```bash
pip install -r requirements.txt
```

Tested with Python 3.12. `pydirectinput` and `keyboard` are Windows-oriented;
on Linux/WSL you can import the modules and tune HSV ranges, but key injection
into a Windows game requires running on Windows.

## Run

```bash
python main.py            # normal run
python main.py --debug    # also pop OpenCV windows for each mask
```

Hotkeys (global):

- **F9** — panic stop (releases held keys and exits).
- **F10** — pause / resume.

You have a 4-second countdown to focus the NTE window before the loop starts.

## Tuning

All knobs live in [config.py](config.py):

- **ROIs** — `roi_action_bubbles`, `roi_hook_button`, `roi_reel_gauge`, and
  `roi_catch_screen` are authored in 1920x1080 reference space; the Vision
  class scales them to whatever size the game window is at runtime. Tune them
  once against your real UI by running `--debug` and watching the mask
  windows (`action_bubbles`, `hook_outline`, `reel_gauge`, `catch_screen`).
- **HSV ranges** — each `HSVRange` has tuning notes inline. Rule of thumb:
  if the mask is too noisy, raise `S` and `V` minimums; if it drops out under
  certain lighting, widen `V` only — never widen `H` or you'll start matching
  unrelated UI colors.
- **`reel_deadzone`** — bigger = calmer controller, smaller = tighter
  tracking. Start at 0.06 and adjust if the slider oscillates.
- **`catch_dismiss_point`** — window-relative pixel where the script clicks
  to close the catch dialog. Default is `(100, 100)` (top-left); change it if
  any UI in your build reaches into that corner.

## Files

- [config.py](config.py) — ROIs, HSV ranges, keybinds, timings.
- [vision.py](vision.py) — mss capture (with optional background thread),
  HSV masking, centroid + horizontal-extent helpers, window auto-locate.
- [fisher.py](fisher.py) — state machine and hysteresis controller.
- [main.py](main.py) — entry point + hotkeys.

## Safety notes

- The script never holds a key past the end of the reel state (`finally`
  clause in `Fisher.reel`), and a hard panic via F9 always lifts whatever is
  down before exit.
- Use at your own risk; check the game's TOS before running.
