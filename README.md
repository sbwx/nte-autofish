# nte-autofish

auto fishing script for *neverness to everness*.

watches the screen, casts when it can, plays the minigame when a fish
bites, dismisses the catch screen, repeat.

## install

```bash
pip install -r requirements.txt
```

windows only.

## run

```bash
python main.py
```

you have 4 seconds to click on the game window before it starts.

- **F9** stop
- **F10** pause / resume

## tuning

if it's not working right, run with `--debug` to see what the script is
looking at. all the numbers to tweak are in [config.py](config.py).
each one has a comment explaining what it does.
