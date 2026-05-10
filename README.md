# nte-autofish

auto fishing script for neverness to everness.

watches the screen, casts when it can, plays the minigame when a fish
bites, dismisses the catch screen, repeat. Does not auto buy bait or anything so buy a ton of bait before leaving it running.

## Installing

written for Windows 11. double-click **install.bat**.

it checks for python, installs it via winget if missing, makes a venv
and installs the packages.

## Running the script

double-click **run.bat**.

you have 4 seconds to open up the game window before it starts.

- **F9** stop
- **F10** pause / resume

## Tuning

if it's not working right, run with `--debug` to see the opencv vision. all the numbers to tweak are in [config.py](config.py).
each one has a comment explaining what it does.
