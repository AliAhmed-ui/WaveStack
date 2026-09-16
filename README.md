# WaveStack

A fully offline, 1990s-styled MP3 player for Linux desktops, built with
Python, Tkinter, and libVLC.

No lyrics fetching. No album art scraping. No telemetry. Just your
local MP3 collection, styled like it's 1996.

![WaveStack running with a loaded library and queue](screenshot.png)

## Features

- **Strict retro aesthetic** — thick raised/sunken borders, classic
  gray (`#C0C0C0`) chrome, monospace fonts, and a scrolling green LCD
  "now playing" marquee, all built from classic Tkinter widgets (not a
  themed skin, so it doesn't inherit your desktop's modern GTK/Qt look)
- **Full playback controls** — Play, Pause, Stop, Next, Previous
- **Library + Queue** — browse your music folder, multi-select tracks
  to enqueue, remove from the queue, right-click context menus
- **Working seek bar** — drag to scrub through a track, powered by
  libVLC for reliable seeking, including on variable-bitrate MP3s
- **Volume slider**
- **Open Folder** — load music from anywhere, not just the default
  directory
- **100% offline** — zero network calls, ever
- **Won't crash on bad input** — missing/empty folders and corrupted
  files are handled with retro-styled dialogs, not stack traces

## Requirements

- Ubuntu 26.04 (or any modern Linux desktop running GNOME/KDE)
- Python 3.8+
- VLC (`libvlc`) — the audio engine
- Tkinter (`python3-tk`) — the GUI toolkit

## Quick Start

```bash
mkdir -p ~/WaveStack && cd ~/WaveStack
# copy wavestack.py, requirements.txt, run.sh, and WaveStack.desktop here

sudo apt update && sudo apt install -y python3-venv python3-tk vlc
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
chmod +x wavestack.py run.sh

mkdir -p ~/.local/share/applications
cp WaveStack.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
```

WaveStack should now appear in your app launcher — search for
"WaveStack". See **SETUP.md** for the same steps with an explanation of
what each one does and how to undo it.

## Usage

### Playback

| Control         | What it does                                          |
|------------------|--------------------------------------------------------|
| **◄◄ Prev**      | Goes back to the previously played track               |
| **► Play**       | Starts the selected/first track, or resumes if paused  |
| **\|\| Pause**   | Pauses the current track                                |
| **■ Stop**       | Stops and resets the position to 0:00                  |
| **Next ►►**      | Plays the next queued track, or the next Library track  |

### Library

- Double-click a track to play it immediately
- Select one or more tracks (`Ctrl`/`Shift`-click) and hit
  **Enqueue >>** to add them to the queue
- Right-click a track for a quick context menu (Play / Add to Queue)

### Queue

- Shows what's coming up next, always checked before falling back to
  Library order when you hit Next or a track ends
- Double-click a queued track to jump to it right now
- **<< Remove** takes the selected track(s) out of the queue
- **Clear Queue** empties it entirely

### Keyboard shortcuts

- `Ctrl+O` — Open Folder
- `Ctrl+Q` — Quit

## Configuration

WaveStack loads `.mp3` files from a hardcoded default folder on
startup. Near the top of `wavestack.py`:

```python
DEFAULT_MUSIC_DIR = "/home/syed-ali-ahmed-shah/Music"
```

Change this if your username or music folder ever differs. If the
folder is missing or empty, WaveStack shows a dialog offering to let
you browse to the right one instead of crashing — that's intentional,
not a bug.

## Project Structure

| File               | Purpose                                              |
|--------------------|-------------------------------------------------------|
| `wavestack.py`     | The whole application: GUI, audio engine, playback logic |
| `requirements.txt` | Python dependencies (just `python-vlc`)               |
| `WaveStack.desktop`| App launcher entry for GNOME/KDE                       |
| `run.sh`           | Wrapper that launches `wavestack.py` with the right venv |
| `SETUP.md`         | Step-by-step install commands, explained               |
| `README.md`        | This file                                              |

## How It Works

- **GUI:** classic (non-`ttk`) Tkinter widgets. Unlike themed `ttk`
  widgets, these are rendered by Tk itself rather than your desktop's
  theme, so the beveled borders and chunky buttons look authentically
  retro everywhere, with no stylesheet fighting required.
- **Audio:** [libVLC](https://www.videolan.org/vlc/libvlc.html) via
  `python-vlc`. VLC's own audio pipeline handles PipeWire/PulseAudio
  compatibility, reliable seeking, and format quirks far more robustly
  than pure-Python audio libraries.
- **Threading:** VLC fires playback events (track ended, playback
  error) on its own internal thread. WaveStack never touches Tkinter
  widgets from that thread directly — events are placed on a
  thread-safe queue and drained by the main window's own periodic
  timer instead.
- **Window chrome:** the title bar and minimize/close buttons are
  drawn by your window manager, not WaveStack. Ubuntu 26.04 runs
  Wayland-only, where custom-positioned, undecorated windows are
  unreliable, so everything *inside* the window is retro-styled while
  the outer frame stays native for compatibility.

## Troubleshooting

**"WaveStack could not start because the VLC engine is missing or broken"**
Run `sudo apt install vlc` and try again.

**"WaveStack could not start because the 'python-vlc' package is not installed"**
Run `./venv/bin/pip install -r requirements.txt` from inside `~/WaveStack`.

**App doesn't appear in the launcher after installing**
Log out and back in, or re-run
`update-desktop-database ~/.local/share/applications/`. Double-check
that `Exec=` in `WaveStack.desktop` points to where `run.sh` actually
lives on your machine.

**No sound, but the app runs fine**
Check your system volume and default output device — WaveStack plays
through whatever PipeWire/PulseAudio device is already set as default.
The in-app volume slider only controls WaveStack's own output level.

**"No MP3 Files Found" dialog on startup**
Either your default Music folder doesn't exist or is empty, or
`DEFAULT_MUSIC_DIR` doesn't match your actual username — see
**Configuration** above. Click **Browse...** in the dialog to pick the
right folder without editing any code.

## Privacy

WaveStack makes zero network requests. No lyrics, no album art
downloads, no update checks, no analytics,everything it does happens
against your local filesystem and your local audio device.
