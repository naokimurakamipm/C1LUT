"""Capture One-inspired dark theme for the Tk/ttk interface.

The palette mirrors Capture One's charcoal panels with its signature warm
orange accent, applied on top of the 'clam' theme (the most recolourable
ttk theme on Windows).
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

FONT = "Yu Gothic UI"

# Capture One-style palette.
WINDOW = "#191919"    # toplevel / recessed areas
PANEL = "#232323"     # cards, tab pages, labelframes
FIELD = "#2e2e2e"     # inputs, buttons, list rows
FIELD_ACTIVE = "#3a3a3a"
SEL_BG = "#3f3f3f"    # list / row selection
BORDER = "#3a3a3a"
TEXT = "#d8d8d8"
TEXT_DIM = "#909090"
ACCENT = "#e08a2e"    # Capture One orange
ACCENT_HOVER = "#f09a44"
ACCENT_TEXT = "#191212"

# Status colours tuned for the dark background.
STATUS_COLORS = {
    "success": "#5db56b",
    "error": "#e06055",
    "skipped": "#c8a64b",
    "cancelled": "#8a8a8a",
}

PRIMARY_BUTTON = "Accent.TButton"
CARD_FRAME = "Card.TFrame"
DIM_LABEL = "Dim.TLabel"


def enable_high_dpi() -> bool:
    """Declare DPI awareness before the first Tk window is created.

    Without this Windows reports 96 DPI to the process and then bitmap-
    stretches the window on scaled displays: text renders blurry and Tk's
    winfo coordinates no longer match physical screen pixels. Returns True
    when some form of DPI awareness was set.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        try:
            context = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(context):
                return True
        except (AttributeError, OSError):
            pass
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # per-monitor v1
                return True
        except (AttributeError, OSError):
            pass
        try:
            return bool(ctypes.windll.user32.SetProcessDPIAware())
        except (AttributeError, OSError):
            return False
    except Exception:
        return False


