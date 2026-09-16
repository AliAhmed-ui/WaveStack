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
import time
import queue
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog

try:
    import vlc
except ImportError:
    vlc = None


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
# Retro color palette (classic Windows 95 / Motif style)
# --------------------------------------------------------------------------

BG = "#C0C0C0"              # standard "button face" gray
BG_LIGHT = "#E0E0E0"         # lighter gray, used for hover/active states
BORDER_BLACK = "#000000"
TITLE_BLUE = "#000080"       # classic navy blue
SELECT_BG = "#000080"
SELECT_FG = "#FFFFFF"
LCD_BG = "#0A1F0A"           # dark green-black LCD panel background
LCD_FG = "#39FF14"           # bright LCD green


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


# --------------------------------------------------------------------------
# Retro dialog box (replaces tkinter.messagebox so error/info popups
# match the rest of the UI instead of looking like a modern GTK dialog)
# --------------------------------------------------------------------------

class RetroDialog(tk.Toplevel):
    """A chunky, beveled message box in the style of a 1990s Windows
    dialog. The OS still draws the title bar (for compatibility with
    Wayland window managers); everything below it is custom-styled."""

    ICON_GLYPHS = {"info": "i", "warning": "!", "error": "X"}
    ICON_COLORS = {"info": TITLE_BLUE, "warning": "#806000", "error": "#800000"}

    def __init__(self, parent, title, message, kind="info", buttons=None):
        super().__init__(parent)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.title(title)
        self.transient(parent)
        self.result = None

        outer = tk.Frame(self, bg=BG, bd=2, relief=tk.RAISED)
        outer.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)

        content = tk.Frame(outer, bg=BG)
        content.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        icon_glyph = self.ICON_GLYPHS.get(kind, "i")
        icon_color = self.ICON_COLORS.get(kind, "black")
        icon_frame = tk.Frame(content, bg="white", bd=2, relief=tk.SUNKEN,
                               width=36, height=36)
        icon_frame.grid(row=0, column=0, padx=(0, 14), sticky="n")
        icon_frame.grid_propagate(False)
        tk.Label(icon_frame, text=icon_glyph, bg="white", fg=icon_color,
                 font=get_retro_font(self, 16, bold=True)).place(
            relx=0.5, rely=0.5, anchor="center")

        tk.Label(content, text=message, bg=BG, fg="black", justify=tk.LEFT,
                 wraplength=320, font=get_retro_font(self, 9)).grid(
            row=0, column=1, sticky="w")

        button_row = tk.Frame(outer, bg=BG)
        button_row.pack(fill=tk.X, padx=14, pady=(0, 14))

        if buttons is None:
            buttons = [("OK", "ok")]

        for label, value in buttons:
            tk.Button(
                button_row, text=label, width=11,
                font=get_retro_font(self, 9), bg=BG, relief=tk.RAISED,
                bd=3, activebackground=BG_LIGHT,
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
        self.configure(bg=BG)
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
        menubar = tk.Menu(self, bg=BG, fg="black", font=self.font_normal,
                           tearoff=0)

        file_menu = tk.Menu(menubar, tearoff=0, bg=BG, font=self.font_normal)
        file_menu.add_command(label="Open Folder...",
                               command=self.on_open_folder,
                               accelerator="Ctrl+O")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close,
                               accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        playback_menu = tk.Menu(menubar, tearoff=0, bg=BG,
                                 font=self.font_normal)
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
        menubar.add_cascade(label="Playback", menu=playback_menu)

        help_menu = tk.Menu(menubar, tearoff=0, bg=BG, font=self.font_normal)
        help_menu.add_command(label="About WaveStack...",
                               command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _bind_shortcuts(self):
        self.bind_all("<Control-o>", lambda e: self.on_open_folder())
        self.bind_all("<Control-q>", lambda e: self.on_close())

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
        outer = tk.Frame(self, bg=BG, bd=2, relief=tk.RIDGE)
        outer.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)

        # Header banner
        header = tk.Frame(outer, bg=BG, bd=2, relief=tk.RAISED)
        header.pack(fill=tk.X, padx=4, pady=(4, 2))
        tk.Label(header, text="W A V E S T A C K", bg=BG, fg=TITLE_BLUE,
                 font=self.font_title).pack(side=tk.LEFT, padx=10, pady=6)
        tk.Label(header, text="100% Offline MP3 Player", bg=BG, fg="black",
                 font=self.font_normal).pack(side=tk.RIGHT, padx=10)

        # Now playing LCD marquee
        self.now_playing_display = NowPlayingDisplay(outer)
        self.now_playing_display.pack(fill=tk.X, padx=6, pady=6)

        # Transport controls
        transport = tk.Frame(outer, bg=BG)
        transport.pack(fill=tk.X, padx=6, pady=2)

        def make_button(parent, text, command):
            return tk.Button(parent, text=text, command=command,
                              font=self.font_bold, bg=BG, relief=tk.RAISED,
                              bd=3, activebackground=BG_LIGHT, padx=6)

        self.prev_btn = make_button(transport, "\u25c4\u25c4 Prev",
                                     self.on_prev_clicked)
        self.play_btn = make_button(transport, "\u25ba Play",
                                     self.on_play_clicked)
        self.pause_btn = make_button(transport, "|| Pause",
                                      self.on_pause_clicked)
        self.stop_btn = make_button(transport, "\u25a0 Stop",
                                     self.on_stop_clicked)
        self.next_btn = make_button(transport, "Next \u25ba\u25ba",
                                     self.on_next_clicked)
        for b in (self.prev_btn, self.play_btn, self.pause_btn,
                  self.stop_btn, self.next_btn):
            b.pack(side=tk.LEFT, padx=3, pady=3)

        self.open_folder_btn = make_button(transport, "Open Folder...",
                                            self.on_open_folder)
        self.open_folder_btn.pack(side=tk.RIGHT, padx=3, pady=3)

        # Seek bar
        seek_frame = tk.Frame(outer, bg=BG)
        seek_frame.pack(fill=tk.X, padx=6, pady=(6, 2))
        tk.Label(seek_frame, textvariable=self.elapsed_var, bg=BG,
                 font=self.font_normal, width=6).pack(side=tk.LEFT)
        self.seek_scale = tk.Scale(
            seek_frame, from_=0, to=100, orient=tk.HORIZONTAL,
            showvalue=False, bg=BG, troughcolor="white", relief=tk.RAISED,
            bd=2, sliderrelief=tk.RAISED, highlightthickness=0,
            command=self._on_seek_scale_moved)
        self.seek_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.seek_scale.bind("<ButtonPress-1>", self._on_seek_press)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)
        tk.Label(seek_frame, textvariable=self.total_var, bg=BG,
                 font=self.font_normal, width=6).pack(side=tk.LEFT)

        # Volume
        vol_frame = tk.Frame(outer, bg=BG)
        vol_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
        tk.Label(vol_frame, text="Volume", bg=BG,
                 font=self.font_normal).pack(side=tk.LEFT, padx=(0, 6))
        self.volume_scale = tk.Scale(
            vol_frame, from_=0, to=100, orient=tk.HORIZONTAL,
            showvalue=False, bg=BG, troughcolor="white", relief=tk.RAISED,
            bd=2, sliderrelief=tk.RAISED, highlightthickness=0,
            command=self.on_volume_changed, length=160)
        self.volume_scale.set(70)
        self.volume_scale.pack(side=tk.LEFT)
        self.audio.set_volume(70)

        # Library / Queue panes
        panes = tk.Frame(outer, bg=BG)
        panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        panes.columnconfigure(0, weight=3)
        panes.columnconfigure(1, weight=0)
        panes.columnconfigure(2, weight=2)
        panes.rowconfigure(0, weight=1)

        # -- Library pane --
        lib_frame = tk.Frame(panes, bg=BG, bd=2, relief=tk.SUNKEN)
        lib_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        tk.Label(lib_frame, text="Library", bg=BG, font=self.font_bold,
                 anchor="w").pack(fill=tk.X, padx=4, pady=(2, 0))
        lib_list_frame = tk.Frame(lib_frame, bg=BG)
        lib_list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        lib_scroll = tk.Scrollbar(lib_list_frame, orient=tk.VERTICAL)
        self.library_listbox = tk.Listbox(
            lib_list_frame, bg="white", fg="black", font=self.font_normal,
            relief=tk.SUNKEN, bd=2, selectbackground=SELECT_BG,
            selectforeground=SELECT_FG, activestyle="none",
            selectmode=tk.EXTENDED, exportselection=False,
            yscrollcommand=lib_scroll.set)
        lib_scroll.config(command=self.library_listbox.yview)
        self.library_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lib_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.library_listbox.bind("<Double-Button-1>",
                                   lambda e: self.on_play_clicked())
        self.library_listbox.bind("<Button-3>",
                                   self.show_library_context_menu)

        # -- Middle buttons --
        mid_frame = tk.Frame(panes, bg=BG)
        mid_frame.grid(row=0, column=1, sticky="ns", padx=4)
        mid_inner = tk.Frame(mid_frame, bg=BG)
        mid_inner.pack(expand=True)
        tk.Button(mid_inner, text="Enqueue >>",
                  command=self.on_enqueue_clicked, font=self.font_normal,
                  bg=BG, relief=tk.RAISED, bd=3).pack(pady=6, fill=tk.X)
        tk.Button(mid_inner, text="<< Remove",
                  command=self.on_remove_from_queue_clicked,
                  font=self.font_normal, bg=BG, relief=tk.RAISED,
                  bd=3).pack(pady=6, fill=tk.X)
        tk.Button(mid_inner, text="Clear Queue",
                  command=self.on_clear_queue_clicked,
                  font=self.font_normal, bg=BG, relief=tk.RAISED,
                  bd=3).pack(pady=6, fill=tk.X)

        # -- Queue pane --
        queue_frame = tk.Frame(panes, bg=BG, bd=2, relief=tk.SUNKEN)
        queue_frame.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        queue_header = tk.Frame(queue_frame, bg=BG)
        queue_header.pack(fill=tk.X, padx=4, pady=(2, 0))
        tk.Label(queue_header, text="Queue", bg=BG, font=self.font_bold,
                 anchor="w").pack(side=tk.LEFT)
        tk.Label(queue_header, textvariable=self.queue_count_var, bg=BG,
                 font=self.font_normal).pack(side=tk.RIGHT)
        queue_list_frame = tk.Frame(queue_frame, bg=BG)
        queue_list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        queue_scroll = tk.Scrollbar(queue_list_frame, orient=tk.VERTICAL)
        self.queue_listbox = tk.Listbox(
            queue_list_frame, bg="white", fg="black", font=self.font_normal,
            relief=tk.SUNKEN, bd=2, selectbackground=SELECT_BG,
            selectforeground=SELECT_FG, activestyle="none",
            selectmode=tk.EXTENDED, exportselection=False,
            yscrollcommand=queue_scroll.set)
        queue_scroll.config(command=self.queue_listbox.yview)
        self.queue_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        queue_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.queue_listbox.bind("<Double-Button-1>", self.on_play_from_queue)
        self.queue_listbox.bind("<Button-3>", self.show_queue_context_menu)

        # Status bar
        status_bar = tk.Frame(outer, bg=BG, bd=2, relief=tk.SUNKEN)
        status_bar.pack(fill=tk.X, padx=4, pady=(2, 4))
        tk.Label(status_bar, textvariable=self.status_var, bg=BG,
                 font=self.font_normal, anchor="w").pack(
            side=tk.LEFT, padx=6, pady=2, fill=tk.X, expand=True)

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
        self.on_play_pause_toggle()
        return "break"

    def on_play_clicked(self):
        if self.is_paused and self.now_playing:
            self.audio.resume()
            self.is_paused = False
            self.is_stopped = False
            self.status_var.set(
                f"Playing: {os.path.basename(self.now_playing)}")
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

    def on_stop_clicked(self):
        self.audio.stop()
        self.is_paused = False
        self._set_stopped_state()

    def _set_stopped_state(self):
        self.is_stopped = True
        self.seek_scale.set(0)
        self.elapsed_var.set("00:00")
        self.status_var.set("Stopped")

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

        if resume_position and resume_position > 0:
            self._pending_resume_seconds = resume_position
            self._pending_resume_ticks = 0
        else:
            self._pending_resume_seconds = None

    def _highlight_now_playing(self):
        for i in range(self.library_listbox.size()):
            self.library_listbox.itemconfig(i, bg="white", fg="black")
        if self.now_playing in self.library_files:
            idx = self.library_files.index(self.now_playing)
            self.library_listbox.itemconfig(idx, bg=SELECT_BG,
                                             fg=SELECT_FG)

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
        menu = tk.Menu(self, tearoff=0, bg=BG, font=self.font_normal)
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
        menu = tk.Menu(self, tearoff=0, bg=BG, font=self.font_normal)
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
