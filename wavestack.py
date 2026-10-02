#!/usr/bin/env python3
"""
WaveStack - a fully offline, 1990s-styled MP3 player for Linux desktops.

Design notes
------------
* Zero network access: no lyrics, no album art scraping, no telemetry.
* The chunky, high-contrast, Windows-95-era look is built entirely with
  classic (non-"ttk") Tkinter widgets. Unlike ttk widgets, these are
  drawn by Tk itself rather than the desktop theme, so they naturally
  render with beveled borders on every desktop environment with no
  theming hacks required.
* Playback is handled by libVLC (via python-vlc), which is far more
  robust against odd file encodings, VBR MP3s, and PipeWire/PulseAudio
  quirks than pure-Python audio libraries, and gives rock-solid seeking.
* The spinning vinyl widget's album art comes only from each MP3's own
  embedded ID3 tag (read locally with mutagen) -- never fetched from
  the web, consistent with the zero-network-access design above.

A note on window chrome: the outer title bar, minimize/close buttons,
etc. are left to your desktop's own window manager rather than
hand-painted, because Ubuntu 26.04 runs a Wayland-only session where
undecorated, manually-positioned windows (the trick some retro-skinned
apps use to fake an OS title bar) are unreliable. Everything *inside*
the window is fully retro-styled.
"""

import os
import sys
import json
import re
import time
import queue
import traceback
import threading
import hashlib
import shutil
import subprocess
import collections
import urllib.parse
import requests
import random
from io import BytesIO
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog

try:
    import vlc
except ImportError:
    vlc = None

try:
    from PIL import Image, ImageDraw, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from mutagen.id3 import ID3
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    np = None
    NUMPY_AVAILABLE = False



# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Change this if your username or music folder differs.
DEFAULT_MUSIC_DIR = "/home/syed-ali-ahmed-shah/Music"

APP_TITLE = "WaveStack"
APP_VERSION = "1.0"

# Where WaveStack remembers the last song, its position, and the volume
# level, so it can resume exactly where you left off next time. Follows
# the usual ~/.config convention so it's easy to find or delete by hand.
STATE_FILE = os.path.expanduser("~/.config/wavestack/state.json")

# How often (seconds) to write the current song/position to disk while
# playing, as a safety net against the app being force-killed instead
# of closed normally. A graceful close always saves immediately.
AUTOSAVE_INTERVAL_SECONDS = 5

# --------------------------------------------------------------------------
# Genre tree / AUTO-SORT
# --------------------------------------------------------------------------

# The six "core" genres the offline DSP classifier can assign, in the
# order their folders appear in the Library tree. A genre read from an
# ID3 tag that doesn't normalise to one of these (e.g. "Latin") keeps
# its own folder, listed alphabetically after the core ones.
GENRE_HIPHOP = "Hip-Hop"
GENRE_ELECTRONIC = "Electronic"
GENRE_ROCK = "Rock"
GENRE_POP = "Pop"
GENRE_RNB = "R&B / Chill"
GENRE_ACOUSTIC = "Acoustic / Instrumental"
GENRE_UNSORTED = "Unsorted / Untagged"

GENRE_ORDER = [GENRE_HIPHOP, GENRE_ELECTRONIC, GENRE_ROCK, GENRE_POP,
               GENRE_RNB, GENRE_ACOUSTIC]

# Key used in visual_row_map for the "[-] All Music (Library)" root row.
# Deliberately not a string any real genre tag could normalise to.
TREE_ROOT_KEY = "\x00root"
TREE_ROOT_LABEL = "All Music (Library)"

# Classified genres are cached here, inside the music folder itself, so
# a re-scan is avoided across sessions (and the cache travels with the
# folder). If that folder isn't writable, the cache falls back to
# GENRE_CACHE_FALLBACK_DIR instead.
GENRE_CACHE_NAME = ".wavestack_genres.json"
GENRE_CACHE_FALLBACK_DIR = os.path.expanduser("~/.config/wavestack/genres")

# The DSP stage decodes this many seconds of mono PCM, starting this far
# into the track (to skip quiet intros), at this sample rate.
DSP_SAMPLE_RATE = 22050
DSP_SLICE_SECONDS = 10
DSP_SLICE_OFFSET_SECONDS = 30

# How often (ms) the main thread drains the AUTO-SORT worker's queue.
AUTOSORT_POLL_MS = 150

# --------------------------------------------------------------------------
# Retro color palettes (Light = Classic Windows 95 / Motif; Dark = Retro Noir)
# --------------------------------------------------------------------------

THEMES = {
    "light": {
        "name": "light",
        "bg": "#C0C0C0",              # standard "button face" gray
        "bg_light": "#E0E0E0",        # lighter gray, used for hover/active states
        "fg": "#000000",              # standard black text
        "title_fg": "#000080",        # classic navy blue
        "border_black": "#000000",
        "select_bg": "#000080",
        "select_fg": "#FFFFFF",
        "lcd_bg": "#0A1F0A",          # dark green-black LCD panel background
        "lcd_fg": "#39FF14",          # bright LCD green
        "entry_bg": "#FFFFFF",        # white input/listbox backgrounds
        "entry_fg": "#000000",
        "entry_insert": "#000000",
        "search_placeholder_fg": "#777777",
        "search_normal_fg": "#000000",
        "scale_trough": "#FFFFFF",
        "btn_bg": "#C0C0C0",
        "btn_fg": "#000000",
        "btn_active_bg": "#E0E0E0",
        "btn_active_fg": "#000000",
        "menu_bg": "#C0C0C0",
        "menu_fg": "#000000",
        "menu_active_bg": "#000080",
        "menu_active_fg": "#FFFFFF",
    },
    "dark": {
        "name": "dark",
        "bg": "#242424",              # chunky dark chassis gray (preserves Tk 3D bevels)
        "bg_light": "#383838",        # lighter dark gray for active states
        "fg": "#E0E0E0",              # soft light silver text
        "title_fg": "#00E5FF",        # electric retro cyan
        "border_black": "#0F0F0F",
        "select_bg": "#005599",        # deep navy blue selection
        "select_fg": "#FFFFFF",
        "lcd_bg": "#0A1F0A",          # authentic green phosphor LCD remains iconic
        "lcd_fg": "#39FF14",
        "entry_bg": "#141414",        # sunken dark well for lists & inputs
        "entry_fg": "#E0E0E0",
        "entry_insert": "#FFFFFF",
        "search_placeholder_fg": "#888888",
        "search_normal_fg": "#E0E0E0",
        "scale_trough": "#141414",    # sunken dark slider track
        "btn_bg": "#303030",          # 3D raised dark buttons
        "btn_fg": "#E0E0E0",
        "btn_active_bg": "#444444",
        "btn_active_fg": "#FFFFFF",
        "menu_bg": "#282828",
        "menu_fg": "#E0E0E0",
        "menu_active_bg": "#005599",
        "menu_active_fg": "#FFFFFF",
    }
}

DEFAULT_THEME = "light"

# Backward-compatibility alias constants (referencing default light theme)
BG = THEMES["light"]["bg"]
BG_LIGHT = THEMES["light"]["bg_light"]
BORDER_BLACK = THEMES["light"]["border_black"]
TITLE_BLUE = THEMES["light"]["title_fg"]
SELECT_BG = THEMES["light"]["select_bg"]
SELECT_FG = THEMES["light"]["select_fg"]
LCD_BG = THEMES["light"]["lcd_bg"]
LCD_FG = THEMES["light"]["lcd_fg"]

# Vinyl disc palette -- deliberately more "real object" than "UI
# chrome": a physical record is black vinyl with a colored paper
# label, not another gray Windows-95 panel.
VINYL_DISC_COLOR = (18, 18, 18, 255)
VINYL_LABEL_COLOR = (122, 28, 28, 255)      # classic deep vinyl-label red
VINYL_GROOVE_LIGHT = (46, 46, 46, 255)
VINYL_GROOVE_DARK = (26, 26, 26, 255)
VINYL_RIM_HIGHLIGHT = (55, 55, 55, 255)
VINYL_HOLE_COLOR = (0, 0, 0, 255)

# CRT Lyrics Terminal palette -- always phosphor-green-on-black regardless
# of the chassis theme: a real CRT screen doesn't change colour just
# because the case around it is light gray or charcoal.
CRT_BG          = "#051505"   # deep near-black phosphor screen
CRT_FG_ACTIVE   = "#00FF66"   # bright phosphor green -- the current lyric line
CRT_FG_DIM      = "#1A6633"   # dim/inactive lines
CRT_ACTIVE_BG   = "#00FF66"   # reverse-video highlight background
CRT_ACTIVE_FG   = "#000000"   # reverse-video highlight foreground (black)
CRT_BANNER_FG   = "#00AA44"   # slightly dimmer than active, for the title banner

# --------------------------------------------------------------------------
# Playback modes
# --------------------------------------------------------------------------

MODE_NORMAL = 0
MODE_SHUFFLE = 1
MODE_REPEAT_ONE = 2
MODE_REPEAT_ALL = 3

MODE_LABELS = {
    MODE_NORMAL: "[ Normal ]",
    MODE_SHUFFLE: "[ Shuffle ]",
    MODE_REPEAT_ONE: "[ Repeat 1 ]",
    MODE_REPEAT_ALL: "[ Repeat All ]",
}

MODE_NAMES = {
    MODE_NORMAL: "Normal",
    MODE_SHUFFLE: "Shuffle",
    MODE_REPEAT_ONE: "Repeat One",
    MODE_REPEAT_ALL: "Repeat All",
}

# Lower-pane layout: how many tracks the Queue list shows without
# scrolling, and the least height (px) the lyrics terminal below it is
# squeezed to in a small window.
QUEUE_VISIBLE_ROWS = 7
LYRICS_MIN_HEIGHT = 80

SEARCH_PLACEHOLDER_TEXT = "search song"
SEARCH_PLACEHOLDER_FG = THEMES["light"]["search_placeholder_fg"]
SEARCH_NORMAL_FG = THEMES["light"]["search_normal_fg"]

# Keys that change the search entry's cursor position or invoke a
# specific search action, but don't change its text -- re-filtering
# the Library tree on these would be wasted work, and for Down/Up it
# would actively fight the match navigation below.
_SEARCH_NON_TEXT_KEYSYMS = {
    "Down", "Up", "Return", "KP_Enter", "Escape", "Tab",
    "Left", "Right", "Home", "End",
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
}


# --------------------------------------------------------------------------
# Small, dependency-free helpers
# --------------------------------------------------------------------------

_FONT_PREFERENCE = [
    "Fixedsys",
    "MS Sans Serif",
    "Perfect DOS VGA 437",
    "Terminal",
    "DejaVu Sans Mono",
    "Liberation Mono",
    "Courier New",
    "Courier",
]


def get_retro_font(widget, size=9, bold=False):
    """Pick the most authentic-looking monospace font actually installed
    on this system, falling back to Tk's built-in fixed font so the UI
    never breaks just because a particular font pack isn't installed."""
    try:
        available = set(tkfont.families(widget))
    except tk.TclError:
        available = set()
    weight = "bold" if bold else "normal"
    for name in _FONT_PREFERENCE:
        if name in available:
            return (name, size, weight)
    return ("TkFixedFont", size, weight)


def format_time(seconds):
    """Format a duration in seconds as MM:SS."""
    seconds = int(max(0, seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def scan_mp3_files(directory):
    """Return a sorted list of absolute .mp3 paths in *directory*.

    Never raises -- returns an empty list if the directory does not
    exist, isn't readable, or has no .mp3 files in it."""
    if not directory:
        return []
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    files = []
    for name in entries:
        if name.lower().endswith(".mp3"):
            full_path = os.path.join(directory, name)
            if os.path.isfile(full_path):
                files.append(full_path)
    files.sort(key=lambda p: os.path.basename(p).lower())
    return files


def extract_album_art_bytes(filepath):
    """Return the raw bytes of the first embedded ID3 cover-art frame
    in *filepath*, or None if there isn't one, mutagen isn't
    installed, or the file's tags can't be read for any reason. Reads
    only the file's own local ID3 tag -- never touches the network."""
    if not MUTAGEN_AVAILABLE:
        return None
    try:
        tags = ID3(filepath)
    except Exception:
        return None  # no ID3 header, corrupt tag, etc. -- just means no art
    for key in tags.keys():
        if key.startswith("APIC"):
            return tags[key].data
    return None


def _crop_to_square(img):
    """Center-crop an image to a square, so resizing it afterward
    doesn't distort non-square album art."""
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _make_circular_mask(size, supersample=4):
    """A smooth, anti-aliased circular alpha mask, size x size.
    Drawing at supersample x the target size and downsampling avoids
    the jagged edge a single ellipse() at small sizes would have."""
    big = size * supersample
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    return mask.resize((size, size), Image.LANCZOS)



# LRC timestamp pattern: [mm:ss.xx] or [mm:ss] -- metadata tags like
# [ti:...] / [ar:...] / [al:...] share the same bracket syntax but have
# a non-numeric character immediately after '[', so the digit-anchored
# pattern below skips them naturally.
_LRC_TIMESTAMP_RE = re.compile(
    r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]"
)


def parse_lrc(filepath):
    """Parse an LRC file and return [(timestamp_ms, lyric_text), ...].

    Supports both ``[mm:ss.xx]`` and ``[mm:ss]`` timestamp formats.
    Multiple timestamps on the same line (common in LRC chorus repeats)
    each produce their own entry sharing the same lyric text.
    Returns an empty list if the file does not exist, contains no valid
    timestamp lines, or raises any OS/encoding error.
    """
    if not filepath or not os.path.isfile(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except UnicodeDecodeError:
        try:
            with open(filepath, "r", encoding="latin-1") as fh:
                raw = fh.read()
        except OSError:
            return []
    except OSError:
        return []

    entries = []
    for line in raw.splitlines():
        # Strip all leading timestamp tags, collect their ms values
        timestamps = []
        rest = line
        while True:
            m = _LRC_TIMESTAMP_RE.match(rest)
            if not m:
                break
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            centis  = m.group(3) or "0"
            # centis may be 1-3 digits; normalise to milliseconds
            millis  = int(centis.ljust(3, "0")[:3])
            timestamps.append(minutes * 60_000 + seconds * 1_000 + millis)
            rest = rest[m.end():]
        if not timestamps:
            continue  # metadata line or blank
        lyric = rest.strip()
        for ms in timestamps:
            entries.append((ms, lyric))

    entries.sort(key=lambda x: x[0])
    return entries


# --------------------------------------------------------------------------
# Offline genre classification (AUTO-SORT)
#
# Three stages, cheapest first, all local -- nothing here touches the
# network:
#   1. Metadata:   the file's own ID3 TCON genre tag, normalised.
#   2. Tokens:     artist / filename tokens cross-referenced against
#                  tracks whose genre is already known.
#   3. DSP:        a 10s PCM slice decoded by ffmpeg and analysed with
#                  numpy (sub-bass ratio, spectral centroid, zero-crossing
#                  rate, onset pulse) -- a heuristic, not a trained model.
#
# Everything in this section is plain functions with no Tkinter access,
# so it is safe to run on the AUTO-SORT worker thread.
# --------------------------------------------------------------------------

# Checked top to bottom, first hit wins, so "pop rap" lands in Hip-Hop
# and "indie pop" in Pop rather than Rock. Keywords only match as whole
# words ("rap" must not match "therapy").
_GENRE_KEYWORDS = [
    (GENRE_HIPHOP, ("hip hop", "hip-hop", "hiphop", "rap", "trap", "drill",
                    "grime", "boom bap", "crunk")),
    (GENRE_RNB, ("r&b", "rnb", "r & b", "r'n'b", "rhythm and blues", "soul",
                 "neo soul", "chill", "chillout", "lo-fi", "lofi",
                 "downtempo", "ambient", "trip hop", "trip-hop", "funk",
                 "reggae")),
    (GENRE_ELECTRONIC, ("electronic", "electronica", "electro", "edm",
                        "house", "techno", "trance", "dubstep",
                        "drum and bass", "drum & bass", "dnb", "garage",
                        "synth", "synthpop", "synthwave", "electropop",
                        "dance", "idm", "hardstyle", "breakbeat", "disco")),
    (GENRE_ROCK, ("rock", "metal", "punk", "grunge", "hardcore")),
    (GENRE_POP, ("pop", "k-pop", "j-pop", "kpop", "jpop")),
    (GENRE_ROCK, ("indie", "alternative", "emo")),
    (GENRE_ACOUSTIC, ("acoustic", "instrumental", "classical", "folk",
                      "jazz", "soundtrack", "score", "piano", "orchestral",
                      "orchestra", "blues", "country", "singer-songwriter",
                      "new age", "opera")),
]

_GENRE_KEYWORD_PATTERNS = [
    (genre, re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in keywords)
        + r")(?![a-z0-9])"))
    for genre, keywords in _GENRE_KEYWORDS
]

# Tag values that mean "nobody filled this in", not an actual genre.
_GENRE_PLACEHOLDERS = {"", "other", "unknown", "none", "misc", "genre",
                       "default", "n/a", "na", "undefined"}


