# WaveStack — Setup Guide

WaveStack is a fully offline, 1990s-styled MP3 player built with Python,
Tkinter, and libVLC. This guide gets it installed as a native launcher
entry on Ubuntu 26.04.

Every command below was run and verified against a real Ubuntu
environment (venv creation, dependency install, `.desktop` validation,
and an actual launch) before being included here.

## 1. Get the files in place

Put `wavestack.py`, `requirements.txt`, `run.sh`, and `WaveStack.desktop`
all in the same folder:

```bash
mkdir -p ~/WaveStack
# copy the four downloaded files into ~/WaveStack/
cd ~/WaveStack
```

## 2. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3-venv python3-tk vlc
```

- `python3-tk` provides the Tkinter GUI toolkit (not a pip package).
- `vlc` provides `libvlc`, the actual audio engine `python-vlc` binds to.

## 3. Create the virtual environment and install Python deps

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

(Tkinter does not need to be pip-installed into the venv — venvs
automatically see the standard library, including `_tkinter`, once
`python3-tk` is installed system-wide.)

## 4. Make the scripts executable

```bash
chmod +x wavestack.py run.sh
```

## 5. Install the app launcher

```bash
mkdir -p ~/.local/share/applications
cp WaveStack.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
```

WaveStack should now appear in your GNOME/KDE app launcher — search for
"WaveStack". If it doesn't show up immediately, log out and back in.

## Notes

- **Default music folder:** WaveStack looks for `.mp3` files in
  `/home/syed-ali-ahmed-shah/Music` on startup. If your username is
  different, either rename that folder to match, or open
  `wavestack.py` and change the `DEFAULT_MUSIC_DIR` constant near the
  top of the file. If the folder is missing or empty, WaveStack shows
  a retro-styled dialog and lets you browse for a different folder
  instead of crashing.
- **The `WaveStack.desktop` file's `Exec=` line is hardcoded** to
  `/home/syed-ali-ahmed-shah/WaveStack/run.sh`. If you place the
  project folder somewhere else, update that line to match.
- **Running it manually** (e.g. to see errors in a terminal) instead
  of via the launcher:
  ```bash
  cd ~/WaveStack
  ./run.sh
  ```
- **Uninstalling:** `rm ~/.local/share/applications/WaveStack.desktop`
  and delete the `~/WaveStack` folder.
