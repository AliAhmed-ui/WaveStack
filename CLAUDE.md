# WaveStack

A 1990s-styled, offline-first MP3 player for Linux desktops. Python 3 + classic Tkinter for the UI, libVLC (via `python-vlc`) for audio. The whole application is one file: `wavestack.py` (~2,700 lines).

## Commands

```bash
./run.sh                                   # normal launch; first run installs system deps with sudo, creates venv/, writes launchers
./venv/bin/python3 wavestack.py            # launch directly, skipping setup (use this while developing)
./venv/bin/python3 -m py_compile wavestack.py   # syntax check; the only automated check available
./venv/bin/pip install -r requirements.txt # reinstall Python deps
```

- There is no test suite, linter config, or build step. Verify changes by launching the app; it needs a display (X11/Wayland) and libVLC installed.
- `run.sh` is gated by `.wavestack-setup-complete` plus an executable `venv/bin/python3`. Delete the marker to force a full re-setup. It rewrites `~/.local/share/applications/WaveStack.desktop` and the Desktop shortcut on **every** launch.
- `test_layout.py` is an untracked scratch file whose `__main__` block is `pass`; it runs nothing.

## Files

| File | Role |
| --- | --- |
| `wavestack.py` | Entire app: config, helpers, widgets, audio engine, main window, entry point |
| `run.sh` | Cross-distro bootstrap (apt/dnf/pacman/zypper) + launcher; `exec`s the venv Python |
| `requirements.txt` | `python-vlc`, `Pillow`, `mutagen`, `requests`, `numpy` |
| `WaveStack.desktop` | Checked-in launcher with a hardcoded absolute path; `run.sh` generates the real one |
| `README.md`, `SETUP.md` | User docs (partly stale, see Known gaps) |

Runtime state lives outside the repo at `~/.config/wavestack/state.json` (volume, last song, position, theme).

## Layout of `wavestack.py`

Top to bottom, separated by `# ----` banner comments:

1. **Imports** — `vlc`, `PIL`, `mutagen`, `numpy` are wrapped in `try/except ImportError` (`vlc = None`, `PIL_AVAILABLE`, `MUTAGEN_AVAILABLE`, `NUMPY_AVAILABLE`). `requests` is imported unconditionally, so it is a hard dependency.
2. **Configuration** — `DEFAULT_MUSIC_DIR` (hardcoded to `/home/syed-ali-ahmed-shah/Music`), `STATE_FILE`, `AUTOSAVE_INTERVAL_SECONDS`, the `THEMES` dict (`light` / `dark`), fixed vinyl and CRT palettes, `MODE_*` playback constants.
3. **Helpers** — `get_retro_font`, `format_time`, `scan_mp3_files` (non-recursive, `.mp3` only, never raises), `extract_album_art_bytes`, `parse_lrc`, then the **offline genre classification** functions (`normalize_genre`, `propagate_genres_by_tokens`, `decode_pcm_slice`, `extract_dsp_features`, `classify_dsp_features`, `load/save_genre_cache`, `run_autosort_worker`). These have no Tk access.
4. **`RetroDialog(tk.Toplevel)`** — modal replacement for `tkinter.messagebox`; blocks in `__init__` via `wait_window` and exposes `.result`.
5. **`NowPlayingDisplay`** — green LCD marquee; scrolls 2px per call to `tick()`.
6. **`VinylDisc`** — PIL-rendered rotating record with embedded ID3 art; own 50 ms `after` loop.
7. **`Visualizer`** — simulated LED spectrum analyzer; own 60 ms `after` loop.
8. **`CrtLyricsDisplay`** — read-only `tk.Text` showing `.lrc` lines with an `active` tag.
9. **`AudioEngine`** — the only code that touches libVLC.
10. **`WaveStackApp(tk.Tk)`** — main window and all playback/queue/search/sync/persistence logic.
11. **`main()`** — re-execs into `venv/bin/python3` if `vlc` failed to import, probes `vlc.Instance()`, then runs the app. Failures go through `_fatal_startup_error` (stderr + retro dialog).

