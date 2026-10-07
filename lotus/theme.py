"""Lotus palette and terminal helpers. Zero dependencies; Windows, macOS, Linux."""
import os
import re
import shutil
import sys
import time
import unicodedata


def _prepare_windows():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for handle_id in (-11, -12):
            h = k.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)  # VT processing
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


_prepare_windows()


def _color_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("LOTUS_COLOR", "").lower() in ("1", "always", "true"):
        return True
    return sys.stdout.isatty()


COLOR = _color_enabled()
TRUECOLOR = bool(
    os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    or os.environ.get("WT_SESSION")
    or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "vscode", "WezTerm", "ghostty", "Apple_Terminal")
)
ASCII = os.environ.get("LOTUS_ASCII") == "1"

# Two palettes with the same names, so every caller stays theme-agnostic. PAL is
# swapped in place (see apply), and colors are looked up when text is drawn, so a
# change takes effect on the next line printed.
DARK = {
    "petal": (244, 166, 198),   # headers and bold
    "bloom": (224, 86, 155),    # borders, bullets, prompt
    "stamen": (242, 193, 78),   # code and tool icon
    "leaf": (127, 182, 133),    # success marks
    "pond": (108, 142, 173),
    "mist": (150, 132, 148),    # dim text
    "ink": (236, 226, 233),     # body text
    "thorn": (232, 96, 96),     # errors
}
LIGHT = {
    "petal": (196, 74, 132),
    "bloom": (176, 38, 110),
    "stamen": (168, 112, 0),
    "leaf": (46, 125, 60),
    "pond": (40, 90, 140),
    "mist": (110, 96, 108),
    "ink": (40, 30, 38),
    "thorn": (190, 40, 40),
}
PAL = dict(DARK)
MODE = "dark"      # the palette in use
PREF = "auto"      # what the user asked for: auto | dark | light
SOURCE = "default"  # how MODE was decided, shown by /theme and doctor
_osc_ok = None     # None = not tried yet, False = terminal never answers OSC 11


def apply(mode):
    global MODE
    MODE = "light" if mode == "light" else "dark"
    PAL.clear()
    PAL.update(LIGHT if MODE == "light" else DARK)


# ── background detection ────────────────────────────────────────────────────

OSC11_RE = re.compile(r"\x1b\]11;rgba?:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)(?:/[0-9a-fA-F]+)?(?:\x07|\x1b\\)")
DA1_RE = re.compile(r"\x1b\[\?[0-9;]*c")
ESC_SEQ_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|.)")


def _luma(rgb):
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex(h):
    return int(h, 16) / (16 ** len(h) - 1)


def query_background(timeout=0.4):
    """Ask the terminal for its background color with OSC 11.

    Sends the query followed by a DA1 request, which every terminal answers, so a
    terminal without OSC 11 support costs one round trip instead of the full timeout.
    Returns (rgb 0..1 or None, typeahead). Typeahead is anything the user had already
    typed (an unfinished line), so the caller can put it back into the prompt."""
    if os.name == "nt" or not sys.stdout.isatty():
        return None, ""
    try:
        import select
        import termios
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except (ImportError, OSError):
        return None, ""
    try:
        r, _, _ = select.select([fd], [], [], 0)
        if r:  # a finished line is waiting for input(); don't steal it
            return None, ""
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ICANON | termios.ECHO)
        new[6][termios.VMIN], new[6][termios.VTIME] = 0, 0
        buf = b""
        try:
            termios.tcsetattr(fd, termios.TCSANOW, new)
            os.write(fd, b"\x1b]11;?\x1b\\\x1b[c")
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                r, _, _ = select.select([fd], [], [], left)
                if not r:
                    break
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                buf += chunk
                if DA1_RE.search(buf.decode("latin-1")):
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old)
    except Exception:
        return None, ""
    finally:
        os.close(fd)
    text = buf.decode("utf-8", "replace")
    m = OSC11_RE.search(text)
    rgb = tuple(_hex(x) for x in m.groups()) if m else None
    rest = DA1_RE.sub("", OSC11_RE.sub("", text))
    rest = ESC_SEQ_RE.sub("", rest)
    return rgb, "".join(ch for ch in rest if ch.isprintable())


def _from_colorfgbg():
    v = os.environ.get("COLORFGBG", "")  # set by rxvt, Konsole and others, e.g. "15;0"
    bg = v.split(";")[-1] if v else ""
    if bg.isdigit():
        return "light" if int(bg) in (7, 15) or 9 <= int(bg) <= 14 else "dark"
    return None


def _from_os():
    """The desktop's light/dark setting; only a hint about the terminal."""
    import subprocess
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=1)
            return "dark" if "dark" in r.stdout.lower() else "light"
        if os.name == "nt":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        if shutil.which("gsettings"):
            r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                               capture_output=True, text=True, timeout=1)
            if "dark" in r.stdout:
                return "dark"
            if "light" in r.stdout:
                return "light"
    except Exception:
        pass
    return None


def detect():
    """Return (mode, source, typeahead)."""
    global _osc_ok
    rgb, typed = query_background()
    _osc_ok = rgb is not None
    if rgb:
        return ("light" if _luma(rgb) > 0.5 else "dark"), "terminal background", typed
    mode = _from_colorfgbg()
    if mode:
        return mode, "COLORFGBG", typed
    mode = _from_os()
    if mode:
        return mode, "system appearance", typed
    return "dark", "default", typed


