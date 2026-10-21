# WaveStack

A fully offline-first, 1990s-styled MP3 player for Linux desktops, built with Python, Tkinter, and a native C++ audio engine (miniaudio), with libVLC as a fallback.

No telemetry, no analytics, and no mandatory cloud connections. Just your local MP3 collection styled like it's 1996, featuring an opt-in hardware-style toggle to fetch synchronized lyrics only when you explicitly allow it.

## Screenshot

## Features

* **Strict retro aesthetic** thick raised/sunken borders, classic gray (`#C0C0C0`) chrome, monospace fonts, and a scrolling green LCD "now playing" marquee, all built from classic Tkinter widgets (not a themed skin, so it doesn't inherit your desktop's modern GTK/Qt look).
* **Full playback controls** Play, Pause, Stop, Next, Previous, and Mode Cycling (Normal, Shuffle, Repeat 1, Repeat All).
* **Library + Queue** browse your music folder, multi-select tracks to enqueue, remove from the queue, right-click context menus.
* **Collapsible Hierarchical Genre Tree + AUTO-SORT** the Library is an authentic retro directory explorer built purely inside a classic `tk.Listbox` (avoiding themed `ttk` widgets). **AUTO-SORT** classifies tracks fully offline in the background: ID3 `TCON` genre tags first, then artist/filename matching against already-known tracks, then a spectral-analysis heuristic evaluating sub-bass energy, spectral brightness, and onset rhythms (needs `numpy`; audio is decoded by the native engine, with `ffmpeg` as a fallback). Results are cached in `.wavestack_genres.json` inside your music folder.
* **Synchronized CRT Lyrics Terminal** Displays timestamped `.lrc` lyrics in a monochrome CRT terminal format with active line tracking and click-to-seek support.
* **Opt-In Online Sync** A dedicated toggle switch defaults to `[ Offline ]`. When switched to `[ Online Sync ]`, it queries the LRCLIB API to download missing `.lrc` files for your local tracks.
* **Spinning Vinyl Animation & Visualizer** Extracts ID3 album art directly from your local audio files for a rotating vinyl disc, plus an in-app retro LED equalizer. With the native engine the bars are **real**: the FFT, band levels and smoothing are computed in C++ from the audio you are actually hearing (libVLC fallback shows a simulated animation).
* **Working seek bar** drag to scrub through a track, backed by the in-memory decoded track, so seeking is instant and exact, including on variable-bitrate MP3s.
* **Native C++ audio engine** `AudioEngine.cpp` uses [miniaudio](https://miniaud.io) for MP3/WAV/FLAC decoding and playback on its own audio thread, exposed to Python through pybind11 as the `wavestack_audio` module. It gives sample-accurate playback position (used to keep the lyrics in sync), zero-copy NumPy access to the decoded audio, the real-time spectrum for the visualizer, and a device-free decoder for AUTO-SORT. If the module can't be built or no audio device opens, WaveStack falls back to libVLC automatically.
* **Headphone and media keys** WaveStack registers as an MPRIS2 player over D-Bus, so Play/Pause/Next/Previous from Bluetooth headphones, keyboards and the desktop media widget control it even when another window has focus.
* **Won't crash on bad input** missing/empty folders and corrupted files are handled with retro-styled dialogs, not stack traces.

## Requirements

WaveStack supports modern Linux desktops, including:

* **Debian-based:** Ubuntu, Debian, Linux Mint, Pop!_OS, Zorin OS, elementary OS
* **Fedora/RHEL-based:** Fedora, RHEL, Rocky Linux, AlmaLinux
* **Arch-based:** Arch Linux, Manjaro, EndeavourOS, Garuda Linux
* **openSUSE**

For the first launch, you need:

* An internet connection
* `sudo` access to install VLC, Python, Tkinter, a C++ compiler, Python headers, and Python virtual-environment support
* The complete WaveStack project folder containing `wavestack.py`, `requirements.txt`, and `run.sh`
* Optional: `ffmpeg` on your PATH, used for AUTO-SORT only if the native engine could not be built

## Quick Start

Open a terminal inside the WaveStack project folder, make the launcher executable, and run it:

```bash
chmod +x run.sh
./run.sh

```

On its first run, WaveStack automatically:

1. Detects your Linux distribution and package manager.
2. Installs VLC, Python, Tkinter, a C++ compiler (`g++`), Python headers, and virtual-environment support.
3. Creates WaveStack's private Python environment and installs its dependencies.
4. Adds WaveStack to your app launcher and places a clickable desktop icon in your Desktop folder.
5. Builds the native audio engine (`wavestack_audio`). This repeats automatically whenever `AudioEngine.cpp`, `miniaudio.h` or `setup.py` change, and is skipped otherwise. If the build fails, WaveStack prints the compiler error and runs on libVLC.
6. Starts the player.

After setup, start WaveStack by clicking its desktop icon or searching for **WaveStack** in your system app launcher. No commands are needed after each boot.

To build the engine by hand: `./venv/bin/pip install pybind11 setuptools && ./venv/bin/python3 setup.py build_ext --inplace`. To check which engine is active: `./venv/bin/python3 -c "import wavestack; print(type(wavestack.create_audio_engine()).__name__)"` prints `NativeAudioEngine` or `AudioEngine` (libVLC).

## Usage

### Playback

| Control | What it does |
| --- | --- |
| **◄◄ Prev** | Goes back to the previously played track |
| **► Play** | Starts the selected/first track, or resumes if paused |
| **|| Pause** | Pauses the current track |
| **■ Stop** | Stops and resets the position to 0:00 |
| **Next ►►** | Plays the next queued track, or the next Library track |
| **Mode Toggle** | Cycles through Normal, Shuffle, Repeat 1, Repeat All |
| **Visualizer** | Switches to the equalizer visualizer (Escape to exit) |

### Synchronized Lyrics and Online Mode

* Keep the bottom right button set to `[ Offline ]` to block all network traffic. WaveStack will only read existing `.lrc` files stored locally alongside your MP3s.
* Click `[ Offline ]` to switch to `[ Online Sync ]`. The player will read missing lyric tags in your loaded music directory, securely query LRCLIB in the background, and save `.lrc` files locally.
* **Click-to-Seek**: Click any lyric line on the CRT terminal to jump directly to that timestamp in the audio track.

### Library Tree & Auto-Sorting

* **Tree Navigation**: Click a `[-]` / `[+]` folder header to collapse or expand it; click **All Music (Library)** to collapse the whole collection tree.
* **AUTO-SORT**: Click the **AUTO-SORT** button in the middle control column to organize unsorted tracks into genre folders via an isolated background daemon thread (click again to stop).
* **Smart Filtering**: Type in the `search song` box to dynamically filter matching tracks and auto-expand their parent genres; `Up`/`Down` move between matches, `Enter` picks one, and `Esc` clears the search.
* Double-click an actual track to play it immediately.
* Select one or more tracks (`Ctrl`/`Shift`-click) and hit **Enqueue >>** to add them to the queue.
* Right-click a track for a quick context menu (Play / Add to Queue).
* **<< Remove** takes the selected track(s) out of the queue.
* **Clear Queue** empties it entirely.

### Keyboard shortcuts

* `Space` = Play / Pause
* `Ctrl+O` = Open Folder
* `Ctrl+F` = Focus search
* `F10` = Visualizer
* `Esc` = Leave visualizer / clear search
* `Ctrl+Q` = Quit
* Media keys / headphone buttons = Play, Pause, Next, Previous (via MPRIS)

## Configuration

WaveStack loads `.mp3` files from a hardcoded default folder on startup. Near the top of `wavestack.py`:

```python
DEFAULT_MUSIC_DIR = "/home/syed-ali-ahmed-shah/Music"

```

Change this if your username or music folder ever differs. If the folder is missing or empty, WaveStack shows a dialog offering to let you browse to the right one instead of crashing — that's intentional, not a bug.

## Project Structure

| File | Purpose |
| --- | --- |
| `wavestack.py` | The whole application: GUI, playback logic, engine selection, MPRIS media keys |
| `AudioEngine.cpp` | Native C++ engine: miniaudio playback, position clock, NumPy buffer access, real-time spectrum, device-free decoder |
| `miniaudio.h` | Vendored single-header miniaudio 0.11.21 |
| `setup.py` | Builds `AudioEngine.cpp` into the `wavestack_audio` Python module |
| `requirements.txt` | Python dependencies (`python-vlc`, `requests`, `mutagen`, `Pillow`, `numpy`, `dbus-next`, `pybind11`, `setuptools`) |
| `WaveStack.desktop` | App launcher entry for GNOME/KDE |
| `run.sh` | Setup, native-engine build, and launcher for `wavestack.py` with the right venv |
| `SETUP.md` | Step-by-step install commands, explained |
| `README.md` | This file |

## How It Works

* **GUI:** classic (non-`ttk`) Tkinter widgets. Unlike themed `ttk` widgets, these are rendered by Tk itself rather than your desktop's theme, so the beveled borders and chunky buttons look authentically retro everywhere, with no stylesheet fighting required.
* **Audio:** a C++ engine built on [miniaudio](https://miniaud.io), bound with pybind11. A whole track is decoded to float32 stereo at 44.1 kHz in memory; miniaudio's audio thread streams it to the device without locks or allocation in the callback. The playback position is an atomic frame counter minus the device's output latency, so what Python polls matches what you hear. [libVLC](https://www.videolan.org/vlc/libvlc.html) via `python-vlc` remains as an automatic fallback.
* **Visualizer maths:** `get_spectrum()` in C++ takes a 2048-sample Hann-windowed slice at the audible position, runs a radix-2 FFT, groups bins into 28 log-spaced bands, converts to dB with a treble tilt, and applies attack/decay smoothing and the volume level. Python only draws the result.
* **Media keys:** an MPRIS2 service on a background D-Bus thread queues commands; the Tk main thread drains them, so no Tk or audio call happens off-thread.
* **Threading:** audio-engine events never touch Tkinter. The native engine just sets a finished flag that the main window polls; libVLC (fallback) fires events on its own thread. WaveStack never touches Tkinter widgets from that thread directly — those are placed on a thread-safe queue and drained by the main window's own periodic timer instead. Network requests for lyrics are isolated to their own background thread.
* **Offline Classification & Threading:** When `AUTO-SORT` is triggered, audio slicing and spectral feature computations run strictly in an isolated background daemon thread. Live progress updates are safely pushed across a thread-safe FIFO queue to the main Tkinter event loop, ensuring zero stutter in the spinning vinyl animation or active audio playback.
* **Window chrome:** the title bar and minimize/close buttons are drawn by your window manager, not WaveStack. Ubuntu 26.04 runs Wayland-only, where custom-positioned, undecorated windows are unreliable, so everything *inside* the window is retro-styled while the outer frame stays native for compatibility.

## Troubleshooting

**"WaveStack could not start because the VLC engine is missing or broken"**
Run `sudo apt install vlc` and try again.

**"WaveStack could not start because the 'python-vlc' package is not installed"**
Run `./venv/bin/pip install -r requirements.txt` from inside `~/WaveStack`.

**App doesn't appear in the launcher after installing**
Log out and back in, or re-run `update-desktop-database ~/.local/share/applications/`. Double-check that `Exec=` in `WaveStack.desktop` points to where `run.sh` actually lives on your machine.

**Headphone / media buttons do nothing**
Bluetooth buttons are forwarded by the `mpris-proxy` service, which can keep pointing at a player that has since closed. Restart it and try again: `systemctl --user restart mpris-proxy`.

**"Native audio engine build failed" in the terminal**
Install a C++ compiler and Python headers (`build-essential python3-dev` on Debian/Ubuntu). WaveStack keeps working on libVLC in the meantime.

**No sound, but the app runs fine**
Check your system volume and default output device — WaveStack plays through whatever PipeWire/PulseAudio device is already set as default. The in-app volume slider only controls WaveStack's own output level.

## Privacy

By default, WaveStack runs **100% offline** and makes zero network requests. No analytics, no telemetry, and no background updates.

Network connections only occur if you manually toggle the bottom status button to **[ Online Sync ]**. When enabled, WaveStack queries the public LRCLIB API strictly to download missing `.lrc` synchronized lyrics for your local tracks. You can turn this off at any time to return to a fully isolated offline environment.