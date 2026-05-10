"""
Entry point for the NTE auto-fisher.

Run: python main.py [--debug]

Hotkeys:
    F9   — panic stop (exits immediately)
    F10  — pause / resume

The script focuses the game window? No — it does not. Bring the game to the
foreground yourself before starting; pydirectinput sends keys to whichever
window currently has focus.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import keyboard

from config import Config
from fisher import Fisher
from vision import Vision


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NTE auto-fisher")
    p.add_argument("--debug", action="store_true",
                   help="Show OpenCV debug windows for each ROI mask.")
    p.add_argument("--no-thread", action="store_true",
                   help="Disable the background capture thread (debugging).")
    p.add_argument("--countdown", type=int, default=4,
                   help="Seconds to wait before starting (focus the game).")
    return p.parse_args()


def install_hotkeys(fisher: Fisher, cfg: Config) -> None:
    keyboard.add_hotkey(cfg.panic_key, fisher.panic)
    keyboard.add_hotkey(cfg.pause_key, fisher.toggle_pause)


def countdown(seconds: int):
    for i in range(seconds, 0, -1):
        print(f"  starting in {i}…")
        time.sleep(1)


def main() -> int:
    args = parse_args()
    cfg = Config()
    if args.debug:
        cfg.debug_mode = True
    if args.no_thread:
        cfg.use_capture_thread = False

    print("[main] focus the NTE window now.")
    countdown(args.countdown)

    vision = Vision(cfg)
    fisher = Fisher(cfg, vision)
    install_hotkeys(fisher, cfg)

    try:
        fisher.run()
    except KeyboardInterrupt:
        fisher.panic()
    finally:
        vision.close()
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