def normalize_genre(raw):
    """Map a raw genre string to a folder name: one of the core genres
    if it's recognisably one of them ("Rap", "Trap" -> "Hip-Hop"),
    otherwise the tidied-up tag itself. Returns None for an empty or
    placeholder tag."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = " ".join(raw.replace("\x00", " ").split())
    lowered = cleaned.lower()
    if lowered in _GENRE_PLACEHOLDERS or lowered == GENRE_UNSORTED.lower():
        return None
    for genre, pattern in _GENRE_KEYWORD_PATTERNS:
        if pattern.search(lowered):
            return genre
    return cleaned[:40]


def read_track_tags(filepath):
    """Return (raw_genre_or_None, [artist strings]) from *filepath*'s
    own ID3 tag. Never raises; an untagged or corrupt file just yields
    (None, [])."""
    if not MUTAGEN_AVAILABLE:
        return None, []
    try:
        tags = ID3(filepath)
    except Exception:
        return None, []
    genre = None
    artists = []
    try:
        for frame in tags.getall("TCON"):
            # .genres resolves ID3v1-style numeric references like "(17)"
            for value in (getattr(frame, "genres", None) or frame.text):
                if value and normalize_genre(str(value)):
                    genre = str(value)
                    break
            if genre:
                break
        for frame_id in ("TPE1", "TPE2"):
            for frame in tags.getall(frame_id):
                artists.extend(str(t) for t in frame.text if t)
    except Exception:
        pass
    return genre, artists


_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:/|,|;|&|\bfeat(?:uring)?\b\.?|\bft\b\.?)\s*", re.IGNORECASE)
_NON_TOKEN_RE = re.compile(r"[^a-z0-9$']+")


def _normalize_token_text(text):
    """Lowercase *text* and collapse every run of punctuation/separators
    to a single space, so "Don-Toliver", "Don_Toliver" and "Don Toliver"
    all compare equal."""
    return _NON_TOKEN_RE.sub(" ", text.lower()).strip()


def _split_artist_names(text):
    names = []
    for part in _ARTIST_SPLIT_RE.split(text):
        name = _normalize_token_text(part)
        if len(name) >= 3 and name not in names:
            names.append(name)
    return names


def build_track_identity(filepath, tag_artists):
    """Return (artist_names, searchable_text) for token propagation.

    artist_names come from the ID3 artist tag and, when the filename
    follows the common "Artist - Title" shape, from its prefix too.
    searchable_text is the normalised artist tags plus filename, padded
    with spaces so whole-phrase containment is a plain substring test."""
    stem = os.path.splitext(os.path.basename(filepath))[0]
    names = []
    for artist in tag_artists:
        for name in _split_artist_names(artist):
            if name not in names:
                names.append(name)
    spaced = stem.replace("_", " ")
    if " - " in spaced:
        for name in _split_artist_names(spaced.split(" - ", 1)[0]):
            if name not in names:
                names.append(name)
    text = _normalize_token_text(" ".join(tag_artists) + " " + stem)
    return names, f" {text} "


def propagate_genres_by_tokens(pending, resolved, identities, cancel):
    """Stage 2. Assign genres to *pending* paths by cross-referencing
    their artist names / filename tokens against tracks in *resolved*
    ({path: genre}). Runs repeated passes, so a collaborator who only
    becomes "known" in one pass can resolve further tracks in the next.
    Returns {path: genre} for the newly resolved tracks."""
    newly = {}
    known = dict(resolved)
    remaining = [p for p in pending if p not in known]
    for _pass in range(6):
        if not remaining or cancel.is_set():
            break
        artist_votes = collections.defaultdict(collections.Counter)
        known_texts = []
        for path, genre in known.items():
            names, text = identities.get(path, ([], " "))
            known_texts.append((text, genre))
            for name in names:
                artist_votes[name][genre] += 1
        progressed = []
        for path in remaining:
            if cancel.is_set():
                break
            names, text = identities.get(path, ([], " "))
            votes = collections.Counter()
            for name in names:
                if name in artist_votes:
                    votes.update(artist_votes[name])
                else:
                    # This track's artist, mentioned in a known track's
                    # filename/tags (e.g. as a "Ft-" guest).
                    needle = f" {name} "
                    for known_text, genre in known_texts:
                        if needle in known_text:
                            votes[genre] += 1
            if not votes:
                # A known artist's name appearing in this file's name.
                for name, counts in artist_votes.items():
                    if f" {name} " in text:
                        votes.update(counts)
            if votes:
                progressed.append((path, votes.most_common(1)[0][0]))
        if not progressed:
            break
        for path, genre in progressed:
            known[path] = genre
            newly[path] = genre
        done = {p for p, _g in progressed}
        remaining = [p for p in remaining if p not in done]
    return newly


def decode_pcm_slice(filepath, ffmpeg_path):
    """Decode a short mono slice of *filepath* to float samples in
    [-1, 1] via an ffmpeg subprocess. Returns a numpy array, or None if
    the file can't be decoded. Tries DSP_SLICE_OFFSET_SECONDS in first;
    falls back to the very start for tracks shorter than that."""
    for offset in (DSP_SLICE_OFFSET_SECONDS, 0):
        cmd = [ffmpeg_path, "-nostdin", "-v", "error",
               "-ss", str(offset), "-t", str(DSP_SLICE_SECONDS),
               "-i", filepath, "-vn", "-ac", "1",
               "-ar", str(DSP_SAMPLE_RATE), "-f", "s16le", "-"]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        raw = proc.stdout or b""
        raw = raw[:len(raw) - (len(raw) % 2)]
        if len(raw) // 2 >= DSP_SAMPLE_RATE * 3:   # at least 3s of audio
            return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return None


def extract_dsp_features(samples, sample_rate=DSP_SAMPLE_RATE):
    """Compute the handful of spectral/rhythm features the heuristic
    classifier uses. Returns a dict, or None for silence / too little
    audio.

      sub_bass    share of spectral power in 20-150 Hz
      centroid    mean spectral centroid in Hz ("brightness")
      zcr         zero-crossing rate per sample ("noisiness")
      pulse       0..1 strength of the steadiest beat period, from the
                  autocorrelation of the onset envelope
      onset_rate  detected onsets per second
      tempo       BPM of that steadiest period (octave-ambiguous)
    """
    n_fft, hop = 2048, 512
    if samples is None or len(samples) < n_fft * 8:
        return None
    if float(np.sqrt(np.mean(samples ** 2))) < 1e-4:
        return None  # digital silence

    n_frames = 1 + (len(samples) - n_fft) // hop
    index = (np.arange(n_fft)[None, :]
             + hop * np.arange(n_frames)[:, None])
    mag = np.abs(np.fft.rfft(samples[index] * np.hanning(n_fft), axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)

    power = (mag ** 2).mean(axis=0)
    total_power = float(power[freqs >= 20].sum())
    if total_power <= 0:
        return None
    sub_bass = float(power[(freqs >= 20) & (freqs <= 150)].sum()) / total_power

    frame_energy = mag.sum(axis=1)
    loud = frame_energy > 0.1 * frame_energy.mean()
    if not loud.any():
        return None
    centroid = float(((mag[loud] * freqs).sum(axis=1)
                      / frame_energy[loud]).mean())

    signs = np.signbit(samples)
    zcr = float(np.mean(signs[1:] != signs[:-1]))

    # Onset envelope: positive spectral flux on a log-compressed
    # spectrum, so a kick drum and a hi-hat both register.
    log_mag = np.log1p(1000.0 * mag / (mag.max() + 1e-12))
    flux = np.maximum(0.0, np.diff(log_mag, axis=0)).sum(axis=1)
    envelope = flux - flux.mean()
    spread = float(envelope.std())
    pulse, onset_rate, tempo = 0.0, 0.0, 0.0
    if spread > 0 and len(envelope) > 64:
        mid = envelope[1:-1]
        peaks = ((mid > envelope[:-2]) & (mid >= envelope[2:])
                 & (mid > 0.5 * spread))
        onset_rate = float(peaks.sum()) / (len(samples) / sample_rate)
        autocorr = np.correlate(envelope, envelope, mode="full")[
            len(envelope) - 1:]
        frames_per_second = sample_rate / hop
        lag_min = max(1, int(frames_per_second * 60 / 200))   # 200 BPM
        lag_max = min(len(autocorr) - 1,
                      int(frames_per_second * 60 / 60))       # 60 BPM
        if autocorr[0] > 0 and lag_max > lag_min:
            window = autocorr[lag_min:lag_max + 1]
            best = int(np.argmax(window))
            pulse = float(min(1.0, max(0.0, window[best] / autocorr[0])))
            tempo = 60.0 * frames_per_second / (lag_min + best)

    return {"sub_bass": sub_bass, "centroid": centroid, "zcr": zcr,
            "pulse": pulse, "onset_rate": onset_rate, "tempo": tempo}


# Per-genre feature "prototypes" as (typical value, tolerance). A track
# is assigned the genre whose prototype it sits closest to, measured in
# tolerances. These are hand-set rules of thumb about how each genre is
# usually mixed -- booming sub-bass for hip-hop, bright noisy guitars
# for rock, a rigid pulse for electronic -- not learned parameters.
_DSP_PROTOTYPES = {
    #                   sub_bass      centroid      zcr            pulse        onset_rate
    GENRE_HIPHOP:     ((0.70, 0.18), (2500, 700), (0.085, 0.040), (0.47, 0.18), (4.7, 1.2)),
    GENRE_ELECTRONIC: ((0.40, 0.15), (2800, 800), (0.100, 0.040), (0.72, 0.15), (5.5, 1.5)),
    GENRE_ROCK:       ((0.12, 0.10), (2900, 700), (0.130, 0.040), (0.30, 0.20), (4.5, 1.5)),
    GENRE_POP:        ((0.28, 0.12), (2600, 600), (0.100, 0.035), (0.45, 0.20), (4.5, 1.5)),
    GENRE_RNB:        ((0.45, 0.15), (1700, 500), (0.050, 0.025), (0.35, 0.20), (3.2, 1.2)),
    GENRE_ACOUSTIC:   ((0.06, 0.08), (1500, 700), (0.050, 0.030), (0.15, 0.15), (2.5, 1.5)),
}
_DSP_FEATURE_ORDER = ("sub_bass", "centroid", "zcr", "pulse", "onset_rate")


def classify_dsp_features(features):
    """Stage 3's decision: the core genre whose prototype is nearest."""
    best_genre, best_distance = None, None
    for genre in GENRE_ORDER:
        distance = 0.0
        for name, (typical, tolerance) in zip(_DSP_FEATURE_ORDER,
                                              _DSP_PROTOTYPES[genre]):
            distance += ((features[name] - typical) / tolerance) ** 2
        if best_distance is None or distance < best_distance:
            best_genre, best_distance = genre, distance
    return best_genre


def _genre_cache_paths(directory):
    """The cache file inside the music folder, then the per-folder
    fallback under ~/.config for folders that aren't writable."""
    digest = hashlib.md5(
        os.path.abspath(directory).encode("utf-8", "replace")).hexdigest()
    return [os.path.join(directory, GENRE_CACHE_NAME),
            os.path.join(GENRE_CACHE_FALLBACK_DIR, digest + ".json")]


def load_genre_cache(directory):
    """Return {mp3 filename: {"genre": str, "source": str}} for
    *directory*. Never raises; a missing or corrupt cache is just empty."""
    if not directory:
        return {}
    for path in _genre_cache_paths(directory):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            tracks = data.get("tracks") if isinstance(data, dict) else None
            if not isinstance(tracks, dict):
                continue
            return {
                name: {"genre": rec["genre"],
                       "source": str(rec.get("source", ""))}
                for name, rec in tracks.items()
                if isinstance(name, str) and isinstance(rec, dict)
                and isinstance(rec.get("genre"), str) and rec["genre"]
            }
        except Exception:
            continue
    return {}


def save_genre_cache(directory, cache):
    """Best-effort write of the genre cache; returns True on success."""
    if not directory:
        return False
    payload = {"version": 1, "tracks": cache}
    for path in _genre_cache_paths(directory):
        tmp_path = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp_path, path)
            return True
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return False


def run_autosort_worker(files, known, out, cancel):
    """AUTO-SORT background thread body.

    *files* is the flat list of library paths, *known* is {path: genre}
    for tracks already classified (from the cache). Never touches
    Tkinter: results are reported only by putting tuples on *out*, which
    the main thread drains from WaveStackApp._poll_autosort:

      ("progress", stage_label, done, total)
      ("genre", path, genre, source)        source: id3 / tokens / dsp
      ("done", stats_dict)

    If *cancel* is set the worker just returns without a "done"."""
    stats = {"id3": 0, "tokens": 0, "dsp": 0, "unsorted": 0, "note": ""}
    try:
        resolved = dict(known)
        identities = {}

        # Stage 1: embedded ID3 genre tags. Artists are read for every
        # track, including already-known ones, because stage 2 needs
        # them as its reference set.
        total = len(files)
        for i, path in enumerate(files):
            if cancel.is_set():
                return
            raw_genre, artists = read_track_tags(path)
            identities[path] = build_track_identity(path, artists)
            if path not in resolved:
                genre = normalize_genre(raw_genre)
                if genre:
                    resolved[path] = genre
                    stats["id3"] += 1
                    out.put(("genre", path, genre, "id3"))
            out.put(("progress", "ID3", i + 1, total))

        # Stage 2: token propagation from known tracks.
        pending = [p for p in files if p not in resolved]
        if pending and resolved:
            out.put(("progress", "TOKENS", 0, len(pending)))
            found = propagate_genres_by_tokens(pending, resolved,
                                               identities, cancel)
            if cancel.is_set():
                return
            for path, genre in found.items():
                resolved[path] = genre
                stats["tokens"] += 1
                out.put(("genre", path, genre, "tokens"))
            out.put(("progress", "TOKENS", len(found), len(pending)))

        # Stage 3: spectral analysis of whatever is still unknown.
        pending = [p for p in files if p not in resolved]
        if pending:
            ffmpeg_path = shutil.which("ffmpeg")
            if not NUMPY_AVAILABLE:
                stats["note"] = "DSP skipped: numpy not installed"
            elif not ffmpeg_path:
                stats["note"] = "DSP skipped: ffmpeg not installed"
            else:
                total = len(pending)
                for i, path in enumerate(pending):
                    if cancel.is_set():
                        return
                    out.put(("progress", "DSP", i, total))
                    try:
                        features = extract_dsp_features(
                            decode_pcm_slice(path, ffmpeg_path))
                    except Exception:
                        features = None  # one odd file must not end the run
                    if features:
                        genre = classify_dsp_features(features)
                        resolved[path] = genre
                        stats["dsp"] += 1
                        out.put(("genre", path, genre, "dsp"))
                out.put(("progress", "DSP", total, total))

        stats["unsorted"] = sum(1 for p in files if p not in resolved)
    except Exception as exc:
        stats["note"] = f"stopped early: {exc}"
    out.put(("done", stats))


# --------------------------------------------------------------------------
# Online lyrics sync (LRCLIB)
#
# Only ever runs after the user switches the status-bar toggle to
# [ Online Sync ]. Like the AUTO-SORT section above, nothing here
# touches Tkinter, so it is safe on the sync worker thread.
# --------------------------------------------------------------------------

LRCLIB_API = "https://lrclib.net/api"
LRCLIB_HEADERS = {"User-Agent": f"WaveStack/{APP_VERSION}"}
LRCLIB_TIMEOUT_SECONDS = 15
LRCLIB_ATTEMPTS = 3
# Synced lyrics timed for a different cut of the song (radio edit,
# extended mix...) would scroll out of step, so a search result is only
# accepted if its duration is within this many seconds of the file's.
LRCLIB_DURATION_TOLERANCE = 5
# This many songs in a row failing with network errors means LRCLIB (or
# the connection) is down, and the sync switches itself back offline.
LRCLIB_MAX_CONSECUTIVE_FAILURES = 3

# Download sites stamp their name into titles and filenames in endless
# variations ("E85 | HipHopKit.com", "Honest_(mp3.pm)", "[www.site.net]").
# Rather than list sites, anything shaped like a domain name is junk.
_JUNK_DOMAIN = (
    r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\."
    r"(?:com|net|org|pm|io|co|me|ru|cc|to|xyz|info|biz|in|ng|fm|tv|uk|us|"
    r"za|site|online|club|app|live|link|top|vip|ws|su|fun|music|audio|"
    r"download|lol|is|ly|gg|pro|store|blog)\b(?:/\S*)?")
_JUNK_DOMAIN_RE = re.compile(_JUNK_DOMAIN, re.IGNORECASE)

# Words that mark a bracketed group or "|"-separated segment as
# packaging rather than part of the song's name.
_JUNK_WORDS = (
    r"official|video|audio|lyrics?|visuali[sz]er|hq|hd|4k|\d{2,3}\s?kbps|"
    r"kbps|download|mp3|m4a|explicit|clean|dirty|prod(?:uced)?\.?(?:\s+by)?|"
    r"no\s+dj|remaster(?:ed)?|full\s+song|new\s+song|out\s+now|"
    r"free\s+download|high\s+quality")
_JUNK_GROUP_RE = re.compile(
    r"\s*[\(\[\{][^\(\)\[\]\{\}]*(?:" + _JUNK_DOMAIN + r"|\b(?:"
    + _JUNK_WORDS + r")\b)[^\(\)\[\]\{\}]*[\)\]\}]", re.IGNORECASE)
_JUNK_SEGMENT_RE = re.compile(
    _JUNK_DOMAIN + r"|^\s*(?:(?:" + _JUNK_WORDS + r"|music|the|by)\s*)+$",
    re.IGNORECASE)
