"""
run: python main.py [--debug]

hotkeys:
    F9   stop
    F10  pause / resume

we don't focus the game window for you. click on the game first, then
the script will start sending keys to whatever window is in focus.
"""
from __future__ import annotations

import argparse
import sys
import time

import keyboard

from config import Config
from fisher import Fisher
from vision import Vision


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NTE auto-fisher")
    p.add_argument("--debug", action="store_true",
                   help="show opencv windows of what the script sees")
    p.add_argument("--no-thread", action="store_true",
                   help="grab frames in the foreground (for debugging)")
    p.add_argument("--countdown", type=int, default=4,
                   help="seconds to wait before starting (so you can focus the game)")
    return p.parse_args()


def install_hotkeys(fisher: Fisher, cfg: Config) -> None:
    keyboard.add_hotkey(cfg.panic_key, fisher.panic)
    keyboard.add_hotkey(cfg.pause_key, fisher.toggle_pause)


def countdown(seconds: int):
    for i in range(seconds, 0, -1):
        print(f"  starting in {i}...")
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
