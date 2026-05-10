# nte-autofish

auto fishing script for neverness to everness.

watches the screen, casts when it can, plays the minigame when a fish
bites, dismisses the catch screen, repeat. Does not auto buy bait or anything so buy a ton of bait and then leave it running.

## install

```bash
pip install -r requirements.txt
```

## run

```bash
python main.py
```

you have 4 seconds to open up the game window before it starts.

- **F9** stop
- **F10** pause / resume

## tuning

if it's not working right, run with `--debug` to see the opencv vision. all the numbers to tweak are in [config.py](config.py).
each one has a comment explaining what it does.
