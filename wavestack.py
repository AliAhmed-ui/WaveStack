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
import urllib.parse
import requests
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
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False



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

SEARCH_PLACEHOLDER_TEXT = "search song"
SEARCH_PLACEHOLDER_FG = THEMES["light"]["search_placeholder_fg"]
SEARCH_NORMAL_FG = THEMES["light"]["search_normal_fg"]

# Keys that change the search entry's cursor position or invoke a
# specific search action, but don't change its text -- recomputing
# suggestions on these would be wasted work, and for Down/Up it would
# actively fight the dropdown navigation below.
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
        label_r = disc_r * 0.36
        label_d = max(2, int(label_r * 2))

        art = _crop_to_square(art).resize((label_d, label_d), Image.LANCZOS)
        mask = _make_circular_mask(label_d)
        circular_art = Image.new("RGBA", (label_d, label_d), (0, 0, 0, 0))
        circular_art.paste(art, (0, 0), mask)

        combined = self._blank_vinyl.copy()
        top_left = (int(cx - label_d / 2), int(cy - label_d / 2))
        combined.paste(circular_art, top_left, circular_art)

        # The art circle covers the spindle hole; redraw it on top.
        self._draw_spindle_hole(ImageDraw.Draw(combined), cx, cy, disc_r)
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
 
        self.canvas.bind("<Configure>", lambda e: self._layout())
        self.after_idle(self._layout)
 
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
 
    def _layout(self):
        """(Re)builds the LED segment grid to fit the canvas's current
        size. Safe to call on resize: it doesn't touch self._heights,
        it only repositions/recreates the rectangles that display
        them, so a resize never disturbs the running animation."""
        width = self.canvas.winfo_width()
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

        # Search-suggestions dropdown state: the list of full file
        # paths currently shown in the dropdown, in display order, so
        # a click or Enter can map a row straight back to a path.
        self._current_suggestions = []
        # LRC sync state
        self.sync_enabled = False
        self.sync_thread = None
        self.cancel_sync = threading.Event()
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

        self.subtitle_label = tk.Label(self.header_frame, text="100% Offline MP3 Player",
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

        self.sync_btn = tk.Button(self.status_bar, text="[ Offline ]", width=14,
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
        self.panes.columnconfigure(0, weight=3)
        self.panes.columnconfigure(1, weight=0)
        self.panes.columnconfigure(2, weight=2)
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

        # Suggestions dropdown: a plain child of the main window,
        # positioned with .place() rather than shown as a separate
        # Toplevel popup. A Toplevel's requested screen position is
        # frequently ignored under Wayland (Ubuntu 26.04's default
        # session), which would make a floating popup appear in the
        # wrong place or not track the window at all; a placed child
        # widget is pure internal Tk layout and has no such risk.
        self.suggestions_frame = tk.Frame(self, bg=t["border_black"], bd=1,
                                           relief=tk.RAISED)
        self.suggestions_listbox = tk.Listbox(
            self.suggestions_frame, bg=t["entry_bg"], fg=t["entry_fg"],
            font=self.font_normal, relief=tk.FLAT, bd=0,
            selectbackground=t["select_bg"], selectforeground=t["select_fg"],
            activestyle="none", exportselection=False, highlightthickness=0)
        self.suggestions_listbox.pack(fill=tk.BOTH, expand=True,
                                       padx=1, pady=1)
        self.suggestions_listbox.bind("<Button-1>",
                                       self._on_suggestion_clicked)

        self.lib_list_frame = tk.Frame(self.lib_frame, bg=t["bg"])
        self.lib_list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.lib_scroll = tk.Scrollbar(self.lib_list_frame, orient=tk.VERTICAL)
        self.library_listbox = tk.Listbox(
            self.lib_list_frame, bg=t["entry_bg"], fg=t["entry_fg"], font=self.font_normal,
            relief=tk.SUNKEN, bd=2, selectbackground=t["select_bg"],
            selectforeground=t["select_fg"], activestyle="none",
            selectmode=tk.EXTENDED, exportselection=False,
            yscrollcommand=self.lib_scroll.set)
        self.lib_scroll.config(command=self.library_listbox.yview)
        self.library_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.lib_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.library_listbox.bind("<Double-Button-1>",
                                   lambda e: self.on_play_clicked())
        self.library_listbox.bind("<Button-3>",
                                   self.show_library_context_menu)

        # -- Middle buttons --
        self.mid_frame = tk.Frame(self.panes, bg=t["bg"])
        self.mid_frame.grid(row=0, column=1, sticky="ns", padx=4)
        self.mid_inner = tk.Frame(self.mid_frame, bg=t["bg"])
        self.mid_inner.pack(expand=True)
        self.enqueue_btn = tk.Button(self.mid_inner, text="Enqueue >>",
                  command=self.on_enqueue_clicked, font=self.font_normal,
                  bg=t["btn_bg"], fg=t["btn_fg"], relief=tk.RAISED, bd=3,
                  activebackground=t["btn_active_bg"],
                  activeforeground=t["btn_active_fg"])
        self.enqueue_btn.pack(pady=6, fill=tk.X)
        self.remove_btn = tk.Button(self.mid_inner, text="<< Remove",
                  command=self.on_remove_from_queue_clicked,
                  font=self.font_normal, bg=t["btn_bg"], fg=t["btn_fg"],
                  relief=tk.RAISED, bd=3,
                  activebackground=t["btn_active_bg"],
                  activeforeground=t["btn_active_fg"])
        self.remove_btn.pack(pady=6, fill=tk.X)
        self.clear_btn = tk.Button(self.mid_inner, text="Clear Queue",
                  command=self.on_clear_queue_clicked,
                  font=self.font_normal, bg=t["btn_bg"], fg=t["btn_fg"],
                  relief=tk.RAISED, bd=3,
                  activebackground=t["btn_active_bg"],
                  activeforeground=t["btn_active_fg"])
        self.clear_btn.pack(pady=6, fill=tk.X)

        self.vinyl = VinylDisc(self.mid_inner, theme=t)
        self.vinyl.pack(pady=(14, 6))


        # -- Right column: Queue (top) + CRT Lyrics Terminal (bottom) --
        # A dedicated container occupies column 2 so both sub-panes can
        # share vertical space with an equal 50/50 split.
        self.right_col = tk.Frame(self.panes, bg=t["bg"])
        self.right_col.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        self.right_col.rowconfigure(0, weight=1)
        self.right_col.rowconfigure(1, weight=1)
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
        else:
            self.panes.pack_forget()
            self.visualizer.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
            self.visualizer_showing = True
            self.visualizer_btn.config(relief=tk.SUNKEN)

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
        if hasattr(self, "suggestions_frame"):
            self.suggestions_frame.configure(bg=t["border_black"])
        if hasattr(self, "suggestions_listbox"):
            self.suggestions_listbox.configure(
                bg=t["entry_bg"], fg=t["entry_fg"],
                selectbackground=t["select_bg"],
                selectforeground=t["select_fg"]
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
        for btn in (getattr(self, "enqueue_btn", None),
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

        # Queue
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
        self.library_dir = directory
        self.library_files = files
        self.refresh_library_listbox()
        self.status_var.set(f"Loaded {len(files)} track(s) from {directory}")
        return True

    def refresh_library_listbox(self):
        self.library_listbox.delete(0, tk.END)
        for path in self.library_files:
            name = os.path.splitext(os.path.basename(path))[0]
            self.library_listbox.insert(tk.END, name)
        self._highlight_now_playing()
        self._hide_suggestions()  # library changed; any open suggestions are stale

    # -- search ---------------------------------------------------

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
        self._update_suggestions()

    def _on_search_focus_out(self, event=None):
        # Delay briefly so a click landing on the suggestions list has
        # a chance to register (and move focus there) before deciding
        # whether to close the dropdown -- otherwise closing it here
        # first could swallow that click.
        self.after(120, self._resolve_search_focus_out)

    def _resolve_search_focus_out(self):
        if self.focus_get() is self.suggestions_listbox:
            return
        if not self.search_entry.get():
            t = getattr(self, "theme", THEMES[DEFAULT_THEME])
            self.search_entry.config(fg=t["search_placeholder_fg"])
            self.search_entry.insert(0, SEARCH_PLACEHOLDER_TEXT)
        self._hide_suggestions()

    def _on_search_enter(self, event=None):
        if not self._current_suggestions:
            return "break"
        sel = self.suggestions_listbox.curselection()
        idx = sel[0] if sel else 0
        self._confirm_suggestion(idx)
        return "break"

    def _on_search_escape(self, event=None):
        self._hide_suggestions()
        self._clear_search_to_placeholder()
        self.library_listbox.focus_set()
        return "break"

    def _on_search_arrow_down(self, event=None):
        self._move_suggestion_selection(1)
        return "break"

    def _on_search_arrow_up(self, event=None):
        self._move_suggestion_selection(-1)
        return "break"

    def _move_suggestion_selection(self, delta):
        count = len(self._current_suggestions)
        if count == 0:
            return
        sel = self.suggestions_listbox.curselection()
        current = sel[0] if sel else 0
        new_idx = max(0, min(count - 1, current + delta))
        self.suggestions_listbox.selection_clear(0, tk.END)
        self.suggestions_listbox.selection_set(new_idx)
        self.suggestions_listbox.activate(new_idx)
        self.suggestions_listbox.see(new_idx)

    def _on_suggestion_clicked(self, event):
        # Compute the clicked row directly from the click position
        # rather than trusting curselection(): this instance-level
        # binding runs before the Listbox's own class-level "select
        # the clicked row" binding, so curselection() could still
        # reflect the previous selection at this point.
        idx = self.suggestions_listbox.nearest(event.y)
        self._confirm_suggestion(idx)
        return "break"

    def _confirm_suggestion(self, idx):
        if not (0 <= idx < len(self._current_suggestions)):
            return
        path = self._current_suggestions[idx]
        self._select_track_in_library(path)
        self._hide_suggestions()
        self._clear_search_to_placeholder()
        self.library_listbox.focus_set()

    def _select_track_in_library(self, path):
        if path not in self.library_files:
            return
        idx = self.library_files.index(path)
        self.library_listbox.selection_clear(0, tk.END)
        self.library_listbox.selection_set(idx)
        self.library_listbox.activate(idx)
        self.library_listbox.see(idx)

    def _get_search_matches(self, query, limit=8):
        query = query.strip().lower()
        if not query:
            return []
        starts_with, contains = [], []
        for path in self.library_files:
            name = os.path.splitext(os.path.basename(path))[0].lower()
            if name.startswith(query):
                starts_with.append(path)
            elif query in name:
                contains.append(path)
        return (starts_with + contains)[:limit]

    def _update_suggestions(self):
        query = "" if self._search_showing_placeholder() \
            else self.search_entry.get()
        if not query.strip():
            self._hide_suggestions()
            return
        self._show_suggestions(self._get_search_matches(query))

    def _show_suggestions(self, matches):
        self._current_suggestions = matches
        self.suggestions_listbox.delete(0, tk.END)
        if matches:
            for path in matches:
                name = os.path.splitext(os.path.basename(path))[0]
                self.suggestions_listbox.insert(tk.END, name)
            self.suggestions_listbox.config(height=min(len(matches), 8))
            self.suggestions_listbox.selection_clear(0, tk.END)
            self.suggestions_listbox.selection_set(0)
            self.suggestions_listbox.activate(0)
        else:
            self.suggestions_listbox.insert(tk.END, "(no matches)")
            self.suggestions_listbox.config(height=1)

        self._position_suggestions_box()
        self.suggestions_frame.lift()

    def _hide_suggestions(self):
        self.suggestions_frame.place_forget()
        self._current_suggestions = []

    def _position_suggestions_box(self):
        self.update_idletasks()
        x = self.search_entry.winfo_rootx() - self.winfo_rootx()
        y = (self.search_entry.winfo_rooty() - self.winfo_rooty()
             + self.search_entry.winfo_height())
        width = self.search_entry.winfo_width()
        self.suggestions_frame.place(x=x, y=y, width=width)

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
        """Drives the vinyl widget from the same three-state model
        (playing / paused / stopped-or-nothing) used everywhere else
        in the app, e.g. on_play_pause_toggle(). Cheap and idempotent,
        so it's safe to call from every place playback state changes
        without worrying about redundant calls."""
        if hasattr(self, "vinyl"):
            if self.now_playing and not self.is_paused and not self.is_stopped:
                self.vinyl.start_spinning()
            elif self.now_playing and self.is_paused:
                self.vinyl.pause_spinning()
            else:
                self.vinyl.stop_and_reset()

    def on_play_clicked(self):
        if self.is_paused and self.now_playing:
            self.audio.resume()
            self.is_paused = False
            self.is_stopped = False
            self.status_var.set(
                f"Playing: {os.path.basename(self.now_playing)}")
            self._sync_vinyl_spin_state()
            return
        sel = self.library_listbox.curselection()
        if sel:
            self._play_path(self.library_files[sel[0]])
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

    def on_next_clicked(self):
        self._advance(auto=False)

    def on_prev_clicked(self):
        if self.history:
            path = self.history.pop()
            self._play_path(path)
        else:
            self.status_var.set("No previous track.")

    def _advance(self, auto=False):
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
            self._advance(auto=True)
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
            self._advance(auto=True)
            return
        self.now_playing = path
        self.is_paused = False
        self.is_stopped = False
        self._known_length = 0.0
        self.seek_scale.config(to=100)
        self.seek_scale.set(0)
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
        for i in range(self.library_listbox.size()):
            self.library_listbox.itemconfig(i, bg=t["entry_bg"], fg=t["entry_fg"])
        if self.now_playing in self.library_files:
            idx = self.library_files.index(self.now_playing)
            self.library_listbox.itemconfig(idx, bg=t["select_bg"],
                                             fg=t["select_fg"])

    # -- queue management ---------------------------------------------------

    def on_enqueue_clicked(self):
        sel = self.library_listbox.curselection()
        if not sel:
            self.status_var.set("Select a track in Library first.")
            return
        for i in sel:
            self.play_queue.append(self.library_files[i])
        self.refresh_queue_listbox()
        self.status_var.set(f"Added {len(sel)} track(s) to the queue.")

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
        idx = self.library_listbox.nearest(event.y)
        if idx < 0:
            return
        self.library_listbox.selection_clear(0, tk.END)
        self.library_listbox.selection_set(idx)
        t = getattr(self, "theme", THEMES[DEFAULT_THEME])
        menu = tk.Menu(self, tearoff=0, bg=t["menu_bg"], fg=t["menu_fg"],
                       activebackground=t["menu_active_bg"],
                       activeforeground=t["menu_active_fg"],
                       font=self.font_normal)
        menu.add_command(label="Play", command=self.on_play_clicked)
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
        self.audio.set_volume(int(float(value)))

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
        self._advance(auto=True)

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
            self.cancel_sync.clear()
            if self.sync_thread is None or not self.sync_thread.is_alive():
                self.sync_thread = threading.Thread(target=self._lrc_sync_worker, daemon=True)
                self.sync_thread.start()
        else:
            self.sync_btn.config(relief=tk.RAISED, text="[ Offline ]")
            self.cancel_sync.set()
            self.lrc_status_var.set("No internet access (Offline Mode)")

    def _lrc_sync_worker(self):
        self.after(0, self.lrc_status_var.set, "Scanning local .lrc files...")
        
        # Determine files to scan
        if self.library_dir:
            files_to_scan = scan_mp3_files(self.library_dir)
        else:
            files_to_scan = self.library_files
            
        if not files_to_scan:
            self.after(0, self.lrc_status_var.set, "No tracks to sync.")
            return

        missing_lrc = []
        for mp3_path in files_to_scan:
            if self.cancel_sync.is_set():
                return
            lrc_path = os.path.splitext(mp3_path)[0] + ".lrc"
            if not os.path.exists(lrc_path):
                missing_lrc.append(mp3_path)

        if not missing_lrc:
            if not self.cancel_sync.is_set():
                self.after(0, self.lrc_status_var.set, "All available lyrics up to date.")
            return

        for mp3_path in missing_lrc:
            if self.cancel_sync.is_set():
                return
            
            title, artist = "", ""
            if MUTAGEN_AVAILABLE:
                try:
                    from mutagen.easyid3 import EasyID3
                    audio = EasyID3(mp3_path)
                    title = audio.get("title", [""])[0]
                    artist = audio.get("artist", [""])[0]
                except Exception:
                    pass
                    
            if not title:
                title = os.path.splitext(os.path.basename(mp3_path))[0]
                
            display_name = os.path.basename(mp3_path)
            self.after(0, self.lrc_status_var.set, f"Fetching lyrics: {display_name}...")
            
            try:
                url = f"https://lrclib.net/api/get?track_name={urllib.parse.quote(title)}"
                if artist:
                    url += f"&artist_name={urllib.parse.quote(artist)}"
                    
                res = requests.get(url, headers={'User-Agent': 'WaveStack/1.0'}, timeout=5)
                
                if res.status_code == 200:
                    data = res.json()
                    synced = data.get("syncedLyrics")
                    if synced:
                        lrc_path = os.path.splitext(mp3_path)[0] + ".lrc"
                        try:
                            with open(lrc_path, "w", encoding="utf-8") as f:
                                f.write(synced)
                        except OSError:
                            pass
                
                time.sleep(0.5)
            except requests.RequestException:
                if not self.cancel_sync.is_set():
                    self.after(0, self.lrc_status_var.set, "Rate limit: sync paused")
                    # Turn off sync automatically if connection fails hard
                    self.after(0, self.toggle_sync)
                return

        if not self.cancel_sync.is_set():
            self.after(0, self.lrc_status_var.set, "All lyrics up to date.")

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
        self.save_state()
        self.audio.release()
        self.destroy()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _fatal_startup_error(message):
    root = tk.Tk()
    root.withdraw()
    RetroDialog(root, "WaveStack - Startup Error", message, kind="error")
    root.destroy()


def main():
    if vlc is None:
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