_JUNK_TRAILING_RE = re.compile(
    r"(?:\s+|^)(?:official\s+(?:music\s+)?(?:video|audio)|lyrics?\s+video|"
    r"official\s+lyrics?|\d{2,3}\s?kbps|free\s+download|mp3\s+download|"
    r"no\s+dj|hq|hd)\s*$", re.IGNORECASE)
_LEADING_TRACK_NO_RE = re.compile(r"^\d{1,2}(?:[.)]\s+|\s+-\s+)")
_FEATURING_RE = re.compile(
    r"\s*[\(\[]\s*(?:feat(?:uring)?|ft|with)\b[^\)\]]*[\)\]]"
    r"|\s+(?:feat(?:uring)?|ft)\b\.?\s.*$", re.IGNORECASE)


def clean_track_title(text, from_filename=False):
    """Strip download-site junk from a title tag or filename so it can
    be looked up: site names in any position or bracket style, "|"
    suffixes, "Official Video" / "320kbps" / "No DJ" style packaging.
    With from_filename=True also turns underscores into spaces and drops
    a leading track number. Never returns less than it was given a
    reason to: if cleaning would leave nothing, the tidied original is
    returned instead."""
    if not text:
        return ""
    original = " ".join(str(text).replace("\x00", " ").split())
    s = original.replace("\u2019", "'").replace("\u2018", "'")
    if from_filename:
        s = s.replace("_", " ")
    previous = None
    while previous != s:          # nested/adjacent groups: "(x) [y]"
        previous = s
        s = _JUNK_GROUP_RE.sub("", s)
    # "Title | Site.com", "Title || Official Video": keep the real parts
    segments = [seg for seg in re.split(r"\s*\|+\s*|\s+~\s+|\s+//\s+", s)
                if seg.strip() and not _JUNK_SEGMENT_RE.search(seg)]
    if segments:
        s = segments[0]
    s = _JUNK_DOMAIN_RE.sub(" ", s)
    previous = None
    while previous != s:
        previous = s
        s = _JUNK_TRAILING_RE.sub("", s).strip(" -_|.,~")
    if from_filename:
        s = _LEADING_TRACK_NO_RE.sub("", s)
    s = " ".join(s.split()).strip(" -_|.,~")
    return s or original


def strip_featuring(title):
    """ "Around Me (feat. Don Toliver)" -> "Around Me". """
    return _FEATURING_RE.sub("", title).strip() or title


def _primary_artist(text):
    """The first-billed artist of "A, B & C" / "A/B" / "A ft. B"."""
    for part in _ARTIST_SPLIT_RE.split(text or ""):
        if part.strip():
            return part.strip()
    return ""


def describe_track_for_lyrics(mp3_path):
    """Work out what to ask LRCLIB for. Returns a dict with:

      title     cleaned track title ("" if it can't be told apart
                from the artist -- see query)
      artist    first-billed artist, "" if unknown
      duration  length in seconds, or None
      query     free-text fallback: the cleaned filename, for files
                with no usable tags

    Tags win over the filename; the filename fills in whatever the
    tags lack."""
    title = artist = ""
    duration = None
    if MUTAGEN_AVAILABLE:
        try:
            tags = EasyID3(mp3_path)
            title = (tags.get("title") or [""])[0]
            artist = (tags.get("artist") or [""])[0]
        except Exception:
            pass
        try:
            duration = float(MP3(mp3_path).info.length)
        except Exception:
            duration = None

    stem = os.path.splitext(os.path.basename(mp3_path))[0]
    cleaned_stem = clean_track_title(stem, from_filename=True)
    if " " not in cleaned_stem:
        # "Don-Toliver-E85": hyphens are the only word separators
        cleaned_stem = " ".join(cleaned_stem.replace("-", " ").split())

    title = clean_track_title(title)
    artist = _primary_artist(clean_track_title(artist))
    if " - " in cleaned_stem:
        file_artist, file_title = cleaned_stem.split(" - ", 1)
        if not title:
            title = file_title.strip()
        if not artist:
            artist = _primary_artist(file_artist)
    return {"title": title, "artist": artist, "duration": duration,
            "query": cleaned_stem}


class LrclibUnavailable(Exception):
    """LRCLIB couldn't be reached, or kept answering with server errors."""


def _lrclib_request(endpoint, params, cancel):
    """GET an LRCLIB endpoint, retrying timeouts, 429s and 5xx answers
    with a short back-off. Returns parsed JSON, or None for a definite
    "no such thing" (404 / 400). Raises LrclibUnavailable if every
    attempt failed."""
    for attempt in range(LRCLIB_ATTEMPTS):
        if cancel.is_set():
            return None
        try:
            response = requests.get(f"{LRCLIB_API}/{endpoint}", params=params,
                                    headers=LRCLIB_HEADERS,
                                    timeout=LRCLIB_TIMEOUT_SECONDS)
            if response.status_code == 200:
                return response.json()
            if response.status_code in (400, 404):
                return None
        except (requests.RequestException, ValueError):
            pass
        # cancel.wait() doubles as an interruptible sleep
        if cancel.wait(1.5 * (attempt + 1)):
            return None
    raise LrclibUnavailable(endpoint)


def _pick_synced_result(results, title, artist, duration, query_text):
    """Choose the best synced-lyrics record from an LRCLIB search, or
    None. A record must actually be this song -- same title, and
    (where known) same artist and a matching duration."""
    want_title = _normalize_token_text(strip_featuring(title)) if title else ""
    want_artist = _normalize_token_text(artist) if artist else ""
    query_tokens = set(_normalize_token_text(query_text).split())
    best, best_gap = None, None
    for record in results if isinstance(results, list) else []:
        if not isinstance(record, dict) or not record.get("syncedLyrics"):
            continue
        got_title = _normalize_token_text(
            strip_featuring(str(record.get("trackName") or "")))
        got_artist = _normalize_token_text(str(record.get("artistName") or ""))
        if not got_title:
            continue
        if want_title:
            if got_title != want_title:
                continue
            if want_artist and f" {want_artist} " not in f" {got_artist} ":
                continue
        else:
            # Free-text search: every word of the record's title, and
            # its first-billed artist, must appear in our filename.
            lead = _normalize_token_text(
                _primary_artist(str(record.get("artistName") or "")))
            if not set(got_title.split()) <= query_tokens:
                continue
            if not lead or not set(lead.split()) <= query_tokens:
                continue
        gap = 0.0
        if duration and record.get("duration"):
            try:
                gap = abs(float(record["duration"]) - duration)
            except (TypeError, ValueError):
                gap = 0.0
            if gap > LRCLIB_DURATION_TOLERANCE:
                continue
        if best_gap is None or gap < best_gap:
            best, best_gap = record, gap
    return best


def fetch_synced_lyrics(info, cancel):
    """Look one track up on LRCLIB. Returns the LRC text, or None if
    LRCLIB has no synced lyrics for it. Raises LrclibUnavailable on
    network trouble.

    Tries the cheap exact-match endpoint first, then the fuzzy search
    (with and without any "feat." suffix), then a free-text search on
    the cleaned filename for files with no usable tags."""
    title, artist = info["title"], info["artist"]
    duration, query = info["duration"], info["query"]

    if title and artist:
        variants = [title]
        if strip_featuring(title) != title:
            variants.append(strip_featuring(title))
        for variant in variants:
            record = _lrclib_request(
                "get", {"track_name": variant, "artist_name": artist}, cancel)
            if isinstance(record, dict) and record.get("syncedLyrics"):
                gap = 0.0
                try:
                    if duration and record.get("duration"):
                        gap = abs(float(record["duration"]) - duration)
                except (TypeError, ValueError):
                    pass
                if gap <= LRCLIB_DURATION_TOLERANCE:
                    return record["syncedLyrics"]
        for variant in variants:
            if cancel.is_set():
                return None
            results = _lrclib_request(
                "search", {"track_name": variant, "artist_name": artist},
                cancel)
            record = _pick_synced_result(results, variant, artist, duration,
                                         query)
            if record:
                return record["syncedLyrics"]

    if query and not cancel.is_set():
        results = _lrclib_request("search", {"q": query}, cancel)
        record = _pick_synced_result(results, "" if not artist else title,
                                     artist, duration, query)
        if record:
            return record["syncedLyrics"]
    return None


def run_lyrics_sync_worker(directory, fallback_files, out, cancel):
    """Online-sync background thread body: for every MP3 with no .lrc
    beside it, try to download synced lyrics from LRCLIB.

    Reports only through *out* (drained by WaveStackApp._tick):

      ("status", text)
      ("downloaded", mp3_path)
      ("unreachable",)      LRCLIB can't be reached; go back offline

    One song failing never stops the run; only
    LRCLIB_MAX_CONSECUTIVE_FAILURES network failures in a row do."""
    out.put(("status", "Scanning local .lrc files..."))
    files = scan_mp3_files(directory) if directory else list(fallback_files)
    if not files:
        out.put(("status", "No tracks to sync."))
        return
    missing = [p for p in files
               if not os.path.exists(os.path.splitext(p)[0] + ".lrc")]
    if not missing:
        out.put(("status", f"All {len(files)} tracks already have lyrics."))
        return

    downloaded = not_found = failed = consecutive_failures = 0
    for i, mp3_path in enumerate(missing):
        if cancel.is_set():
            return
        out.put(("status", f"Fetching lyrics {i + 1}/{len(missing)}: "
                           f"{os.path.basename(mp3_path)}..."))
        try:
            lyrics = fetch_synced_lyrics(describe_track_for_lyrics(mp3_path),
                                         cancel)
        except LrclibUnavailable:
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= LRCLIB_MAX_CONSECUTIVE_FAILURES:
                out.put(("status",
                         f"Can't reach LRCLIB -- sync paused "
                         f"({downloaded} downloaded so far)."))
                out.put(("unreachable",))
                return
            continue
        except Exception:
            failed += 1   # one odd file must not end the run
            continue
        if cancel.is_set():
            return
        consecutive_failures = 0
        if lyrics and _LRC_TIMESTAMP_RE.search(lyrics):
            lrc_path = os.path.splitext(mp3_path)[0] + ".lrc"
            try:
                with open(lrc_path + ".tmp", "w", encoding="utf-8") as fh:
                    fh.write(lyrics)
                os.replace(lrc_path + ".tmp", lrc_path)
                downloaded += 1
                out.put(("downloaded", mp3_path))
            except OSError:
                failed += 1
        else:
            not_found += 1
        if cancel.wait(0.3):   # be polite to a free public API
            return

    summary = f"Lyrics sync done: {downloaded} downloaded"
    if not_found:
        summary += f", {not_found} not on LRCLIB"
    if failed:
        summary += f", {failed} failed (toggle again to retry)"
    out.put(("status", summary + "."))


# --------------------------------------------------------------------------
# Retro dialog box (replaces tkinter.messagebox so error/info popups
# match the rest of the UI instead of looking like a modern GTK dialog)
# --------------------------------------------------------------------------

class RetroDialog(tk.Toplevel):
    """A chunky, beveled message box in the style of a 1990s Windows
    dialog. The OS still draws the title bar (for compatibility with
    Wayland window managers); everything below it is custom-styled."""

    ICON_GLYPHS = {"info": "i", "warning": "!", "error": "X"}

    def __init__(self, parent, title, message, kind="info", buttons=None, theme=None):
        super().__init__(parent)
        if theme is None:
            theme = getattr(parent, "theme", None) or THEMES[DEFAULT_THEME]
        self.theme = theme

        self.configure(bg=theme["bg"])
        self.resizable(False, False)
        self.title(title)
        self.transient(parent)
        self.result = None

        outer = tk.Frame(self, bg=theme["bg"], bd=2, relief=tk.RAISED)
        outer.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)

        content = tk.Frame(outer, bg=theme["bg"])
        content.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        icon_glyph = self.ICON_GLYPHS.get(kind, "i")
        icon_color_map = {
            "info": theme["title_fg"],
            "warning": "#C89600" if theme["name"] == "dark" else "#806000",
            "error": "#FF4D4D" if theme["name"] == "dark" else "#800000"
        }
        icon_color = icon_color_map.get(kind, theme["fg"])
        icon_frame = tk.Frame(content, bg=theme["entry_bg"], bd=2, relief=tk.SUNKEN,
                               width=36, height=36)
        icon_frame.grid(row=0, column=0, padx=(0, 14), sticky="n")
        icon_frame.grid_propagate(False)
        tk.Label(icon_frame, text=icon_glyph, bg=theme["entry_bg"], fg=icon_color,
                 font=get_retro_font(self, 16, bold=True)).place(
            relx=0.5, rely=0.5, anchor="center")

        tk.Label(content, text=message, bg=theme["bg"], fg=theme["fg"], justify=tk.LEFT,
                 wraplength=320, font=get_retro_font(self, 9)).grid(
            row=0, column=1, sticky="w")

        button_row = tk.Frame(outer, bg=theme["bg"])
        button_row.pack(fill=tk.X, padx=14, pady=(0, 14))

        if buttons is None:
            buttons = [("OK", "ok")]

        for label, value in buttons:
            tk.Button(
                button_row, text=label, width=11,
                font=get_retro_font(self, 9), bg=theme["btn_bg"], fg=theme["btn_fg"],
                relief=tk.RAISED, bd=3,
                activebackground=theme["btn_active_bg"],
                activeforeground=theme["btn_active_fg"],
                command=lambda v=value: self._finish(v),
            ).pack(side=tk.RIGHT, padx=(6, 0))

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(None))
        self.update_idletasks()
        self._center_over(parent)
        self.grab_set()
        self.focus_set()
        self.wait_window(self)

    def _center_over(self, parent):
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            w = self.winfo_width()
            h = self.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 2)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass  # positioning is a nicety on some window managers
            # (notably Wayland) may ignore it; never worth crashing over

    def _finish(self, value):
        self.result = value
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


# --------------------------------------------------------------------------
# "LCD" now-playing marquee, Winamp-style
# --------------------------------------------------------------------------