## How the audio/UI core works

**Audio.** `AudioEngine` owns one `vlc.Instance` and one media player. `MediaPlayerEndReached` and `MediaPlayerEncounteredError` fire on a libVLC thread; the callbacks only push `"ended"` / `"error"` onto a `queue.Queue`. Nothing else in the app calls VLC directly.

**The main tick.** `WaveStackApp._tick` reschedules itself every 250 ms and is the heartbeat for everything:
- drains `audio.poll_events()` → `_handle_track_ended` / `_handle_playback_error`, both of which call `_advance`
- updates seek bar, elapsed/total labels (skipped while `_user_seeking`)
- finishes a pending resume-from-saved-position (`_complete_pending_resume`)
- autosaves state every 5 s
- re-syncs vinyl + visualizer state, pushes the position to the lyrics display, advances the marquee

**Playback state** is three flags on the app: `now_playing` (path or `None`), `is_paused`, `is_stopped`. Playing = `now_playing and not is_paused and not is_stopped`. `_sync_vinyl_spin_state` (alias `_sync_playback_visuals`) maps that onto the vinyl and visualizer; call it after any state change.

**Track flow.** Every track start goes through `_play_path(path, resume_position=None)`: it checks the file exists, pushes the previous track onto `history`, loads and plays, resets the seek bar, updates marquee/highlight, and calls `vinyl.load_track` and `lyrics_display.load_track` once per track. `_advance(auto, force_next)` picks what comes next: Repeat One replays on natural end; otherwise the queue is consumed first (random pick in Shuffle), then `_next_shuffle_track` (a no-repeat pool) or `_next_in_library` (wraps only in Repeat All). Prev pops `history`.

**Seeking.** `<ButtonPress-1>` on the scale sets `_user_seeking`, which stops `_tick` from fighting the drag; release calls `audio.seek_seconds`. The scale's `to=` is set to the track length once VLC reports it.

**Lyrics.** `CrtLyricsDisplay.load_track` looks for a sibling `.lrc` (same basename as the MP3) and parses it with `parse_lrc`. `sync_to_ms` binary-searches for the active line and only touches the `Text` widget when the index changes.

**Online sync.** `toggle_sync` starts a daemon thread running `run_lyrics_sync_worker` (module-level, no Tk access), which finds MP3s without an `.lrc` and looks each up on LRCLIB via `fetch_synced_lyrics`: exact `/api/get`, then `/api/search` (with and without the `feat.` suffix), then a free-text search on the cleaned filename. `describe_track_for_lyrics` builds the query from ID3 tags plus filename, and `clean_track_title` strips download-site junk generically (anything domain-shaped, `| Site.com`, `(Official Video)`, `320kbps`, `No DJ`). Search results must match title, artist and duration (within 5 s). Requests retry on timeouts/5xx; one song failing is skipped, three consecutive network failures switch back to Offline. The worker reports through `_sync_events`, drained by `_tick`; a download for the playing track reloads the lyrics terminal. Each run gets a fresh `cancel_sync` Event. This is the only network access in the app and is off by default.

**Library tree.** The Library `Listbox` renders a collapsible genre tree, so a row index is never an index into `library_files`. `refresh_library_listbox` rebuilds the rows and `visual_row_map` (`{row: ("folder", genre_key) | ("track", path)}`; the root row is `("folder", TREE_ROOT_KEY)`), preserving selection and scroll by identity. Always go through `_selected_track_paths()`, `_row_for_entry()` or the map. `library_files` remains the flat playable list that Next/Prev/Shuffle/Repeat use. Genres live in `genre_cache` (`{mp3 filename: {"genre", "source"}}`); tracks missing from it show under "Unsorted / Untagged". Typing in the search entry sets `_tree_filter`, which filters the tree and forces matching folders open without touching `genre_expanded`.