def set_theme(pref="auto"):
    """Pick the palette. pref is auto, dark or light; LOTUS_THEME overrides auto.
    Returns typeahead captured while querying the terminal."""
    global PREF, SOURCE
    pref = (pref or "auto").lower()
    env = os.environ.get("LOTUS_THEME", "").lower()
    if pref == "auto" and env in ("dark", "light"):
        pref = env
    PREF = pref
    if pref in ("dark", "light"):
        SOURCE = "set by you"
        apply(pref)
        return ""
    if not COLOR:
        SOURCE = "no color"
        return ""
    mode, SOURCE, typed = detect()
    apply(mode)
    return typed


def refresh():
    """Follow the terminal if its theme changed (e.g. the OS switched to dark at sunset).
    Cheap: one round trip, and skipped entirely if the terminal can't answer.
    Returns (changed, typeahead)."""
    if PREF != "auto" or not COLOR or not _osc_ok:
        return False, ""
    rgb, typed = query_background(timeout=0.2)
    if rgb is None:
        return False, typed
    mode = "light" if _luma(rgb) > 0.5 else "dark"
    changed = mode != MODE
    if changed:
        apply(mode)
    return changed, typed


G = {
    "bloom": "*" if ASCII else "❀",
    "bud": "*" if ASCII else "✿",
    "tool": "#" if ASCII else "⚙",
    "ok": "ok" if ASCII else "✓",
    "bad": "x" if ASCII else "✗",
    "sub": "->" if ASCII else "↳",
    "dot": "." if ASCII else "·",
    "stop": "--" if ASCII else "⊘",
    "web": "@" if ASCII else "◈",
}

# A bud opening into a lotus and closing again; drawn by the spinner.
BLOOM_FRAMES = list(".oO*Oo") if ASCII else ["·", "∘", "○", "✻", "❀", "✿", "❁", "✿", "❀", "✻", "○", "∘"]

# Status words for the spinner: (Sanskrit, what it means). Sanskrit leads, the gloss stays dim.
WAIT_WORDS = [("manana", "pondering"), ("vichāra", "reflecting"), ("chintana", "thinking"),
              ("bodha", "understanding"), ("smaraṇa", "recalling")]
THINK_WORD = ("dhyāna", "reasoning")
COMPACT_WORD = ("saṅgraha", "compacting")


def plain_word(s):
    """Drop diacritics in ASCII mode (vichāra -> vichara)."""
    if not ASCII:
        return s
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def _xterm(r, g, b):
    def q(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


def code(color, bg=False):
    if not COLOR:
        return ""
    r, g, b = PAL[color] if isinstance(color, str) else color
    layer = 48 if bg else 38
    if TRUECOLOR:
        return f"\033[{layer};2;{r};{g};{b}m"
    return f"\033[{layer};5;{_xterm(r, g, b)}m"


RESET = "\033[0m" if COLOR else ""
BOLD = "\033[1m" if COLOR else ""
ITALIC = "\033[3m" if COLOR else ""
DIM = "\033[2m" if COLOR else ""


def c(text, color="ink", bold=False, italic=False):
    if not COLOR:
        return str(text)
    return f"{code(color)}{BOLD if bold else ''}{ITALIC if italic else ''}{text}{RESET}"


def mix(a, b, t):
    ca = PAL[a] if isinstance(a, str) else a
    cb = PAL[b] if isinstance(b, str) else b
    return tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))


def gradient(text, a="petal", b="bloom"):
    if not COLOR:
        return text
    n = max(1, len(text) - 1)
    return "".join(code(mix(a, b, i / n)) + ch for i, ch in enumerate(text)) + RESET


def shimmer(text, t, a="mist", b="petal", speed=14.0, band=3.0):
    """A soft highlight that sweeps across the text, for animated status words."""
    if not COLOR:
        return text
    n = len(text)
    pos = (t * speed) % (n + 2 * band) - band
    out = []
    for i, ch in enumerate(text):
        k = max(0.0, 1.0 - abs(i - pos) / band)
        out.append(code(mix(a, b, k)) + ch)
    return "".join(out) + RESET


def breathe(t, a="petal", b="bloom", period=1.6):
    """A color that pulses slowly between two palette entries."""
    import math
    return mix(a, b, (math.sin(t * 2 * math.pi / period) + 1) / 2)


def rule(w, ch=None):
    """A divider that is faint at the edges and blushes toward the middle, like a petal."""
    ch = ch or ("-" if ASCII else "─")
    if not COLOR:
        return ch * w
    out, last = [], None
    for i in range(w):
        x = abs(i / max(1, w - 1) - 0.5) * 2  # 0 at the centre, 1 at the edges
        col = mix("bloom", "mist", min(1.0, 0.45 + x * 0.55))
        if col != last:
            out.append(code(col))
            last = col
        out.append(ch)
    return "".join(out) + RESET


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def vlen(s):
    total = 0
    for ch in strip_ansi(str(s)):
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return total


def width():
    return max(40, min(shutil.get_terminal_size((100, 24)).columns, 160))


def trunc(s, w):
    s = strip_ansi(str(s))
    if vlen(s) <= w:
        return s
    out, n = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if n + cw > w - 1:
            break
        out.append(ch)
        n += cw
    return "".join(out) + "…"


def pad(s, w):
    return s + " " * max(0, w - vlen(s))