class NowPlayingDisplay(tk.Frame):
    """A small scrolling LCD-style readout for the current track name."""

    def __init__(self, parent):
        super().__init__(parent, bg=BORDER_BLACK, bd=2, relief=tk.SUNKEN)
        self.canvas = tk.Canvas(self, bg=LCD_BG, height=30,
                                 highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.font = get_retro_font(self, 12, bold=True)
        self.text = "WAVESTACK READY"
        self.x = 6
        self.text_id = self.canvas.create_text(
            self.x, 16, text=self.text, fill=LCD_FG, font=self.font,
            anchor="w")
        self.canvas.bind("<Configure>", lambda e: self._reset_scroll())

    def set_text(self, text):
        self.text = (text or "").upper()
        self.canvas.itemconfigure(self.text_id, text=self.text)
        self._reset_scroll()

    def _reset_scroll(self):
        self.x = 6
        self.canvas.coords(self.text_id, self.x, 16)

    def tick(self):
        bbox = self.canvas.bbox(self.text_id)
        if not bbox:
            return
        text_width = bbox[2] - bbox[0]
        canvas_width = self.canvas.winfo_width()
        if text_width <= canvas_width:
            return  # fits without scrolling
        self.x -= 2
        if self.x < -text_width:
            self.x = canvas_width
        self.canvas.coords(self.text_id, self.x, 16)

    def apply_theme(self, theme):
        self.configure(bg=theme.get("border_black", BORDER_BLACK))
        self.canvas.configure(bg=theme.get("lcd_bg", LCD_BG))
        self.canvas.itemconfigure(self.text_id, fill=theme.get("lcd_fg", LCD_FG))


# --------------------------------------------------------------------------
# Spinning vinyl record widget
# --------------------------------------------------------------------------

class VinylDisc(tk.Frame):
    """A procedurally-drawn, animated vinyl record.

    Spins clockwise while a track plays, freezes at its current angle
    the instant it's paused, and resets to 0 degrees when stopped or
    nothing is loaded. If the loaded MP3 has embedded ID3 cover art,
    it's masked into a circle and composited onto the label; a track
    with no embedded art just shows the plain disc.

    Performance note: load_track() does the (relatively) expensive
    work -- extracting art, cropping, masking, compositing -- exactly
    once per track and caches the result in self._base_image. The
    50ms animation loop only rotates that already-built image; it
    never re-extracts or re-composites per frame.

    Degrades gracefully if Pillow isn't installed: shows a plain
    static disc drawn with Tkinter's own canvas primitives (no PIL
    needed for that) and a short note, rather than crashing or
    leaving a blank gap. The rest of the player is unaffected either
    way -- this widget's dependencies are optional, unlike libVLC.
    """

    SIZE = 120
    SPIN_STEP_DEGREES = 5   # ~3.6s per rotation at TICK_MS below
    TICK_MS = 50             # matches the requested update cadence

    def __init__(self, parent, theme=None):
        if theme is None:
            theme = getattr(parent, "theme", None) or THEMES[DEFAULT_THEME]
        self.theme = theme
        bg = theme["bg"]
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, width=self.SIZE, height=self.SIZE,
                                 bg=bg, highlightthickness=0)
        self.canvas.pack()

        self._angle = 0.0
        self._spinning = False
        self._after_id = None
        self._image_item = None
        self._current_photo = None   # persistent PhotoImage reference --
        # Tkinter/Tcl does not keep its own strong reference to the
        # image data behind a PhotoImage, only to the (tiny) handle
        # object. If this attribute were allowed to be reassigned
        # without anything else referencing the old PhotoImage first,
        # or simply never stored at all, Python's garbage collector
        # would free it and the canvas would go blank or flicker --
        # a well-known Tkinter pitfall this attribute exists to avoid.

        if PIL_AVAILABLE:
            self._blank_vinyl = self._draw_blank_vinyl()
            self._base_image = self._blank_vinyl
            self._render_frame()
        else:
            self._blank_vinyl = None
            self._base_image = None
            self.canvas.create_oval(4, 4, self.SIZE - 4, self.SIZE - 4,
                                     fill="#202020", outline=theme.get("border_black", BORDER_BLACK))
            self.canvas.create_text(
                self.SIZE // 2, self.SIZE // 2,
                text="Vinyl needs\nPillow", fill=theme.get("search_placeholder_fg", "#999999"),
                font=("TkDefaultFont", 7), justify=tk.CENTER)

    def apply_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme["bg"])
        self.canvas.configure(bg=theme["bg"])

    # -- drawing -----------------------------------------------

    def _draw_blank_vinyl(self):
        size = self.SIZE
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        cx = cy = size / 2
        disc_r = size / 2 - 2

        draw.ellipse((cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r),
                     fill=VINYL_DISC_COLOR, outline=VINYL_RIM_HIGHLIGHT)

        label_r = disc_r * 0.36
        groove_count = 10
        for i in range(groove_count):
            r = label_r + (disc_r - label_r) * (i + 1) / (groove_count + 1)
            color = VINYL_GROOVE_LIGHT if i % 2 == 0 else VINYL_GROOVE_DARK
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color)

        draw.ellipse((cx - label_r, cy - label_r, cx + label_r, cy + label_r),
                     fill=VINYL_LABEL_COLOR)

        self._draw_spindle_hole(draw, cx, cy, disc_r)
        return img

    def _draw_spindle_hole(self, draw, cx, cy, disc_r):
        hole_r = max(2, disc_r * 0.045)
        draw.ellipse((cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r),
                     fill=VINYL_HOLE_COLOR)

    def _composite_album_art(self, art_bytes):
        try:
            art = Image.open(BytesIO(art_bytes))
            art.load()  # force full decode now, so a truncated/corrupt
            art = art.convert("RGB")  # image is caught here, not later
        except Exception:
            return self._blank_vinyl

        size = self.SIZE
        cx = cy = size / 2
        disc_r = size / 2 - 2
        disc_d = max(2, int(disc_r * 2))

        art = _crop_to_square(art).resize((disc_d, disc_d), Image.LANCZOS)
        mask = _make_circular_mask(disc_d)
        circular_art = Image.new("RGBA", (disc_d, disc_d), (0, 0, 0, 0))
        circular_art.paste(art, (0, 0), mask)

        combined = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        top_left = (int(cx - disc_r), int(cy - disc_r))
        combined.paste(circular_art, top_left, circular_art)

        # Draw outer rim highlight and spindle hole on top so it maintains
        # authentic vinyl record aesthetics (picture disc style).
        draw = ImageDraw.Draw(combined)
        draw.ellipse((cx - disc_r, cy - disc_r, cx + disc_r, cy + disc_r),
                     outline=VINYL_RIM_HIGHLIGHT)
        self._draw_spindle_hole(draw, cx, cy, disc_r)
        return combined

    # -- public API -----------------------------------------------

    def load_track(self, filepath):
        """Call once when a new track starts playing. Does all the
        expensive image work up front and caches the result -- see
        the class docstring's performance note."""
        if not PIL_AVAILABLE:
            return
        art_bytes = extract_album_art_bytes(filepath)
        self._base_image = (self._composite_album_art(art_bytes)
                             if art_bytes else self._blank_vinyl)
        self._angle = 0.0
        self._render_frame()

    def start_spinning(self):
        if not PIL_AVAILABLE:
            return
        if self._spinning and self._after_id is not None:
            return  # already spinning; don't disturb the tick timer
        self._spinning = True
        self._schedule_next_tick()

    def pause_spinning(self):
        """Stops advancing rotation but leaves the angle exactly where
        it was -- paused should look frozen mid-turn, not reset."""
        self._spinning = False
        self._cancel_pending_tick()

    def stop_and_reset(self):
        self._spinning = False
        self._cancel_pending_tick()
        self._angle = 0.0
        if PIL_AVAILABLE:
            self._render_frame()

    def shutdown(self):
        self._cancel_pending_tick()

    # -- animation internals -----------------------------------------------

    def _schedule_next_tick(self):
        self._cancel_pending_tick()
        self._after_id = self.after(self.TICK_MS, self._tick)

    def _cancel_pending_tick(self):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _tick(self):
        self._after_id = None
        if not self._spinning:
            return
        # PIL's rotate() turns counter-clockwise for positive angles,
        # so subtracting the step (then wrapping into 0-359) is what
        # actually produces clockwise motion on screen.
        self._angle = (self._angle - self.SPIN_STEP_DEGREES) % 360
        self._render_frame()
        self._after_id = self.after(self.TICK_MS, self._tick)

    def _render_frame(self):
        if self._base_image is None:
            return
        rotated = self._base_image.rotate(self._angle, resample=Image.BICUBIC)
        photo = ImageTk.PhotoImage(rotated)
        self._current_photo = photo  # see the __init__ comment on why
        if self._image_item is None:
            self._image_item = self.canvas.create_image(
                self.SIZE // 2, self.SIZE // 2, image=photo)
        else:
            self.canvas.itemconfig(self._image_item, image=photo)

class Visualizer(tk.Frame):
    """A simulated, 90s-style LED spectrum analyzer.
 
    Extracting real-time PCM sample data from libVLC in pure Python
    is fragile and version-dependent -- it generally needs a custom
    audio callback wired up through ctypes against a specific libvlc
    build, which is a poor fit for a robustness-first app. Instead,
    this draws a convincing, audio-reactive-*looking* bar animation:
    each bar occasionally rolls a new random target height, then
    eases toward it with a fast attack and a slower decay -- the same
    asymmetry real level meters use, since it reads as musical motion
    rather than as flickering noise.
 
    Ties into the exact same three-state model the vinyl disc does
    (see WaveStackApp._sync_playback_visuals): animates only while
    genuinely playing, freezes instantly -- holding its current bar
    heights -- on pause, and drops flat to zero on stop. Peak bar
    heights scale with the current volume slider via set_volume().
 
    The canvas is a fixed black-with-green-LEDs "hardware display",
    like the LCD now-playing marquee -- deliberately NOT theme-tinted,
    the same way a real equalizer's display doesn't repaint itself
    when you change your desktop wallpaper. Only the frame around it
    follows the current theme, for a tidy edge.
    """
 
    BAR_COUNT = 28
    SEGMENTS_PER_BAR = 14
    TICK_MS = 60   # ~16 fps: smooth, but leaves plenty of headroom
 
    CANVAS_BG = "#050505"
    SEGMENT_OFF = "#123312"
    SEGMENT_GREEN = "#39FF14"
    SEGMENT_YELLOW = "#E8FF39"
    SEGMENT_RED = "#FF3B30"
    HINT_FG = "#2E6B2E"
 
    def __init__(self, parent, theme=None):
        if theme is None:
            theme = getattr(parent, "theme", None) or THEMES[DEFAULT_THEME]
        self.theme = theme
        super().__init__(parent, bg=theme["bg"], bd=2, relief=tk.SUNKEN)
 
        self.canvas = tk.Canvas(self, bg=self.CANVAS_BG, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
 
        self._running = False
        self._after_id = None
        self._volume_percent = 70
        self._heights = [0.0] * self.BAR_COUNT
        self._targets = [0.0] * self.BAR_COUNT
        # Bars toward the left re-roll their target less often (bigger,
        # more sustained swings, evoking bass); bars toward the right
        # re-roll more often (quicker flickers, evoking treble) -- a
        # recognizable, classic spectrum-analyzer visual pattern.
        self._reroll_chance = [
            0.06 + 0.22 * (i / max(1, self.BAR_COUNT - 1))
            for i in range(self.BAR_COUNT)
        ]
        self._segment_ids = [[] for _ in range(self.BAR_COUNT)]
        self._hint_id = None
 
        self.canvas.bind("<Configure>", lambda e: self._layout(e.width, e.height))
        self.canvas.bind("<Escape>", lambda e: self._request_close())
        self.bind("<Escape>", lambda e: self._request_close())
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set(), add="+")
        self.after_idle(self._layout)

    def _request_close(self):
        top = self.winfo_toplevel()
        if hasattr(top, "_on_global_escape"):
            top._on_global_escape()
        elif hasattr(top, "toggle_visualizer"):
            top.toggle_visualizer()
        return "break"
 
    def apply_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme["bg"])
        # The canvas itself intentionally stays fixed black/green
        # regardless of theme -- see the class docstring.
 
    # -- public API -----------------------------------------------
 
    def set_volume(self, percent):
        self._volume_percent = max(0, min(100, int(percent)))
 
    def start_animating(self):
        if self._running and self._after_id is not None:
            return  # already animating; don't disturb the tick timer
        self._running = True
        self._schedule_next_tick()
 
    def pause_animating(self):
        """Freezes exactly where the bars currently are -- paused
        should look like the music stopped mid-beat, not reset."""
        self._running = False
        self._cancel_pending_tick()
 
    def stop_and_reset(self):
        self._running = False
        self._cancel_pending_tick()
        self._heights = [0.0] * self.BAR_COUNT
        self._targets = [0.0] * self.BAR_COUNT
        self._render_frame()
 
    def shutdown(self):
        self._cancel_pending_tick()
 
    # -- animation internals ----------------------------------------------
 
    def _schedule_next_tick(self):
        self._cancel_pending_tick()
        self._after_id = self.after(self.TICK_MS, self._tick)
 
    def _cancel_pending_tick(self):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
 
    def _tick(self):
        self._after_id = None
        if not self._running:
            return
        vol_scale = self._volume_percent / 100.0
        for i in range(self.BAR_COUNT):
            if random.random() < self._reroll_chance[i]:
                self._targets[i] = random.uniform(0.12, 1.0) * vol_scale
            current = self._heights[i]
            target = self._targets[i]
            if target > current:
                self._heights[i] = current + (target - current) * 0.55  # fast attack
            else:
                self._heights[i] = current + (target - current) * 0.18  # slower decay
        self._render_frame()
        self._after_id = self.after(self.TICK_MS, self._tick)
 
    # -- drawing -----------------------------------------------
 
    def _layout(self, width=None, height=None):
        """(Re)builds the LED segment grid to fit the canvas's current
        size. Safe to call on resize: it doesn't touch self._heights,
        it only repositions/recreates the rectangles that display
        them, so a resize never disturbs the running animation."""
        if width is None:
            width = self.canvas.winfo_width()
        if height is None:
            height = self.canvas.winfo_height()
        if width <= 2 or height <= 2:
            return  # not mapped/sized yet (e.g. before it's ever packed)
        self.canvas.delete("all")
        self._segment_ids = [[] for _ in range(self.BAR_COUNT)]
 
        margin = 6
        hint_h = 16
        usable_h = max(10, height - margin * 2 - hint_h)
        usable_w = max(10, width - margin * 2)
        gap = 2
        bar_w = max(2, (usable_w - gap * (self.BAR_COUNT - 1)) / self.BAR_COUNT)
        seg_gap = 1
        seg_h = max(2, (usable_h - seg_gap * (self.SEGMENTS_PER_BAR - 1))
                    / self.SEGMENTS_PER_BAR)
 
        for i in range(self.BAR_COUNT):
            x0 = margin + i * (bar_w + gap)
            x1 = x0 + bar_w
            for row in range(self.SEGMENTS_PER_BAR):
                # row 0 = bottom segment, drawn first
                y1 = margin + usable_h - row * (seg_h + seg_gap)
                y0 = y1 - seg_h
                rect = self.canvas.create_rectangle(
                    x0, y0, x1, y1, fill=self.SEGMENT_OFF, outline="")
                self._segment_ids[i].append(rect)
 
        self._hint_id = self.canvas.create_text(
            width // 2, height - hint_h // 2,
            text="Press ESC to return", fill=self.HINT_FG,
            font=("TkDefaultFont", 8))
        self.canvas.tag_bind(self._hint_id, "<Button-1>", lambda e: self._request_close())
 
        self._render_frame()
 
    def _segment_color(self, row_from_bottom):
        frac = row_from_bottom / max(1, self.SEGMENTS_PER_BAR - 1)
        if frac >= 0.85:
            return self.SEGMENT_RED
        if frac >= 0.65:
            return self.SEGMENT_YELLOW
        return self.SEGMENT_GREEN
 
    def _render_frame(self):
        for i, h in enumerate(self._heights):
            lit = int(round(h * self.SEGMENTS_PER_BAR))
            ids = self._segment_ids[i] if i < len(self._segment_ids) else []
            for row, seg_id in enumerate(ids):
                color = self._segment_color(row) if row < lit else self.SEGMENT_OFF
                self.canvas.itemconfig(seg_id, fill=color)
 
 


# --------------------------------------------------------------------------
# CRT Lyrics Terminal widget
# --------------------------------------------------------------------------