**AUTO-SORT.** `on_autosort_clicked` starts `run_autosort_worker` on a daemon thread (clicking again cancels). The worker runs three stages on tracks not in the cache: ID3 `TCON` tag → artist/filename token propagation from known tracks → ffmpeg + numpy spectral heuristic (`_DSP_PROTOTYPES`, hand-set and only calibrated against hip-hop). It reports via a `queue.Queue` that `_poll_autosort` drains every 150 ms on the main thread. The cache is written to `<music dir>/.wavestack_genres.json`, falling back to `~/.config/wavestack/genres/`. The DSP stage needs the `ffmpeg` binary, which `run.sh` does not install.

**Layout.** `_build_widgets` packs, top to bottom, inside `outer_frame`: header, LCD marquee, transport row, seek row, volume row, then the status bar (`side=BOTTOM`), then `panes`. `panes` is a 3-column grid: library (search entry + tree listbox), middle column (AUTO-SORT/Enqueue/Remove/Clear + vinyl), right column (queue over lyrics, 50/50). `toggle_visualizer` swaps `panes` and `visualizer` with `pack_forget`/`pack`.

## Conventions

- **Classic `tk` widgets only, never `ttk`.** The retro bevels depend on Tk drawing the widgets itself. Use `relief=RAISED/SUNKEN` with `bd=2`/`3` and fonts from `get_retro_font`.
- **Theming is manual.** Every themed widget is stored as `self.<name>` and reconfigured explicitly in `WaveStackApp.apply_theme`. A new widget needs colours from `self.theme` at creation **and** an entry in `apply_theme`, or it will not follow the light/dark toggle. Add new colour keys to both `THEMES["light"]` and `THEMES["dark"]`.
- **"Hardware" displays are not themed.** The LCD marquee, visualizer canvas, and CRT lyrics screen keep fixed colours by design; only their surrounding frame follows the theme.
- **No Tk calls off the main thread.** VLC callbacks, the AUTO-SORT worker and the lyrics-sync worker all go through queues drained on the main thread.
- **Fail soft.** Helpers return empty/`None` instead of raising; persistence swallows all exceptions; optional deps degrade the feature (no Pillow → static disc). User-facing errors use `show_retro_error` / `RetroDialog`, not `messagebox` or tracebacks. `report_callback_exception` is overridden to show a dialog.
- **Animations** keep an `_after_id`, and expose `start_*` / `pause_*` / `stop_and_reset` / `shutdown`. `on_close` must cancel every pending `after` job before `destroy()`.
- **Keep a reference to every `PhotoImage`** (`self._current_photo`) or Tk will blank it.
- Handlers are `on_*` for user actions and `_on_*` / `_private` for internals; key handlers that consume the event return `"break"`. Space is bound at class level for `Button` and `Listbox` to override their defaults, and is ignored when an `Entry` has focus.
- Section banners and long "why" comments are the house style; match them when adding code.
- Wayland is the target session: don't use undecorated or manually positioned top-level windows.

## Known gaps

Things in the current code that are easy to trip over:

- `status_var` is set all over the app ("Playing: …", "Stopped", etc.) but no widget displays it. The status bar label is bound to `lrc_status_var`, which AUTO-SORT progress also writes to.
- `sync_btn` is not in `apply_theme`, so it keeps its old colours after a theme toggle. Its initial label is `[ Go Online ]`, while toggling produces `[ Online Sync ]` / `[ Offline ]`.
- README advertises click-to-seek on lyric lines; `CrtLyricsDisplay` has no click binding.
- The module docstring and About dialog still say "zero network access", predating LRCLIB sync.
- README lists only `Ctrl+O` and `Ctrl+Q`; the code also binds `Space`, `Ctrl+F`, `F10`, and `Esc`.
- `SETUP.md` describes the old apt-only manual install under `~/WaveStack`; `run.sh` now does this automatically for four distro families.