def apply_dark_title_bar(root: tk.Tk) -> bool:
    """Paint the native window frame in the palette (Windows 10 1809+ / 11).

    Windows draws a white title bar by default even for dark apps. DWM
    immersive dark mode switches the frame to dark; on Windows 11 the exact
    caption/border/text colours can be set on top of that. Returns True when
    at least the generic dark mode was applied.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        dwmapi = ctypes.WinDLL("dwmapi")
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        hwnd = wintypes.HWND(hwnd)

        def set_dword(attribute: int, value: int) -> int:
            return dwmapi.DwmSetWindowAttribute(
                hwnd, wintypes.DWORD(attribute),
                ctypes.byref(wintypes.DWORD(value)), ctypes.sizeof(wintypes.DWORD))

        def set_color(attribute: int, hex_color: str) -> int:
            r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
            colorref = wintypes.COLORREF(b | (g << 8) | (r << 16))
            return dwmapi.DwmSetWindowAttribute(
                hwnd, wintypes.DWORD(attribute),
                ctypes.byref(colorref), ctypes.sizeof(colorref))

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # 19 before Windows 10 build 19041
        DWMWA_BORDER_COLOR = 34
        DWMWA_CAPTION_COLOR = 35
        DWMWA_TEXT_COLOR = 36

        applied = set_dword(DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
        if applied != 0:
            applied = set_dword(DWMWA_USE_IMMERSIVE_DARK_MODE - 1, 1)  # legacy slot
        # Exact palette match needs Windows 11 build 22000+; failures are fine.
        set_color(DWMWA_CAPTION_COLOR, WINDOW)
        set_color(DWMWA_TEXT_COLOR, TEXT)
        set_color(DWMWA_BORDER_COLOR, BORDER)
        return applied == 0
    except Exception:
        return False


def apply_capture_one_style(root: tk.Tk, style: ttk.Style) -> ttk.Style:
    """Recolour the whole widget set; returns the configured style."""
    style.theme_use("clam")
    apply_dark_title_bar(root)
    # Re-apply once the window is mapped; DWM needs a realised frame on some
    # builds to pick the attributes up.
    root.after(120, lambda: apply_dark_title_bar(root))

    root.configure(background=WINDOW)
    style.configure(".", background=WINDOW, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=WINDOW, darkcolor=WINDOW, font=(FONT, 10),
                    troughcolor=FIELD, focuscolor=ACCENT)
    style.configure("TFrame", background=WINDOW)
    style.configure(CARD_FRAME, background=PANEL)
    style.configure("TLabelframe", background=PANEL, bordercolor=BORDER,
                    borderwidth=1, relief="solid", lightcolor=PANEL, darkcolor=PANEL)
    style.configure("TLabelframe.Label", background=PANEL, foreground=TEXT_DIM,
                    font=(FONT, 10, "bold"))

    style.configure("TLabel", background=WINDOW, foreground=TEXT)
    style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
    style.configure("CardDim.TLabel", background=PANEL, foreground=TEXT_DIM)
    style.configure(DIM_LABEL, background=WINDOW, foreground=TEXT_DIM)
    style.configure("Heading.TLabel", font=(FONT, 18, "bold"), foreground=TEXT)

    style.configure("TButton", background=FIELD, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=FIELD, darkcolor=FIELD, relief="flat", padding=(12, 5),
                    focusthickness=1, focuscolor=BORDER, font=(FONT, 10))
    style.map("TButton",
              background=[("pressed", FIELD_ACTIVE), ("active", FIELD_ACTIVE),
                          ("disabled", WINDOW)],
              lightcolor=[("active", FIELD_ACTIVE)],
              foreground=[("disabled", TEXT_DIM)])
    style.configure(PRIMARY_BUTTON, background=ACCENT, foreground=ACCENT_TEXT,
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    font=(FONT, 10, "bold"), padding=(16, 6), focuscolor=ACCENT_HOVER)
    style.map(PRIMARY_BUTTON,
              background=[("pressed", ACCENT_HOVER), ("active", ACCENT_HOVER),
                          ("disabled", "#5c4322")],
              lightcolor=[("active", ACCENT_HOVER)],
              foreground=[("disabled", "#8a7c66")])

    style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                    insertcolor=TEXT, bordercolor=BORDER, lightcolor=FIELD,
                    darkcolor=FIELD, padding=3,
                    selectbackground=SEL_BG, selectforeground=TEXT)
    style.map("TEntry", fieldbackground=[("focus", FIELD_ACTIVE), ("disabled", PANEL)],
              foreground=[("disabled", TEXT_DIM)],
              bordercolor=[("focus", ACCENT)])

    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                    foreground=TEXT, arrowcolor=TEXT_DIM, bordercolor=BORDER,
                    lightcolor=FIELD, darkcolor=FIELD, arrowsize=12, padding=3,
                    selectbackground=SEL_BG, selectforeground=TEXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", FIELD), ("disabled", PANEL),
                               ("focus", FIELD_ACTIVE)],
              foreground=[("disabled", TEXT_DIM)],
              arrowcolor=[("active", ACCENT)])
    # The dropdown listbox is a classic tk widget reached through the option DB.
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SEL_BG)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.activeBackground", FIELD_ACTIVE)
    root.option_add("*TCombobox*Listbox.activeForeground", ACCENT)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.highlightThickness", 0)

    style.configure("TNotebook", background=WINDOW, borderwidth=0, tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab", background=WINDOW, foreground=TEXT_DIM,
                    borderwidth=0, padding=(16, 7), font=(FONT, 10))
    style.map("TNotebook.Tab",
              background=[("selected", PANEL)],
              foreground=[("selected", ACCENT), ("active", TEXT)])

    style.configure("Treeview", background=FIELD, foreground=TEXT, fieldbackground=FIELD,
                    borderwidth=0, rowheight=26, lightcolor=FIELD, darkcolor=FIELD)
    style.configure("Treeview.Heading", background=PANEL, foreground=TEXT_DIM,
                    relief="flat", padding=(6, 4), font=(FONT, 10, "bold"),
                    borderwidth=0, lightcolor=PANEL, darkcolor=PANEL)
    style.map("Treeview",
              background=[("selected", SEL_BG)],
              foreground=[("selected", TEXT)])
    style.map("Treeview.Heading", background=[("active", FIELD)])

    style.configure("TProgressbar", troughcolor=FIELD, background=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, borderwidth=0,
                    thickness=10)
    style.map("TProgressbar", background=[("disabled", FIELD)])

    style.configure("TCheckbutton", background=WINDOW, foreground=TEXT, focuscolor=WINDOW,
                    indicatorcolor=FIELD, padding=2)
    style.map("TCheckbutton",
              background=[("active", WINDOW)],
              indicatorcolor=[("selected", ACCENT), ("!selected", FIELD),
                              ("alternate", FIELD_ACTIVE)])

    style.configure("Vertical.TScrollbar", troughcolor=WINDOW, background=FIELD,
                    bordercolor=WINDOW, lightcolor=FIELD, darkcolor=FIELD,
                    arrowcolor=TEXT_DIM, gripcount=0)
    style.map("Vertical.TScrollbar", background=[("active", FIELD_ACTIVE)])
    style.configure("Horizontal.TScrollbar", troughcolor=WINDOW, background=FIELD,
                    bordercolor=WINDOW, lightcolor=FIELD, darkcolor=FIELD,
                    arrowcolor=TEXT_DIM, gripcount=0)

    # Classic tk widgets used directly (log Text): derive the log scrollbar
    # style from the styled Vertical.TScrollbar so it keeps a valid layout.
    style.layout("Vertical.Log.TScrollbar", style.layout("Vertical.TScrollbar"))
    style.configure("Vertical.Log.TScrollbar", troughcolor=PANEL, background=FIELD,
                    bordercolor=PANEL, lightcolor=FIELD, darkcolor=FIELD,
                    arrowcolor=TEXT_DIM, gripcount=0)
    return style


def configure_log_text(text: tk.Text) -> tk.Text:
    """Apply the palette to a classic Text widget used for the log."""
    text.configure(
        background=PANEL, foreground=TEXT, insertbackground=TEXT,
        selectbackground=SEL_BG, selectforeground=TEXT,
        relief="flat", borderwidth=0, highlightthickness=1,
        highlightbackground=BORDER, highlightcolor=ACCENT,
        font=("Consolas", 10),
    )
    return text