class CrtLyricsDisplay(tk.Frame):
    """A retro CRT phosphor-green lyrics terminal synced to libVLC playback.

    The widget is always styled as a phosphor-green CRT screen regardless of
    the outer chassis theme -- a real monitor doesn't change colour when you
    repaint the case. The only thing that adapts to the theme is the outer
    border *frame* background, so there's no ugly gap between the chassis and
    the screen bezel.

    Syncing works by calling sync_to_ms(ms) from the main _tick() loop. The
    method walks the pre-parsed LRC entry list backwards to find the last line
    whose timestamp is <= the current position, highlights it with a classic
    reverse-video phosphor effect, and scrolls the Text widget to keep that
    line vertically centred.
    """

    _MSG_WAITING  = "[ WAITING FOR TRACK... ]"
    _MSG_NO_LYRICS = "[ NO SYNCED LYRICS AVAILABLE ]"

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=CRT_BG, bd=3, relief=tk.SUNKEN, **kwargs)

        # Title banner
        self._banner = tk.Label(
            self, text="\u2500 LYRICS TERMINAL [SYNCED] \u2500",
            bg=CRT_BG, fg=CRT_BANNER_FG,
            font=("TkFixedFont", 8, "bold"),
            anchor="center"
        )
        self._banner.pack(fill=tk.X, padx=2, pady=(3, 0))

        # The Text widget that holds all lyric lines. It's always in DISABLED
        # state for the user (no editing), but we temporarily enable it for
        # programmatic updates and disable again immediately after.
        self._text = tk.Text(
            self, bg=CRT_BG, fg=CRT_FG_DIM,
            font=("TkFixedFont", 9),
            relief=tk.FLAT, bd=0,
            highlightthickness=0,
            wrap=tk.WORD,
            cursor="arrow",
            state=tk.DISABLED,
            padx=4, pady=4,
            # A Text asks for 80x24 characters by default, which made
            # this terminal dictate the whole lower pane's proportions.
            # Asking for almost nothing lets the parent's grid weights
            # decide its size instead.
            width=1, height=1,
        )
        self._scrollbar = tk.Scrollbar(self, orient=tk.VERTICAL,
                                        command=self._text.yview,
                                        bg=CRT_BG, troughcolor=CRT_BG,
                                        activebackground="#005522")
        self._text.configure(yscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Tag definitions for dim (default), active line, and the banner msg
        self._text.tag_configure("dim",    foreground=CRT_FG_DIM)
        self._text.tag_configure("active", foreground=CRT_ACTIVE_FG,
                                  background=CRT_ACTIVE_BG,
                                  font=("TkFixedFont", 9, "bold"))
        self._text.tag_configure("notice", foreground=CRT_BANNER_FG,
                                  justify="center")

        self._lines      = []   # [(timestamp_ms, lyric_text), ...]
        self._active_idx = -1   # index of currently highlighted line

        self._show_message(self._MSG_WAITING)

    # ------------------------------------------------------------------
    # Internal helpers

    def _write(self, func):
        """Temporarily enable the Text widget, run *func*, then re-disable."""
        self._text.configure(state=tk.NORMAL)
        try:
            func()
        finally:
            self._text.configure(state=tk.DISABLED)

    def _show_message(self, msg):
        """Clear the widget and show a single centred notice message."""
        def _do():
            self._text.delete("1.0", tk.END)
            self._text.insert(tk.END, "\n\n" + msg + "\n", "notice")
        self._write(_do)
        self._lines = []
        self._active_idx = -1

    def _render_all_lines(self):
        """Re-draw all lyric lines in the dim style (called after load_track)."""
        def _do():
            self._text.delete("1.0", tk.END)
            for _ms, text in self._lines:
                self._text.insert(tk.END, text + "\n", "dim")
        self._write(_do)
        self._active_idx = -1

    # ------------------------------------------------------------------
    # Public API

    def load_track(self, mp3_path):
        """Attempt to load the matching .lrc file for *mp3_path*.

        If found and parseable, renders all lyric lines ready for sync.
        Otherwise shows the NO SYNCED LYRICS notice.
        """
        lrc_path = os.path.splitext(mp3_path)[0] + ".lrc"
        entries = parse_lrc(lrc_path)
        if entries:
            self._lines = entries
            self._render_all_lines()
        else:
            self._show_message(self._MSG_NO_LYRICS)

    def sync_to_ms(self, ms):
        """Highlight the lyric line active at position *ms* milliseconds.

        Does nothing if there are no lyrics loaded. Skips the redraw if
        the active line hasn't changed since the last call (cheap & safe
        to call every 250 ms from _tick).
        """
        if not self._lines:
            return

        # Binary-search backwards: find the last entry with timestamp <= ms
        lo, hi = 0, len(self._lines) - 1
        new_idx = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._lines[mid][0] <= ms:
                new_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if new_idx == self._active_idx:
            return  # nothing changed; skip the expensive Text update

        old_idx = self._active_idx
        self._active_idx = new_idx

        def _do():
            # Restore old active line to dim style
            if old_idx >= 0:
                line_num = old_idx + 1
                self._text.tag_remove("active",
                                       f"{line_num}.0", f"{line_num}.end")
                self._text.tag_add("dim",
                                    f"{line_num}.0", f"{line_num}.end")
            # Apply active highlight to new line
            line_num = new_idx + 1
            self._text.tag_remove("dim",
                                    f"{line_num}.0", f"{line_num}.end")
            self._text.tag_add("active",
                                f"{line_num}.0", f"{line_num}.end")
            # Scroll so the active line is visible (centred as best Tk can)
            self._text.see(f"{line_num}.0")

        self._write(_do)

    def show_stopped(self):
        """Show the 'waiting for track' notice when playback is stopped."""
        self._show_message(self._MSG_WAITING)

    def show_no_lyrics(self):
        """Explicitly show the 'no synced lyrics' notice."""
        self._show_message(self._MSG_NO_LYRICS)


# --------------------------------------------------------------------------
# Audio engine (libVLC wrapper)
# --------------------------------------------------------------------------

class AudioEngine:
    """Thin, defensive wrapper around libVLC.

    All VLC-specific knowledge lives here so the rest of the app only
    deals with simple method calls. VLC event callbacks fire on a
    libVLC-internal thread, so they never touch Tkinter directly --
    they just drop a message on a thread-safe queue that the main
    window drains on its own timer (see WaveStackApp._tick)."""

    def __init__(self):
        self._instance = vlc.Instance()
        self._player = self._instance.media_player_new()
        self._events = queue.Queue()
        manager = self._player.event_manager()
        manager.event_attach(vlc.EventType.MediaPlayerEndReached,
                              lambda e: self._events.put("ended"))
        manager.event_attach(vlc.EventType.MediaPlayerEncounteredError,
                              lambda e: self._events.put("error"))

    def poll_events(self):
        """Drain and return all pending events. Call only from the main
        (Tkinter) thread."""
        drained = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                break
        return drained

    def load(self, filepath):
        media = self._instance.media_new(filepath)
        self._player.set_media(media)
        media.release()

    def play(self):
        self._player.play()

    def pause(self):
        self._player.set_pause(1)

    def resume(self):
        self._player.set_pause(0)

    def stop(self):
        self._player.stop()

    def set_volume(self, percent):
        self._player.audio_set_volume(max(0, min(100, int(percent))))

    def seek_seconds(self, seconds):
        self._player.set_time(max(0, int(seconds * 1000)))

    def get_time_seconds(self):
        ms = self._player.get_time()
        return (ms / 1000.0) if ms and ms > 0 else 0.0

    def get_length_seconds(self):
        ms = self._player.get_length()
        return (ms / 1000.0) if ms and ms > 0 else 0.0

    def release(self):
        try:
            self._player.stop()
            self._player.release()
            self._instance.release()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Main application window
# --------------------------------------------------------------------------

class WaveStackApp(tk.Tk):
    def __init__(self):
        super().__init__(className="WaveStack")
        self.title(APP_TITLE)

        # Theme initialization (restores theme if previously saved in state.json)
        saved_state = self._load_state_file()
        if saved_state and saved_state.get("theme") in THEMES:
            self.current_theme_name = saved_state["theme"]
        else:
            self.current_theme_name = DEFAULT_THEME
        self.theme = THEMES[self.current_theme_name]
        self.current_theme = self.theme
        self.configure(bg=self.theme["bg"])
        self.geometry("860x580")
        self.minsize(720, 480)

        self.font_normal = get_retro_font(self, 9)
        self.font_bold = get_retro_font(self, 9, bold=True)
        self.font_title = get_retro_font(self, 14, bold=True)

        self.audio = AudioEngine()

        self.library_dir = None
        self.library_files = []
        self.play_queue = []
        self.history = []
        self.playback_mode = MODE_NORMAL
        self._shuffle_pool = []
        self.now_playing = None
        self.is_paused = False
        # Tracks the third playback state Stop puts you in, distinct from
        # "paused": a stopped track is loaded but not paused-mid-playback,
        # so Space/Play should restart it rather than try to un-pause it.
        self.is_stopped = True
        self._user_seeking = False
        self._known_length = 0.0
        self._tick_id = None

        # Support for resuming a saved position: once a restored track
        # is loaded, _tick() waits for libVLC to report a real duration
        # (proof the file is actually open and seekable) before seeking
        # and pausing, rather than guessing at a fixed delay.
        self._pending_resume_seconds = None
        self._pending_resume_ticks = 0
        self._last_autosave = 0.0

        # Library tree state. The Library listbox shows a collapsible
        # genre tree, so a listbox row index is NOT an index into
        # library_files: visual_row_map is the only valid way to turn a
        # row into something, as {row: ("folder", genre_key)} or
        # {row: ("track", path)}. library_files stays the flat list of
        # playable tracks that Next/Prev/Shuffle/Repeat operate on.
        self.visual_row_map = {}
        self.genre_cache = {}          # {mp3 filename: {"genre", "source"}}
        self.genre_expanded = {}       # {genre_key: bool}; missing = open
        self.tree_root_expanded = True
        self._tree_filter = ""         # lowercase search query, "" = none
        self._genre_cache_dirty = False

        # AUTO-SORT worker state (see run_autosort_worker)
        self._autosort_thread = None
        self._autosort_queue = None
        self._autosort_cancel = None
        self._autosort_poll_id = None
        # LRC sync state
        self.sync_enabled = False
        self.sync_thread = None
        self.cancel_sync = threading.Event()
        self._sync_events = queue.Queue()
        self.lrc_status_var = tk.StringVar(value="No internet access (Offline Mode)")

        self.status_var = tk.StringVar(value="Ready")
        self.elapsed_var = tk.StringVar(value="00:00")
        self.total_var = tk.StringVar(value="00:00")
        self.queue_count_var = tk.StringVar(value="Queue: 0")

        self._build_menu()
        self._build_widgets()
        self._bind_shortcuts()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick_id = self.after(250, self._tick)
        self._startup_load_id = self.after(200, self.load_default_library)

    # -- error safety net --------------------------------------------------

    def report_callback_exception(self, exc, val, tb):
        """Tkinter calls this whenever a widget callback raises an
        unhandled exception. The default behaviour just prints to
        stderr, which is invisible when launched from the app menu --
        so we also show a retro error dialog instead of silently
        misbehaving."""
        traceback.print_exception(exc, val, tb)
        try:
            self.show_retro_error("Unexpected Error", str(val))
        except Exception:
            pass

    # -- construction --------------------------------------------------

    def _build_menu(self):
        t = self.theme
        self.menubar = tk.Menu(self, bg=t["menu_bg"], fg=t["menu_fg"],
                               activebackground=t["menu_active_bg"],
                               activeforeground=t["menu_active_fg"],
                               font=self.font_normal, tearoff=0)

        self.file_menu = tk.Menu(self.menubar, tearoff=0, bg=t["menu_bg"],
                                 fg=t["menu_fg"],
                                 activebackground=t["menu_active_bg"],
                                 activeforeground=t["menu_active_fg"],
                                 font=self.font_normal)
        self.file_menu.add_command(label="Open Folder...",
                               command=self.on_open_folder,
                               accelerator="Ctrl+O")
        self.file_menu.add_command(label="Find in Library...",
                               command=self._on_focus_search_shortcut,
                               accelerator="Ctrl+F")
        self.file_menu.add_command(label="Toggle Dark/Light Mode",
                               command=self.toggle_theme,
                               accelerator="F10")
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.on_close,
                               accelerator="Ctrl+Q")
        self.menubar.add_cascade(label="File", menu=self.file_menu)

        self.playback_menu = tk.Menu(self.menubar, tearoff=0, bg=t["menu_bg"],
                                     fg=t["menu_fg"],
                                     activebackground=t["menu_active_bg"],
                                     activeforeground=t["menu_active_fg"],
                                     font=self.font_normal)
        playback_menu = self.playback_menu
        playback_menu.add_command(label="Play", command=self.on_play_clicked,
                                   accelerator="Space")
        playback_menu.add_command(label="Pause",
                                   command=self.on_pause_clicked,
                                   accelerator="Space")
        playback_menu.add_command(label="Stop", command=self.on_stop_clicked)
        playback_menu.add_separator()
        playback_menu.add_command(label="Next", command=self.on_next_clicked)
        playback_menu.add_command(label="Previous",
                                   command=self.on_prev_clicked)
        playback_menu.add_separator()
        playback_menu.add_command(label="Cycle Playback Mode",
                                   command=self.cycle_playback_mode)
        self.menubar.add_cascade(label="Playback", menu=playback_menu)

        self.help_menu = tk.Menu(self.menubar, tearoff=0, bg=t["menu_bg"],
                                 fg=t["menu_fg"],
                                 activebackground=t["menu_active_bg"],
                                 activeforeground=t["menu_active_fg"],
                                 font=self.font_normal)
        self.help_menu.add_command(label="Toggle Dark/Light Mode",
                               command=self.toggle_theme)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="About WaveStack...",
                               command=self.show_about)
        self.menubar.add_cascade(label="Help", menu=self.help_menu)

        self.config(menu=self.menubar)

    def _bind_shortcuts(self):
        self.bind_all("<Control-o>", lambda e: self.on_open_folder())
        self.bind_all("<Control-q>", lambda e: self.on_close())
        self.bind_all("<Control-f>", self._on_focus_search_shortcut)
        self.bind_all("<F10>", lambda e: self.toggle_theme())
        self.bind_all("<Escape>", self._on_global_escape)
        self.bind_class("Button", "<Escape>", self._on_global_escape)
        self.bind_class("Listbox", "<Escape>", self._on_global_escape)
        self.bind_class("Canvas", "<Escape>", self._on_global_escape)

        # Space = play/pause, everywhere. Button and Listbox both ship
        # with their OWN default <space> binding (invoke a focused
        # button; reselect a listbox's active row), which would
        # otherwise fire *in addition to* a plain bind_all handler
        # whenever one of those widgets happens to have focus -- e.g.
        # right after clicking a transport button, or after clicking a
        # track in the library. Overriding at the class level makes
        # space unambiguous no matter what last had focus.
        self.bind_class("Button", "<space>", self._on_spacebar)
        self.bind_class("Listbox", "<space>", self._on_spacebar)
        self.bind_all("<space>", self._on_spacebar)

    def _build_widgets(self):
        t = self.theme
        self.outer_frame = tk.Frame(self, bg=t["bg"], bd=2, relief=tk.RIDGE)
        self.outer_frame.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)

        # Header banner
        self.header_frame = tk.Frame(self.outer_frame, bg=t["bg"], bd=2, relief=tk.RAISED)
        self.header_frame.pack(fill=tk.X, padx=4, pady=(4, 2))
        self.title_label = tk.Label(self.header_frame, text="W A V E S T A C K",
                                    bg=t["bg"], fg=t["title_fg"],
                                    font=self.font_title)
        self.title_label.pack(side=tk.LEFT, padx=10, pady=6)

        # Theme toggle button in top right corner
        theme_icon = "☀" if self.current_theme_name == "light" else "🌙"
        self.theme_btn = tk.Button(
            self.header_frame, text=theme_icon,
            command=self.toggle_theme,
            font=get_retro_font(self, 12, bold=True),
            bg=t["btn_bg"], fg=t["btn_fg"],
            relief=tk.RAISED, bd=3,
            activebackground=t["btn_active_bg"],
            activeforeground=t["btn_active_fg"],
            padx=5, pady=0, cursor="hand2"
        )
        self.theme_btn.pack(side=tk.RIGHT, padx=(4, 8), pady=4)

        self.subtitle_label = tk.Label(self.header_frame, text="Offline MP3 Player",
                                       bg=t["bg"], fg=t["fg"],
                                       font=self.font_normal)
        self.subtitle_label.pack(side=tk.RIGHT, padx=10)

        # Now playing LCD marquee
        self.now_playing_display = NowPlayingDisplay(self.outer_frame)
        self.now_playing_display.apply_theme(t)
        self.now_playing_display.pack(fill=tk.X, padx=6, pady=6)

        # Transport controls
        self.transport_frame = tk.Frame(self.outer_frame, bg=t["bg"])
        self.transport_frame.pack(fill=tk.X, padx=6, pady=2)

        def make_button(parent, text, command):
            return tk.Button(parent, text=text, command=command,
                              font=self.font_bold, bg=t["btn_bg"], fg=t["btn_fg"],
                              relief=tk.RAISED, bd=3,
                              activebackground=t["btn_active_bg"],
                              activeforeground=t["btn_active_fg"],
                              padx=6)

        self.prev_btn = make_button(self.transport_frame, "\u25c4\u25c4 Prev",
                                     self.on_prev_clicked)
        self.play_btn = make_button(self.transport_frame, "\u25ba Play",
                                     self.on_play_clicked)
        self.pause_btn = make_button(self.transport_frame, "|| Pause",
                                      self.on_pause_clicked)
        self.stop_btn = make_button(self.transport_frame, "\u25a0 Stop",
                                     self.on_stop_clicked)
        self.next_btn = make_button(self.transport_frame, "Next \u25ba\u25ba",
                                     self.on_next_clicked)
        for b in (self.prev_btn, self.play_btn, self.pause_btn,
                  self.stop_btn, self.next_btn):
            b.pack(side=tk.LEFT, padx=3, pady=3)

        self.visualizer_btn = make_button(self.transport_frame, "Audio Visualizer",
                                          self.toggle_visualizer)
        self.visualizer_btn.pack(side=tk.LEFT, padx=(10, 3), pady=3)

        self.mode_btn = tk.Button(self.transport_frame,
                                  text=MODE_LABELS[self.playback_mode],
                                  command=self.cycle_playback_mode,
                                  width=14,
                                  font=self.font_bold,
                                  bg=t["btn_bg"], fg=t["btn_fg"],
                                  relief=tk.RAISED, bd=2,
                                  activebackground=t["btn_active_bg"],
                                  activeforeground=t["btn_active_fg"],
                                  padx=6)
        self.mode_btn.pack(side=tk.LEFT, padx=3, pady=3)

        self.open_folder_btn = make_button(self.transport_frame, "Open Folder...",
                                            self.on_open_folder)
        self.open_folder_btn.pack(side=tk.RIGHT, padx=3, pady=3)

        # Seek bar
        self.seek_frame = tk.Frame(self.outer_frame, bg=t["bg"])
        self.seek_frame.pack(fill=tk.X, padx=6, pady=(6, 2))
        self.elapsed_label = tk.Label(self.seek_frame, textvariable=self.elapsed_var,
                                      bg=t["bg"], fg=t["fg"],
                                      font=self.font_normal, width=6)
        self.elapsed_label.pack(side=tk.LEFT)
        self.seek_scale = tk.Scale(
            self.seek_frame, from_=0, to=100, orient=tk.HORIZONTAL,
            showvalue=False, bg=t["bg"], fg=t["fg"], troughcolor=t["scale_trough"],
            relief=tk.RAISED, bd=2, sliderrelief=tk.RAISED, highlightthickness=0,
            activebackground=t["btn_active_bg"],
            command=self._on_seek_scale_moved)
        self.seek_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.seek_scale.bind("<ButtonPress-1>", self._on_seek_press)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)
        self.total_label = tk.Label(self.seek_frame, textvariable=self.total_var,
                                    bg=t["bg"], fg=t["fg"],
                                    font=self.font_normal, width=6)
        self.total_label.pack(side=tk.LEFT)

        # Volume
        self.vol_frame = tk.Frame(self.outer_frame, bg=t["bg"])
        self.vol_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.vol_label = tk.Label(self.vol_frame, text="Volume", bg=t["bg"],
                                  fg=t["fg"], font=self.font_normal)
        self.vol_label.pack(side=tk.LEFT, padx=(0, 6))
        self.volume_scale = tk.Scale(
            self.vol_frame, from_=0, to=100, orient=tk.HORIZONTAL,
            showvalue=False, bg=t["bg"], fg=t["fg"], troughcolor=t["scale_trough"],
            relief=tk.RAISED, bd=2, sliderrelief=tk.RAISED, highlightthickness=0,
            activebackground=t["btn_active_bg"],
            command=self.on_volume_changed, length=160)
        self.volume_scale.set(70)
        self.volume_scale.pack(side=tk.LEFT)
        self.audio.set_volume(70)

        # Status bar
        self.status_bar = tk.Frame(self.outer_frame, bg=t["bg"], bd=2, relief=tk.SUNKEN)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=4, pady=(2, 4))
        self.status_label = tk.Label(self.status_bar, textvariable=self.lrc_status_var,
                                     bg=t["bg"], fg=t["fg"],
                                     font=self.font_normal, anchor="w")
        self.status_label.pack(side=tk.LEFT, padx=6, pady=2, fill=tk.X, expand=True)

        self.sync_btn = tk.Button(self.status_bar, text="[ Go Online ]", width=14,
                                  font=self.font_normal, bg=t["btn_bg"], fg=t["btn_fg"],
                                  activebackground=t["btn_active_bg"],
                                  activeforeground=t["btn_active_fg"],
                                  relief=tk.RAISED, bd=2, command=self.toggle_sync)
        self.sync_btn.pack(side=tk.RIGHT, padx=4, pady=2)

        # Library / Queue panes
        self.panes = tk.Frame(self.outer_frame, bg=t["bg"])
        self.panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        
        self.visualizer_showing = False
        self.visualizer = Visualizer(self.outer_frame)
        self.visualizer.set_volume(70)
        # Library (col 0) and Queue+Lyrics (col 2) share one "uniform"
        # group, which makes grid give them exactly equal widths; the
        # button column between them stays at its natural width. The
        # listboxes and the lyrics Text all request width=1 so that
        # those weights, not the widgets' default sizes, set the split.
        self.panes.columnconfigure(0, weight=1, uniform="side_pane")
        self.panes.columnconfigure(1, weight=0)
        self.panes.columnconfigure(2, weight=1, uniform="side_pane")
        self.panes.rowconfigure(0, weight=1)

        # -- Library pane --
        self.lib_frame = tk.Frame(self.panes, bg=t["bg"], bd=2, relief=tk.SUNKEN)
        self.lib_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.lib_header_label = tk.Label(self.lib_frame, text="Library",
                                         bg=t["bg"], fg=t["fg"],
                                         font=self.font_bold, anchor="w")
        self.lib_header_label.pack(fill=tk.X, padx=4, pady=(2, 0))

        self.search_frame = tk.Frame(self.lib_frame, bg=t["bg"])
        self.search_frame.pack(fill=tk.X, padx=4, pady=(2, 4))
        self.search_entry = tk.Entry(
            self.search_frame, font=self.font_normal, bg=t["entry_bg"],
            fg=t["search_placeholder_fg"], relief=tk.SUNKEN, bd=2,
            highlightthickness=0, insertbackground=t["entry_insert"])
        self.search_entry.insert(0, SEARCH_PLACEHOLDER_TEXT)
        self.search_entry.pack(fill=tk.X)
        self.search_entry.bind("<KeyPress>", self._on_search_keypress)
        self.search_entry.bind("<KeyRelease>", self._on_search_key_release)
        self.search_entry.bind("<FocusOut>", self._on_search_focus_out)
        self.search_entry.bind("<Return>", self._on_search_enter)
        self.search_entry.bind("<KP_Enter>", self._on_search_enter)
        self.search_entry.bind("<Escape>", self._on_search_escape)
        self.search_entry.bind("<Down>", self._on_search_arrow_down)
        self.search_entry.bind("<Up>", self._on_search_arrow_up)

        self.lib_list_frame = tk.Frame(self.lib_frame, bg=t["bg"])
        self.lib_list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.lib_scroll = tk.Scrollbar(self.lib_list_frame, orient=tk.VERTICAL)
        self.library_listbox = tk.Listbox(
            self.lib_list_frame, bg=t["entry_bg"], fg=t["entry_fg"], font=self.font_normal,
            width=1, height=1,
            relief=tk.SUNKEN, bd=2, selectbackground=t["select_bg"],
            selectforeground=t["select_fg"], activestyle="none",
            selectmode=tk.EXTENDED, exportselection=False,
            yscrollcommand=self.lib_scroll.set)
        self.lib_scroll.config(command=self.library_listbox.yview)
        self.library_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.lib_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.library_listbox.bind("<Button-1>", self._on_library_click)
        self.library_listbox.bind("<Double-Button-1>",
                                   self._on_library_double_click)
        self.library_listbox.bind("<Return>", self._on_library_return)
        self.library_listbox.bind("<KP_Enter>", self._on_library_return)
        self.library_listbox.bind("<Button-3>",
                                   self.show_library_context_menu)

        # -- Middle buttons --
        self.mid_frame = tk.Frame(self.panes, bg=t["bg"])
        self.mid_frame.grid(row=0, column=1, sticky="ns", padx=4)
        self.mid_inner = tk.Frame(self.mid_frame, bg=t["bg"])
        self.mid_inner.pack(expand=True)
        self.autosort_btn = tk.Button(self.mid_inner, text="AUTO-SORT",
                  command=self.on_autosort_clicked, font=self.font_normal,
                  bg=t["btn_bg"], fg=t["btn_fg"], relief=tk.RAISED, bd=3,
                  activebackground=t["btn_active_bg"],
                  activeforeground=t["btn_active_fg"])
        self.autosort_btn.pack(pady=3, fill=tk.X)
        self.enqueue_btn = tk.Button(self.mid_inner, text="Enqueue >>",
                  command=self.on_enqueue_clicked, font=self.font_normal,
                  bg=t["btn_bg"], fg=t["btn_fg"], relief=tk.RAISED, bd=3,
                  activebackground=t["btn_active_bg"],
                  activeforeground=t["btn_active_fg"])
        self.enqueue_btn.pack(pady=3, fill=tk.X)
        self.remove_btn = tk.Button(self.mid_inner, text="<< Remove",
                  command=self.on_remove_from_queue_clicked,
                  font=self.font_normal, bg=t["btn_bg"], fg=t["btn_fg"],
                  relief=tk.RAISED, bd=3,
                  activebackground=t["btn_active_bg"],
                  activeforeground=t["btn_active_fg"])
        self.remove_btn.pack(pady=3, fill=tk.X)
        self.clear_btn = tk.Button(self.mid_inner, text="Clear Queue",
                  command=self.on_clear_queue_clicked,
                  font=self.font_normal, bg=t["btn_bg"], fg=t["btn_fg"],
                  relief=tk.RAISED, bd=3,
                  activebackground=t["btn_active_bg"],
                  activeforeground=t["btn_active_fg"])
        self.clear_btn.pack(pady=3, fill=tk.X)

        self.vinyl = VinylDisc(self.mid_inner, theme=t)
        self.vinyl.pack(pady=(8, 2))


        # -- Right column: Queue (top) + CRT Lyrics Terminal (bottom) --
        # A dedicated container occupies column 2. The Queue keeps its
        # natural height (QUEUE_VISIBLE_ROWS rows) and the lyrics
        # terminal takes all the remaining height below it.
        self.right_col = tk.Frame(self.panes, bg=t["bg"])
        self.right_col.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        self.right_col.rowconfigure(0, weight=0)
        self.right_col.rowconfigure(1, weight=1, minsize=LYRICS_MIN_HEIGHT)
        self.right_col.columnconfigure(0, weight=1)

        # Queue sub-pane (top half)
        self.queue_frame = tk.Frame(self.right_col, bg=t["bg"], bd=2, relief=tk.SUNKEN)
        self.queue_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 2))
        self.queue_header = tk.Frame(self.queue_frame, bg=t["bg"])
        self.queue_header.pack(fill=tk.X, padx=4, pady=(2, 0))
        self.queue_title_label = tk.Label(self.queue_header, text="Queue",
                                          bg=t["bg"], fg=t["fg"],
                                          font=self.font_bold, anchor="w")
        self.queue_title_label.pack(side=tk.LEFT)
        self.queue_count_label = tk.Label(self.queue_header, textvariable=self.queue_count_var,
                                          bg=t["bg"], fg=t["fg"],
                                          font=self.font_normal)
        self.queue_count_label.pack(side=tk.RIGHT)
        self.queue_list_frame = tk.Frame(self.queue_frame, bg=t["bg"])
        self.queue_list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.queue_scroll = tk.Scrollbar(self.queue_list_frame, orient=tk.VERTICAL)
        self.queue_listbox = tk.Listbox(
            self.queue_list_frame, bg=t["entry_bg"], fg=t["entry_fg"], font=self.font_normal,
            width=1, height=QUEUE_VISIBLE_ROWS,
            relief=tk.SUNKEN, bd=2, selectbackground=t["select_bg"],
            selectforeground=t["select_fg"], activestyle="none",
            selectmode=tk.EXTENDED, exportselection=False,
            yscrollcommand=self.queue_scroll.set)
        self.queue_scroll.config(command=self.queue_listbox.yview)
        self.queue_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.queue_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.queue_listbox.bind("<Double-Button-1>", self.on_play_from_queue)
        self.queue_listbox.bind("<Button-3>", self.show_queue_context_menu)

        # CRT Lyrics Terminal sub-pane (bottom half)
        self.lyrics_display = CrtLyricsDisplay(self.right_col)
        self.lyrics_display.grid(row=1, column=0, sticky="nsew", pady=(2, 0))

    def toggle_visualizer(self):
        if self.visualizer_showing:
            self.visualizer.pack_forget()
            self.panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
            self.visualizer_showing = False
            self.visualizer_btn.config(relief=tk.RAISED)
            if hasattr(self, "library_listbox"):
                self.library_listbox.focus_set()
        else:
            self.panes.pack_forget()
            self.visualizer.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
            self.visualizer_showing = True
            self.visualizer_btn.config(relief=tk.SUNKEN)
            self.update_idletasks()
            self.visualizer._layout()
            self.visualizer.focus_set()
            self.visualizer.canvas.focus_set()
            self._sync_vinyl_spin_state()

    def _on_global_escape(self, event=None):
        if getattr(self, "visualizer_showing", False):
            self.toggle_visualizer()
            return "break"

    def toggle_theme(self):
        new_theme = "dark" if self.current_theme_name == "light" else "light"
        self.apply_theme(new_theme)
        self.save_state()
        self.status_var.set(f"Theme switched to {new_theme.capitalize()} Mode")

    def apply_theme(self, theme_name):
        if theme_name not in THEMES:
            return
        self.current_theme_name = theme_name
        self.theme = THEMES[theme_name]
        self.current_theme = self.theme
        t = self.theme

        # Root window
        self.configure(bg=t["bg"])

        # Menus
        for m in (getattr(self, "menubar", None),
                  getattr(self, "file_menu", None),
                  getattr(self, "playback_menu", None),
                  getattr(self, "help_menu", None)):
            if m:
                try:
                    m.configure(bg=t["menu_bg"], fg=t["menu_fg"],
                                activebackground=t["menu_active_bg"],
                                activeforeground=t["menu_active_fg"])
                except Exception:
                    pass

        # Outer & Header
        if hasattr(self, "outer_frame"):
            self.outer_frame.configure(bg=t["bg"])
        if hasattr(self, "header_frame"):
            self.header_frame.configure(bg=t["bg"])
        if hasattr(self, "title_label"):
            self.title_label.configure(bg=t["bg"], fg=t["title_fg"])
        if hasattr(self, "subtitle_label"):
            self.subtitle_label.configure(bg=t["bg"], fg=t["fg"])

        # Theme toggle button
        if hasattr(self, "theme_btn"):
            icon = "☀" if self.current_theme_name == "light" else "🌙"
            self.theme_btn.configure(
                text=icon,
                bg=t["btn_bg"], fg=t["btn_fg"],
                activebackground=t["btn_active_bg"],
                activeforeground=t["btn_active_fg"]
            )

        # LCD marquee
        if hasattr(self, "now_playing_display"):
            self.now_playing_display.apply_theme(t)

        # Transport
        if hasattr(self, "transport_frame"):
            self.transport_frame.configure(bg=t["bg"])
        for btn in (getattr(self, "prev_btn", None),
                    getattr(self, "play_btn", None),
                    getattr(self, "pause_btn", None),
                    getattr(self, "stop_btn", None),
                    getattr(self, "next_btn", None),
                    getattr(self, "visualizer_btn", None),
                    getattr(self, "mode_btn", None),
                    getattr(self, "open_folder_btn", None)):
            if btn:
                btn.configure(
                    bg=t["btn_bg"], fg=t["btn_fg"],
                    activebackground=t["btn_active_bg"],
                    activeforeground=t["btn_active_fg"]
                )

        # Seek
        if hasattr(self, "seek_frame"):
            self.seek_frame.configure(bg=t["bg"])
        if hasattr(self, "elapsed_label"):
            self.elapsed_label.configure(bg=t["bg"], fg=t["fg"])
        if hasattr(self, "total_label"):
            self.total_label.configure(bg=t["bg"], fg=t["fg"])
        if hasattr(self, "seek_scale"):
            self.seek_scale.configure(
                bg=t["bg"], fg=t["fg"],
                troughcolor=t["scale_trough"],
                activebackground=t["btn_active_bg"]
            )

        # Volume
        if hasattr(self, "vol_frame"):
            self.vol_frame.configure(bg=t["bg"])
        if hasattr(self, "vol_label"):
            self.vol_label.configure(bg=t["bg"], fg=t["fg"])
        if hasattr(self, "volume_scale"):
            self.volume_scale.configure(
                bg=t["bg"], fg=t["fg"],
                troughcolor=t["scale_trough"],
                activebackground=t["btn_active_bg"]
            )

        # Panes & Library
        if hasattr(self, "panes"):
            self.panes.configure(bg=t["bg"])
        if hasattr(self, "lib_frame"):
            self.lib_frame.configure(bg=t["bg"])
        if hasattr(self, "lib_header_label"):
            self.lib_header_label.configure(bg=t["bg"], fg=t["fg"])
        if hasattr(self, "search_frame"):
            self.search_frame.configure(bg=t["bg"])
        if hasattr(self, "search_entry"):
            is_placeholder = self._search_showing_placeholder()
            self.search_entry.configure(
                bg=t["entry_bg"],
                fg=t["search_placeholder_fg"] if is_placeholder else t["search_normal_fg"],
                insertbackground=t["entry_insert"]
            )
        if hasattr(self, "lib_list_frame"):
            self.lib_list_frame.configure(bg=t["bg"])
        if hasattr(self, "lib_scroll"):
            self.lib_scroll.configure(
                bg=t["btn_bg"], troughcolor=t["scale_trough"],
                activebackground=t["btn_active_bg"]
            )
        if hasattr(self, "library_listbox"):
            self.library_listbox.configure(
                bg=t["entry_bg"], fg=t["entry_fg"],
                selectbackground=t["select_bg"],
                selectforeground=t["select_fg"]
            )
            self._highlight_now_playing()

        # Middle buttons
        if hasattr(self, "mid_frame"):
            self.mid_frame.configure(bg=t["bg"])
        if hasattr(self, "mid_inner"):
            self.mid_inner.configure(bg=t["bg"])
        for btn in (getattr(self, "autosort_btn", None),
                    getattr(self, "enqueue_btn", None),
                    getattr(self, "remove_btn", None),
                    getattr(self, "clear_btn", None)):
            if btn:
                btn.configure(
                    bg=t["btn_bg"], fg=t["btn_fg"],
                    activebackground=t["btn_active_bg"],
                    activeforeground=t["btn_active_fg"]
                )

        # Vinyl widget
        if hasattr(self, "vinyl"):
            self.vinyl.apply_theme(t)

        # Visualizer widget
        if hasattr(self, "visualizer"):
            self.visualizer.apply_theme(t)

        # Queue
        if hasattr(self, "right_col"):
            self.right_col.configure(bg=t["bg"])
        if hasattr(self, "queue_frame"):
            self.queue_frame.configure(bg=t["bg"])
        if hasattr(self, "queue_header"):
            self.queue_header.configure(bg=t["bg"])
        if hasattr(self, "queue_title_label"):
            self.queue_title_label.configure(bg=t["bg"], fg=t["fg"])
        if hasattr(self, "queue_count_label"):
            self.queue_count_label.configure(bg=t["bg"], fg=t["fg"])
        if hasattr(self, "queue_list_frame"):
            self.queue_list_frame.configure(bg=t["bg"])
        if hasattr(self, "queue_scroll"):
            self.queue_scroll.configure(
                bg=t["btn_bg"], troughcolor=t["scale_trough"],
                activebackground=t["btn_active_bg"]
            )
        if hasattr(self, "queue_listbox"):
            self.queue_listbox.configure(
                bg=t["entry_bg"], fg=t["entry_fg"],
                selectbackground=t["select_bg"],
                selectforeground=t["select_fg"]
            )

        # Status bar
        if hasattr(self, "status_bar"):
            self.status_bar.configure(bg=t["bg"])
        if hasattr(self, "status_label"):
            self.status_label.configure(bg=t["bg"], fg=t["fg"])

    # -- library / folder handling --------------------------------------

    def load_default_library(self):
        if not self.load_folder(DEFAULT_MUSIC_DIR, show_errors=False):
            self.show_startup_missing_dialog()
        self.restore_saved_state()

    def load_folder(self, directory, show_errors=True):
        files = scan_mp3_files(directory)
        if not files:
            if show_errors:
                self.show_retro_error(
                    "No MP3 Files Found",
                    f"This folder has no .mp3 files, or doesn't exist:\n\n"
                    f"{directory}")
            return False
        # A sort still running against the previous folder is stopped
        # (and its results saved there) before switching.
        self._cancel_autosort()
        self._save_genre_cache_if_dirty()
        self.library_dir = directory
        self.library_files = files
        self._shuffle_pool = []
        self.genre_cache = load_genre_cache(directory)
        self.genre_expanded = {}
        self.tree_root_expanded = True
        self.refresh_library_listbox()
        self.status_var.set(f"Loaded {len(files)} track(s) from {directory}")
        return True

    # -- library tree ---------------------------------------------------

    def _genre_of(self, path):
        record = self.genre_cache.get(os.path.basename(path))
        return record["genre"] if record else GENRE_UNSORTED

    def _group_tracks_by_genre(self):
        """Return [(genre_key, [paths])] in display order -- core genres
        first, then any other tagged genres alphabetically, with the
        Unsorted folder last -- honouring the current search filter.
        Empty folders are left out."""
        query = self._tree_filter
        groups = {}
        for path in self.library_files:
            if query:
                name = os.path.splitext(os.path.basename(path))[0].lower()
                if query not in name:
                    continue
            groups.setdefault(self._genre_of(path), []).append(path)
        ordered = [g for g in GENRE_ORDER if g in groups]
        ordered += sorted((g for g in groups
                           if g not in GENRE_ORDER and g != GENRE_UNSORTED),
                          key=str.lower)
        if GENRE_UNSORTED in groups:
            ordered.append(GENRE_UNSORTED)
        return [(g, groups[g]) for g in ordered]

    def refresh_library_listbox(self):
        """Rebuild the Library tree and visual_row_map from scratch.

        Safe to call at any time (it's what AUTO-SORT does as results
        trickle in): the current selection and scroll position are
        carried across the rebuild by identity, not by row number."""
        lb = self.library_listbox
        selected = {self.visual_row_map.get(i) for i in lb.curselection()}
        top_entry = (self.visual_row_map.get(lb.nearest(0))
                     if lb.size() else None)

        filtering = bool(self._tree_filter)
        groups = self._group_tracks_by_genre()
        root_open = self.tree_root_expanded or filtering
        labels = [f"[{'-' if root_open else '+'}] {TREE_ROOT_LABEL}"]
        entries = [("folder", TREE_ROOT_KEY)]
        if root_open:
            for g_idx, (genre, paths) in enumerate(groups):
                last_genre = g_idx == len(groups) - 1
                # While a search filter is active every matching folder
                # is shown open, without disturbing the remembered
                # open/closed state that comes back when it's cleared.
                is_open = filtering or self.genre_expanded.get(genre, True)
                labels.append(
                    f" {'└──' if last_genre else '├──'} "
                    f"[{'-' if is_open else '+'}] {genre} ({len(paths)})")
                entries.append(("folder", genre))
                if not is_open:
                    continue
                stem = "     " if last_genre else " │   "
                for t_idx, path in enumerate(paths):
                    branch = "└──" if t_idx == len(paths) - 1 else "├──"
                    name = os.path.splitext(os.path.basename(path))[0]
                    labels.append(f"{stem}{branch} {name}")
                    entries.append(("track", path))
            if filtering and not groups:
                labels.append(" └── (no matches)")
                entries.append(("note", None))

        lb.delete(0, tk.END)
        if labels:
            lb.insert(tk.END, *labels)
        self.visual_row_map = dict(enumerate(entries))

        selected.discard(None)
        for row, entry in self.visual_row_map.items():
            if entry in selected:
                lb.selection_set(row)
        top_row = self._row_for_entry(top_entry) if top_entry else None
        if top_row is not None:
            lb.yview(top_row)
        self._highlight_now_playing()

    def _row_for_entry(self, entry):
        for row, candidate in self.visual_row_map.items():
            if candidate == entry:
                return row
        return None

    def _track_rows(self):
        return [row for row, (kind, _key) in sorted(self.visual_row_map.items())
                if kind == "track"]

    def _selected_track_paths(self):
        """The playable tracks in the current Library selection, in
        display order. Folder header rows are skipped."""
        paths = []
        for row in self.library_listbox.curselection():
            kind, key = self.visual_row_map.get(row, (None, None))
            if kind == "track":
                paths.append(key)
        return paths

    def _select_library_row(self, row):
        self.library_listbox.selection_clear(0, tk.END)
        self.library_listbox.selection_set(row)
        self.library_listbox.activate(row)
        self.library_listbox.see(row)

    def _toggle_tree_folder(self, key):
        if self._tree_filter:
            return  # filtered view is always fully expanded
        if key == TREE_ROOT_KEY:
            self.tree_root_expanded = not self.tree_root_expanded
        else:
            self.genre_expanded[key] = not self.genre_expanded.get(key, True)
        self.refresh_library_listbox()
        row = self._row_for_entry(("folder", key))
        if row is not None:
            self._select_library_row(row)

    def _library_row_at(self, event):
        """The row actually under the pointer, or None for a click in
        the empty space below the last row (where Listbox.nearest()
        would otherwise report the last row)."""
        lb = self.library_listbox
        if not lb.size():
            return None
        row = lb.nearest(event.y)
        bbox = lb.bbox(row)
        if not bbox or event.y > bbox[1] + bbox[3]:
            return None
        return row

    def _on_library_click(self, event):
        # A plain click on a folder header toggles it. Everything else
        # (track rows, Ctrl/Shift extend-clicks) falls through to the
        # Listbox's own selection behaviour.
        if event.state & 0x0005:   # Shift or Control held
            return
        kind, key = self.visual_row_map.get(self._library_row_at(event),
                                            (None, None))
        if kind != "folder":
            return
        self.library_listbox.focus_set()
        self._toggle_tree_folder(key)
        return "break"

    def _on_library_double_click(self, event):
        # Tk delivers the second press of a double-click here INSTEAD of
        # to <Button-1>, so a double-clicked folder has been toggled
        # exactly once (by its first press) and needs nothing more.
        kind, key = self.visual_row_map.get(self._library_row_at(event),
                                            (None, None))
        if kind == "track":
            self._play_path(key)
        return "break"

    def _on_library_return(self, event=None):
        lb = self.library_listbox
        if not lb.size():
            return "break"
        kind, key = self.visual_row_map.get(lb.index(tk.ACTIVE), (None, None))
        if kind == "folder":
            self._toggle_tree_folder(key)
        elif kind == "track":
            self._play_path(key)
        return "break"

    # -- search ---------------------------------------------------
    #
    # Typing in the search entry filters the Library tree in place:
    # only matching tracks are shown, with their genre folders forced
    # open. Up/Down move through the matches, Enter picks one, and
    # clearing the entry (or Escape) restores the normal tree.

    def _on_focus_search_shortcut(self, event=None):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, tk.END)
        self.search_entry.icursor(tk.END)
        return "break"

    def _search_showing_placeholder(self):
        t = getattr(self, "theme", THEMES[DEFAULT_THEME])
        return self.search_entry.get() == SEARCH_PLACEHOLDER_TEXT or self.search_entry.cget("fg") == t["search_placeholder_fg"]

    def _clear_search_to_placeholder(self):
        t = getattr(self, "theme", THEMES[DEFAULT_THEME])
        self.search_entry.delete(0, tk.END)
        self.search_entry.insert(0, SEARCH_PLACEHOLDER_TEXT)
        self.search_entry.config(fg=t["search_placeholder_fg"])
        self._set_tree_filter("")

    def _on_search_keypress(self, event):
        # Fires before the character is actually inserted. If the
        # placeholder is showing, clear it first so the keystroke
        # lands in an empty field instead of appending to (or getting
        # lost inside) "search song". Safe to do for every key,
        # including Backspace/Delete/arrows -- clearing an
        # already-empty field is a harmless no-op.
        if self._search_showing_placeholder():
            t = getattr(self, "theme", THEMES[DEFAULT_THEME])
            self.search_entry.delete(0, tk.END)
            self.search_entry.config(fg=t["search_normal_fg"])

    def _on_search_key_release(self, event):
        if event.keysym in _SEARCH_NON_TEXT_KEYSYMS:
            return  # these don't change the query; avoid pointless rebuilds
        query = "" if self._search_showing_placeholder() \
            else self.search_entry.get()
        self._set_tree_filter(query)

    def _set_tree_filter(self, query):
        query = (query or "").strip().lower()
        if query == self._tree_filter:
            return
        self._tree_filter = query
        self.refresh_library_listbox()
        if query:
            # Pre-select the first match so Enter works straight away.
            rows = self._track_rows()
            self.library_listbox.yview(0)
            if rows:
                self.library_listbox.selection_clear(0, tk.END)
                self.library_listbox.selection_set(rows[0])
                self.library_listbox.activate(rows[0])

    def _on_search_focus_out(self, event=None):
        # An emptied entry gets its placeholder back. A non-empty one
        # is left alone, so the tree stays filtered while the user
        # clicks around in the results.
        if not self.search_entry.get():
            t = getattr(self, "theme", THEMES[DEFAULT_THEME])
            self.search_entry.config(fg=t["search_placeholder_fg"])
            self.search_entry.insert(0, SEARCH_PLACEHOLDER_TEXT)

    def _on_search_enter(self, event=None):
        if not self._tree_filter:
            return "break"
        paths = self._selected_track_paths()
        if not paths:
            rows = self._track_rows()
            if not rows:
                return "break"
            paths = [self.visual_row_map[rows[0]][1]]
        self._clear_search_to_placeholder()
        self._select_track_in_library(paths[0])
        self.library_listbox.focus_set()
        return "break"

    def _on_search_escape(self, event=None):
        self._clear_search_to_placeholder()
        self.library_listbox.focus_set()
        return "break"

    def _on_search_arrow_down(self, event=None):
        self._move_search_selection(1)
        return "break"

    def _on_search_arrow_up(self, event=None):
        self._move_search_selection(-1)
        return "break"

    def _move_search_selection(self, delta):
        """Move the Library selection between track rows (skipping
        folder headers) while focus stays in the search entry."""
        rows = self._track_rows()
        if not rows:
            return
        selected = [r for r in self.library_listbox.curselection()
                    if r in rows]
        if selected:
            pos = rows.index(selected[0]) + delta
            pos = max(0, min(len(rows) - 1, pos))
        else:
            pos = 0
        self._select_library_row(rows[pos])

    def _select_track_in_library(self, path):
        """Select *path*'s row, opening its folder first if needed."""
        if path not in self.library_files:
            return
        if self._row_for_entry(("track", path)) is None:
            self.tree_root_expanded = True
            self.genre_expanded[self._genre_of(path)] = True
            self.refresh_library_listbox()
        row = self._row_for_entry(("track", path))
        if row is not None:
            self._select_library_row(row)

    # -- AUTO-SORT ---------------------------------------------------

    def _autosort_running(self):
        return (self._autosort_thread is not None
                and self._autosort_thread.is_alive())

    def _set_autosort_status(self, text):
        self.status_var.set(text)
        self.lrc_status_var.set(text)

    def on_autosort_clicked(self):
        """Start classifying every track that has no genre yet, on a
        background thread -- or stop the run if one is in progress."""
        if self._autosort_queue is not None:
            self._cancel_autosort()
            self._set_autosort_status("Auto-Sort stopped.")
            return
        if not self.library_files:
            self._set_autosort_status(
                "No tracks loaded. Use File > Open Folder.")
            return
        known = {}
        for path in self.library_files:
            record = self.genre_cache.get(os.path.basename(path))
            if record:
                known[path] = record["genre"]
        if len(known) == len(self.library_files):
            self._set_autosort_status(
                f"Auto-Sort: all {len(known)} tracks already sorted.")
            return

        self._autosort_cancel = threading.Event()
        self._autosort_queue = queue.Queue()
        self._autosort_thread = threading.Thread(
            target=run_autosort_worker,
            args=(list(self.library_files), known, self._autosort_queue,
                  self._autosort_cancel),
            daemon=True)
        self._autosort_thread.start()
        self.autosort_btn.config(text="STOP SORT", relief=tk.SUNKEN)
        self._set_autosort_status("Auto-Sorting Library...")
        self._autosort_poll_id = self.after(AUTOSORT_POLL_MS,
                                            self._poll_autosort)

    def _drain_autosort_queue(self):
        """Apply everything the worker has reported so far. Returns the
        worker's final stats dict once it has finished, else None.
        Main thread only."""
        finished = None
        changed = False
        while True:
            try:
                message = self._autosort_queue.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == "genre":
                _kind, path, genre, source = message
                self.genre_cache[os.path.basename(path)] = {
                    "genre": genre, "source": source}
                changed = True
            elif kind == "progress":
                _kind, stage, done, total = message
                self._set_autosort_status(
                    f"Auto-Sorting Library [{stage}]: "
                    f"{done}/{total} tracks...")
            elif kind == "done":
                finished = message[1]
        if changed:
            self._genre_cache_dirty = True
            self.refresh_library_listbox()
        return finished

    def _poll_autosort(self):
        self._autosort_poll_id = None
        if self._autosort_queue is None:
            return
        stats = self._drain_autosort_queue()
        if stats is None:
            self._autosort_poll_id = self.after(AUTOSORT_POLL_MS,
                                                self._poll_autosort)
            return
        self._reset_autosort_state()
        self._save_genre_cache_if_dirty()
        sorted_now = stats["id3"] + stats["tokens"] + stats["dsp"]
        summary = (f"Auto-Sort complete: {sorted_now} sorted "
                   f"(ID3 {stats['id3']}, tokens {stats['tokens']}, "
                   f"DSP {stats['dsp']}), {stats['unsorted']} unsorted.")
        if stats.get("note"):
            summary += f" [{stats['note']}]"
        self._set_autosort_status(summary)

    def _cancel_autosort(self):
        """Stop a running sort, keeping whatever it had classified."""
        if self._autosort_queue is None:
            return
        self._autosort_cancel.set()
        self._drain_autosort_queue()
        self._reset_autosort_state()
        self._save_genre_cache_if_dirty()

    def _reset_autosort_state(self):
        if self._autosort_poll_id is not None:
            try:
                self.after_cancel(self._autosort_poll_id)
            except Exception:
                pass
            self._autosort_poll_id = None
        self._autosort_queue = None
        self._autosort_thread = None
        self._autosort_cancel = None
        if hasattr(self, "autosort_btn"):
            self.autosort_btn.config(text="AUTO-SORT", relief=tk.RAISED)

    def _save_genre_cache_if_dirty(self):
        if self._genre_cache_dirty and self.library_dir:
            if save_genre_cache(self.library_dir, self.genre_cache):
                self._genre_cache_dirty = False

    def show_startup_missing_dialog(self):
        message = (
            f"The default music folder was not found, or has no MP3 "
            f"files in it:\n\n{DEFAULT_MUSIC_DIR}\n\n"
            f"You can browse for a different folder now, or later from "
            f"File > Open Folder.")
        dialog = RetroDialog(
            self, "WaveStack - Folder Not Found", message, kind="warning",
            buttons=[("Browse...", "browse"), ("Later", "later")])
        if dialog.result == "browse":
            self.on_open_folder()

    def on_open_folder(self):
        chosen = filedialog.askdirectory(
            title="Select Music Folder",
            initialdir=self.library_dir or os.path.expanduser("~"))
        if chosen:
            self.load_folder(chosen, show_errors=True)

    # -- playback control -------------------------------------------------

    def on_play_pause_toggle(self):
        """Spacebar handler: pause if something is audibly playing right
        now, otherwise play/resume -- covers all three states (playing,
        paused, stopped-or-nothing-loaded) with one press."""
        if self.now_playing and not self.is_paused and not self.is_stopped:
            self.on_pause_clicked()
        else:
            self.on_play_clicked()

    def _on_spacebar(self, event=None):
        # An Entry widget (the search bar) inserts space as a normal
        # character through its own class binding, which runs before
        # this bind_all handler -- so by the time we get here the
        # space has already been typed. We just need to avoid ALSO
        # toggling playback as an unwanted side effect of searching.
        if isinstance(self.focus_get(), tk.Entry):
            return
        self.on_play_pause_toggle()
        return "break"

    def _sync_vinyl_spin_state(self):
        """Drives both the vinyl widget and the simulated audio visualizer
        from the same three-state model (playing / paused / stopped-or-nothing)
        used everywhere else in the app."""
        is_playing = bool(self.now_playing and not self.is_paused and not self.is_stopped)
        is_paused = bool(self.now_playing and self.is_paused)

        if hasattr(self, "vinyl"):
            if is_playing:
                self.vinyl.start_spinning()
            elif is_paused:
                self.vinyl.pause_spinning()
            else:
                self.vinyl.stop_and_reset()

        if hasattr(self, "visualizer"):
            if is_playing:
                self.visualizer.start_animating()
            elif is_paused:
                self.visualizer.pause_animating()
            else:
                self.visualizer.stop_and_reset()

    _sync_playback_visuals = _sync_vinyl_spin_state

    def on_play_clicked(self):
        if self.is_paused and self.now_playing:
            self.audio.resume()
            self.is_paused = False
            self.is_stopped = False
            self.status_var.set(
                f"Playing: {os.path.basename(self.now_playing)}")
            self._sync_vinyl_spin_state()
            return
        selected = self._selected_track_paths()
        if selected:
            self._play_path(selected[0])
        elif self.now_playing:
            self._play_path(self.now_playing)
        elif self.library_files:
            self._play_path(self.library_files[0])
        else:
            self.status_var.set("No tracks loaded. Use File > Open Folder.")

    def on_pause_clicked(self):
        if self.now_playing and not self.is_paused:
            self.audio.pause()
            self.is_paused = True
            self.status_var.set(
                f"Paused: {os.path.basename(self.now_playing)}")
            self._sync_vinyl_spin_state()

    def on_stop_clicked(self):
        self.audio.stop()
        self.is_paused = False
        self._set_stopped_state()

    def _set_stopped_state(self):
        self.is_stopped = True
        self.seek_scale.set(0)
        self.elapsed_var.set("00:00")
        self.status_var.set("Stopped")
        self._sync_vinyl_spin_state()
        if hasattr(self, "lyrics_display"):
            self.lyrics_display.show_stopped()

    def cycle_playback_mode(self):
        self.playback_mode = (self.playback_mode + 1) % 4
        if hasattr(self, "mode_btn"):
            self.mode_btn.config(text=MODE_LABELS[self.playback_mode])
        self.status_var.set(f"Playback mode: {MODE_NAMES[self.playback_mode]}")
        if self.playback_mode == MODE_SHUFFLE:
            self._reset_shuffle_pool()

    def _reset_shuffle_pool(self):
        if not self.library_files:
            self._shuffle_pool = []
            return
        if len(self.library_files) > 1 and self.now_playing in self.library_files:
            self._shuffle_pool = [p for p in self.library_files if p != self.now_playing]
        else:
            self._shuffle_pool = list(self.library_files)

    def _next_shuffle_track(self):
        if not self.library_files:
            return None
        self._shuffle_pool = [p for p in self._shuffle_pool if p in self.library_files]
        if not self._shuffle_pool:
            self._reset_shuffle_pool()
        if not self._shuffle_pool:
            return None
        chosen = random.choice(self._shuffle_pool)
        self._shuffle_pool.remove(chosen)
        return chosen

    def on_next_clicked(self):
        self._advance(auto=False)

    def on_prev_clicked(self):
        if self.history:
            path = self.history.pop()
            self._play_path(path)
        else:
            if self.library_files and self.now_playing in self.library_files:
                idx = self.library_files.index(self.now_playing)
                if idx > 0:
                    self._play_path(self.library_files[idx - 1])
                    return
                elif self.playback_mode == MODE_REPEAT_ALL:
                    self._play_path(self.library_files[-1])
                    return
            self.status_var.set("No previous track.")

    def _advance(self, auto=False, force_next=False):
        # Repeat One: replay the exact same track when reaching end,
        # unless forced to skip on error or triggered manually by Next.
        if auto and not force_next and self.playback_mode == MODE_REPEAT_ONE:
            if self.now_playing and os.path.isfile(self.now_playing):
                self._play_path(self.now_playing)
                return

        # Pick next track based on mode and queue
        if self.playback_mode == MODE_SHUFFLE:
            if self.play_queue:
                idx = random.randrange(len(self.play_queue))
                next_path = self.play_queue.pop(idx)
                self.refresh_queue_listbox()
            else:
                next_path = self._next_shuffle_track()
        else:
            if self.play_queue:
                next_path = self.play_queue.pop(0)
                self.refresh_queue_listbox()
            else:
                next_path = self._next_in_library()

        if next_path:
            self._play_path(next_path)
        else:
            if not auto:
                self.status_var.set("No more tracks.")
            self._set_stopped_state()

    def _next_in_library(self):
        if not self.library_files:
            return None
        if self.now_playing in self.library_files:
            idx = self.library_files.index(self.now_playing)
            if idx + 1 < len(self.library_files):
                return self.library_files[idx + 1]
            if self.playback_mode == MODE_REPEAT_ALL:
                return self.library_files[0]
            return None
        return self.library_files[0]

    def _play_path(self, path, resume_position=None):
        """Load and play *path*. If resume_position is given (seconds),
        playback starts normally and then, once _tick() confirms the
        file is actually open and seekable, jumps to that position and
        pauses there -- used to restore a saved song without an
        audible moment of playing from 0:00 first."""
        if not os.path.isfile(path):
            self.show_retro_error("File Not Found",
                                   f"This file no longer exists:\n{path}")
            self._advance(auto=True, force_next=True)
            return
        if self.now_playing and self.now_playing != path:
            self.history.append(self.now_playing)
        try:
            self.audio.load(path)
            self.audio.play()
        except Exception as exc:
            self.show_retro_error(
                "Playback Error",
                f"Could not play:\n{os.path.basename(path)}\n\n{exc}")
            self._advance(auto=True, force_next=True)
            return
        self.now_playing = path
        self.is_paused = False
        self.is_stopped = False
        self._known_length = 0.0
        self.seek_scale.config(to=100)
        self.seek_scale.set(0)
        self.elapsed_var.set("00:00")
        if self._shuffle_pool and path in self._shuffle_pool:
            self._shuffle_pool.remove(path)
        self.now_playing_display.set_text(
            os.path.splitext(os.path.basename(path))[0])
        self.status_var.set(f"Playing: {os.path.basename(path)}")
        self._highlight_now_playing()
        if hasattr(self, "vinyl"):
            self.vinyl.load_track(path)  # extracts/caches art once per track
        if hasattr(self, "lyrics_display"):
            self.lyrics_display.load_track(path)  # finds & parses .lrc once per track
        self._sync_vinyl_spin_state()

        if resume_position and resume_position > 0:
            self._pending_resume_seconds = resume_position
            self._pending_resume_ticks = 0
        else:
            self._pending_resume_seconds = None

    def _highlight_now_playing(self):
        t = getattr(self, "theme", THEMES[DEFAULT_THEME])
        for row, (kind, key) in self.visual_row_map.items():
            if kind == "track" and key == self.now_playing:
                self.library_listbox.itemconfig(row, bg=t["select_bg"],
                                                 fg=t["select_fg"])
            elif kind == "track":
                self.library_listbox.itemconfig(row, bg=t["entry_bg"],
                                                 fg=t["entry_fg"])
            else:
                # folder headers get the title colour, like a caption
                self.library_listbox.itemconfig(row, bg=t["entry_bg"],
                                                 fg=t["title_fg"])

    # -- queue management ---------------------------------------------------

    def on_enqueue_clicked(self):
        paths = self._selected_track_paths()
        if not paths:
            self.status_var.set("Select a track in Library first.")
            return
        self.play_queue.extend(paths)
        self.refresh_queue_listbox()
        self.status_var.set(f"Added {len(paths)} track(s) to the queue.")

    def on_remove_from_queue_clicked(self):
        sel = list(self.queue_listbox.curselection())
        if not sel:
            self.status_var.set("Select a track in Queue first.")
            return
        for i in reversed(sel):
            del self.play_queue[i]
        self.refresh_queue_listbox()
        self.status_var.set("Removed from queue.")

    def on_clear_queue_clicked(self):
        self.play_queue.clear()
        self.refresh_queue_listbox()

    def refresh_queue_listbox(self):
        self.queue_listbox.delete(0, tk.END)
        for path in self.play_queue:
            name = os.path.splitext(os.path.basename(path))[0]
            self.queue_listbox.insert(tk.END, name)
        self.queue_count_var.set(f"Queue: {len(self.play_queue)}")

    def on_play_from_queue(self, event):
        sel = self.queue_listbox.curselection()
        if sel:
            self._play_from_queue_index(sel[0])

    def _play_from_queue_index(self, idx):
        if 0 <= idx < len(self.play_queue):
            path = self.play_queue.pop(idx)
            self.refresh_queue_listbox()
            self._play_path(path)

    # -- context menus ---------------------------------------------------

    def show_library_context_menu(self, event):
        if not self.library_files:
            return
        idx = self._library_row_at(event)
        kind, path = self.visual_row_map.get(idx, (None, None))
        if kind != "track":
            return  # folder headers have no track actions
        self.library_listbox.selection_clear(0, tk.END)
        self.library_listbox.selection_set(idx)
        t = getattr(self, "theme", THEMES[DEFAULT_THEME])
        menu = tk.Menu(self, tearoff=0, bg=t["menu_bg"], fg=t["menu_fg"],
                       activebackground=t["menu_active_bg"],
                       activeforeground=t["menu_active_fg"],
                       font=self.font_normal)
        menu.add_command(label="Play",
                          command=lambda: self._play_path(path))
        menu.add_command(label="Add to Queue",
                          command=self.on_enqueue_clicked)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def show_queue_context_menu(self, event):
        if not self.play_queue:
            return
        idx = self.queue_listbox.nearest(event.y)
        if idx < 0:
            return
        self.queue_listbox.selection_clear(0, tk.END)
        self.queue_listbox.selection_set(idx)
        t = getattr(self, "theme", THEMES[DEFAULT_THEME])
        menu = tk.Menu(self, tearoff=0, bg=t["menu_bg"], fg=t["menu_fg"],
                       activebackground=t["menu_active_bg"],
                       activeforeground=t["menu_active_fg"],
                       font=self.font_normal)
        menu.add_command(label="Play Now",
                          command=lambda: self._play_from_queue_index(idx))
        menu.add_command(label="Remove from Queue",
                          command=self.on_remove_from_queue_clicked)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # -- volume / seeking ---------------------------------------------------

    def on_volume_changed(self, value):
        val = int(float(value))
        self.audio.set_volume(val)
        if hasattr(self, "visualizer"):
            self.visualizer.set_volume(val)

    def _on_seek_press(self, event):
        self._user_seeking = True

    def _on_seek_release(self, event):
        self._user_seeking = False
        if self.now_playing:
            self.audio.seek_seconds(self.seek_scale.get())

    def _on_seek_scale_moved(self, value):
        # Fires for BOTH user drags and programmatic .set() calls from
        # _tick(). While the user is actively dragging, mirror the
        # scale position into the elapsed-time label immediately so
        # scrubbing feels responsive; _tick() handles the label the
        # rest of the time.
        if self._user_seeking:
            self.elapsed_var.set(format_time(float(value)))

    # -- periodic UI update ---------------------------------------------------

    def _tick(self):
        for event_name in self.audio.poll_events():
            if event_name == "ended":
                self._handle_track_ended()
            elif event_name == "error":
                self._handle_playback_error()
        self._drain_sync_events()

        if self.now_playing and not self._user_seeking:
            length = self.audio.get_length_seconds()
            position = self.audio.get_time_seconds()
            if length > 0 and abs(length - self._known_length) > 0.5:
                self._known_length = length
                self.seek_scale.config(to=length)
                self.total_var.set(format_time(length))
            if length > 0:
                self.seek_scale.set(position)
            self.elapsed_var.set(format_time(position))
            self._complete_pending_resume(length)

        if self.now_playing:
            self._maybe_autosave()

        # Cheap and idempotent -- a defensive safety net alongside the
        # precise calls already at each state-transition point, in
        # case any future code path changes playback state without
        # remembering to sync the vinyl too.
        self._sync_vinyl_spin_state()

        # Push current position into the CRT lyrics terminal every tick
        # so it scrolls smoothly to the active line.
        if hasattr(self, "lyrics_display") and self.now_playing and not self.is_stopped:
            pos_ms = int(self.audio.get_time_seconds() * 1000)
            self.lyrics_display.sync_to_ms(pos_ms)

        self.now_playing_display.tick()
        self._tick_id = self.after(250, self._tick)

    def _complete_pending_resume(self, length):
        """Finishes what restore_saved_state() started: waits for a
        confirmed, seekable duration from libVLC (or ~3s, whichever
        comes first, as a fallback for an odd file that never firms
        up a length) before seeking to the saved position and pausing
        there."""
        if self._pending_resume_seconds is None:
            return
        self._pending_resume_ticks += 1
        if length <= 0 and self._pending_resume_ticks < 12:
            return  # keep waiting up to ~3s for a trustworthy duration
        target = self._pending_resume_seconds
        if length > 0:
            target = min(target, max(0.0, length - 0.5))
        self.audio.seek_seconds(target)
        self.audio.pause()
        self.is_paused = True
        self.seek_scale.set(target)
        self.elapsed_var.set(format_time(target))
        self.status_var.set(
            f"Resumed: {os.path.basename(self.now_playing)} "
            f"at {format_time(target)}")
        self._pending_resume_seconds = None
        self._sync_vinyl_spin_state()

    def _maybe_autosave(self):
        now = time.time()
        if (now - self._last_autosave) >= AUTOSAVE_INTERVAL_SECONDS:
            self._last_autosave = now
            self.save_state()

    def _handle_track_ended(self):
        if self.now_playing:
            self.status_var.set(
                f"Finished: {os.path.basename(self.now_playing)}")
        self._advance(auto=True)

    def _handle_playback_error(self):
        name = os.path.basename(self.now_playing) if self.now_playing \
            else "track"
        self.status_var.set(f"Could not play {name}, skipping...")
        self._advance(auto=True, force_next=True)

    # -- dialogs ---------------------------------------------------

    def show_retro_error(self, title, message):
        RetroDialog(self, title, message, kind="error")

    def show_retro_info(self, title, message):
        RetroDialog(self, title, message, kind="info")

    def show_about(self):
        message = (
            f"WaveStack v{APP_VERSION}\n\n"
            "A tribute to 1990s desktop audio players.\n"
            "100% offline: no lyrics, no album art downloads,\n"
            "no telemetry -- just your local MP3 collection.\n\n"
            "Built with Python, Tkinter and libVLC.")
        RetroDialog(self, "About WaveStack", message, kind="info")

    # -- LRC sync ------------------------------------------------------------

    def toggle_sync(self):
        self.sync_enabled = not self.sync_enabled
        if self.sync_enabled:
            self.sync_btn.config(relief=tk.SUNKEN, text="[ Online Sync ]")
            # Each run gets its own cancel flag and queue, so a worker
            # still winding down from a previous run can't be revived
            # by, or leak stale messages into, this one.
            self.cancel_sync = threading.Event()
            self._sync_events = queue.Queue()
            self.sync_thread = threading.Thread(
                target=run_lyrics_sync_worker,
                args=(self.library_dir, list(self.library_files),
                      self._sync_events, self.cancel_sync),
                daemon=True)
            self.sync_thread.start()
        else:
            self.sync_btn.config(relief=tk.RAISED, text="[ Offline ]")
            self.cancel_sync.set()
            self.lrc_status_var.set("No internet access (Offline Mode)")

    def _drain_sync_events(self):
        """Apply what the lyrics-sync worker has reported. Called from
        _tick, so all the Tk work happens on the main thread."""
        while True:
            try:
                message = self._sync_events.get_nowait()
            except queue.Empty:
                break
            if not self.sync_enabled:
                continue  # switched offline; drop the leftovers
            kind = message[0]
            if kind == "status":
                self.lrc_status_var.set(message[1])
            elif kind == "downloaded":
                # Lyrics just arrived for the song that's on right now:
                # show them without making the user restart the track.
                if (message[1] == self.now_playing and not self.is_stopped
                        and hasattr(self, "lyrics_display")):
                    self.lyrics_display.load_track(message[1])
            elif kind == "unreachable":
                status = self.lrc_status_var.get()
                self.toggle_sync()               # back to [ Offline ]
                self.lrc_status_var.set(status)  # keep the reason visible

    # -- state persistence ---------------------------------------------------

    def save_state(self):
        """Best-effort write of the current song, position, and volume
        to disk. Persistence is a nice-to-have: it must never raise or
        block shutdown, so every failure mode here is swallowed."""
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            state = {
                "volume": int(self.volume_scale.get()),
                "last_song": self.now_playing,
                "position_seconds": (
                    self.audio.get_time_seconds() if self.now_playing
                    else 0.0),
                "theme": getattr(self, "current_theme_name", DEFAULT_THEME),
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass

    def _load_state_file(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass  # missing file (normal on first run) or corrupted JSON
        return None

    def restore_saved_state(self):
        """Called once at startup, after the library folder has been
        resolved. Restores volume immediately. If the last-played song
        still exists on disk, loads it and arranges for _tick() to
        seek to where playback left off once the file is confirmed
        open, then pause -- so WaveStack is sitting exactly where you
        left it without audibly playing from 0:00 first."""
        state = self._load_state_file()
        if not state:
            return

        theme = state.get("theme")
        if theme in THEMES and theme != self.current_theme_name:
            self.apply_theme(theme)

        volume = state.get("volume")
        if isinstance(volume, (int, float)):
            vol = max(0, min(100, int(volume)))
            self.volume_scale.set(vol)
            self.audio.set_volume(vol)

        song_path = state.get("last_song")
        if not (isinstance(song_path, str) and os.path.isfile(song_path)):
            return

        try:
            position = float(state.get("position_seconds") or 0)
        except (TypeError, ValueError):
            position = 0.0

        self._play_path(song_path,
                         resume_position=position if position > 0 else None)

    # -- shutdown ---------------------------------------------------

    def on_close(self):
        for job_id in (self._tick_id, self._startup_load_id):
            if job_id is not None:
                try:
                    self.after_cancel(job_id)
                except Exception:
                    pass
        if hasattr(self, "vinyl"):
            self.vinyl.shutdown()
        if hasattr(self, "visualizer"):
            self.visualizer.shutdown()
        self._cancel_autosort()
        self._save_genre_cache_if_dirty()
        self.cancel_sync.set()
        self.save_state()
        self.audio.release()
        self.destroy()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _fatal_startup_error(message):
    sys.stderr.write(f"\n[WaveStack Startup Error]\n{message}\n\n")
    try:
        root = tk.Tk()
        root.withdraw()
        RetroDialog(root, "WaveStack - Startup Error", message, kind="error")
        root.destroy()
    except Exception:
        pass


def main():
    global vlc
    if vlc is None:
        venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python3")
        if os.path.isfile(venv_python) and sys.executable != venv_python:
            os.execv(venv_python, [venv_python] + sys.argv)
        _fatal_startup_error(
            "WaveStack could not start because the 'python-vlc' package "
            "is not installed.\n\nActivate your virtual environment and "
            "run:\n  pip install -r requirements.txt")
        sys.exit(1)

    try:
        probe = vlc.Instance()
        if probe is None:
            raise RuntimeError("libVLC failed to initialize.")
        probe.release()
    except Exception as exc:
        _fatal_startup_error(
            "WaveStack could not start because the VLC engine is "
            "missing or broken.\n\nInstall it with:\n"
            "  sudo apt install vlc\n\n"
            f"Details: {exc}")
        sys.exit(1)

    try:
        app = WaveStackApp()
        app.mainloop()
    except Exception as exc:
        _fatal_startup_error(
            f"WaveStack hit an unexpected error and had to close:\n\n{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
