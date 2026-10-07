"""Interactive prompt with a live completion menu.

Type "/" and a menu of commands opens under the prompt and narrows as you type; arrows
pick, Tab completes, Enter runs. The same menu completes command arguments, model names
and @paths. Also: multi-line input (Alt+Enter or a trailing backslash), bracketed paste
that collapses big pastes into a placeholder, history with prefix search, and a footer
showing model and context use.

Stdlib only. POSIX terminals; cli.py falls back to input() elsewhere."""
import json
import os
import select
import shutil
import signal
import sys
import unicodedata
from collections import namedtuple

from .theme import BOLD, G, RESET, c, code, rule as petal_rule, trunc, vlen

Item = namedtuple("Item", "insert label desc run")  # run: Enter accepts and submits at once
SUBMIT = object()
PASTE_RE_FMT = "[pasted #{n} +{lines} lines]"
MENU_ROWS = 8


def available():
    if os.name == "nt" or os.environ.get("LOTUS_SIMPLE_INPUT") or os.environ.get("TERM") == "dumb":
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import termios  # noqa: F401
        return True
    except ImportError:
        return False


def _cw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


CSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift-tab",
            "1~": "home", "7~": "home", "4~": "end", "8~": "end", "3~": "delete", "5~": "pgup", "6~": "pgdn",
            "1;5C": "word-right", "1;3C": "word-right", "1;5D": "word-left", "1;3D": "word-left",
            "13;2u": "newline", "13;3u": "newline", "27;2;13~": "newline", "27;5;13~": "newline", "200~": "paste"}
CTRL_KEYS = {"\x01": "home", "\x05": "end", "\x02": "left", "\x06": "right", "\x10": "up", "\x0e": "down",
             "\x7f": "backspace", "\x08": "backspace", "\x17": "word-back", "\x15": "kill-left", "\x0b": "kill-right",
             "\x0c": "redraw", "\x03": "ctrl-c", "\x04": "ctrl-d", "\x1a": "suspend", "\t": "tab",
             "\r": "enter", "\n": "enter"}


class Editor:
    def __init__(self, history_file, menu_fn=None, footer_fn=None, glyph=None):
        self.hist_path = history_file
        self.history = self._load()
        self.menu_fn, self.footer_fn = menu_fn, footer_fn
        self.glyph = glyph or (G["bloom"] + " ")
        self.q = b""

    # ── history ──────────────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.hist_path, encoding="utf-8") as f:
                items = [json.loads(l) for l in f if l.strip()]
        except (OSError, ValueError):
            return []
        if len(items) > 2000:
            items = items[-1000:]
            try:
                with open(self.hist_path, "w", encoding="utf-8") as f:
                    f.writelines(json.dumps(x) + "\n" for x in items)
            except OSError:
                pass
        return items

    def _remember(self, entry):
        if not entry.strip() or (self.history and self.history[-1] == entry):
            return
        self.history.append(entry)
        try:
            with open(self.hist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    # ── keys ─────────────────────────────────────────────────────────────────
    def _fill(self, fd, wait=None):
        r, _, _ = select.select([fd], [], [], wait)
        if r:
            self.q += os.read(fd, 4096)
        return bool(r)

    def _key(self, fd):
        while not self.q:
            self._fill(fd)
        b = self.q[0]
        if b == 0x1b:
            if len(self.q) == 1 and not self._fill(fd, 0.03):
                self.q = b""
                return ("esc", "")
            nxt = self.q[1:2]
            if nxt in (b"[", b"O"):
                i = 2
                while True:
                    while i >= len(self.q):
                        if not self._fill(fd, 0.05):
                            self.q = b""
                            return ("esc", "")
                    if 0x40 <= self.q[i] <= 0x7e:
                        break
                    i += 1
                seq = self.q[2:i + 1].decode("latin-1")
                self.q = self.q[i + 1:]
                name = CSI_KEYS.get(seq) or CSI_KEYS.get(seq[-1:] if seq[:-1] in ("", "1") else "", "")
                if name == "paste":
                    return ("paste", self._paste(fd))
                return (name or "unknown", seq)
            self.q = self.q[2:]
            alt = {b"\r": "newline", b"\n": "newline", b"\x7f": "word-back", b"b": "word-left", b"f": "word-right"}
            return (alt.get(nxt, "unknown"), "")
        if b < 0x20 or b == 0x7f:
            self.q = self.q[1:]
            return (CTRL_KEYS.get(chr(b), "unknown"), "")
        n = 1 if b < 0x80 else 2 if b >> 5 == 0b110 else 3 if b >> 4 == 0b1110 else 4
        while len(self.q) < n and self._fill(fd, 0.05):
            pass
        ch, self.q = self.q[:n].decode("utf-8", "replace"), self.q[n:]
        return ("char", ch)

    def _paste(self, fd):
        end = b"\x1b[201~"
        while end not in self.q:
            if not self._fill(fd, 2.0):
                break
        data, _, self.q = self.q.partition(end)
        return data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")

    # ── editing ──────────────────────────────────────────────────────────────
    def _insert(self, s):
        self.buf = self.buf[:self.pos] + s + self.buf[self.pos:]
        self.pos += len(s)

    def _word_left(self):
        i = self.pos
        while i > 0 and not self.buf[i - 1].isalnum():
            i -= 1
        while i > 0 and self.buf[i - 1].isalnum():
            i -= 1
        return i

    def _word_right(self):
        i, n = self.pos, len(self.buf)
        while i < n and not self.buf[i].isalnum():
            i += 1
        while i < n and self.buf[i].isalnum():
            i += 1
        return i

    def _line_bounds(self):
        start = self.buf.rfind("\n", 0, self.pos) + 1
        end = self.buf.find("\n", self.pos)
        return start, len(self.buf) if end < 0 else end

    def _vertical(self, d):
        """Move between lines of a multi-line buffer. False when there is no line that way."""
        start, end = self._line_bounds()
        col = self.pos - start
        if d < 0:
            if start == 0:
                return False
            pstart = self.buf.rfind("\n", 0, start - 1) + 1
            self.pos = min(pstart + col, start - 1)
        else:
            if end == len(self.buf):
                return False
            nend = self.buf.find("\n", end + 1)
            nend = len(self.buf) if nend < 0 else nend
            self.pos = min(end + 1 + col, nend)
        return True

    def _history(self, d):
        if self.hidx == len(self.history):
            self.draft = self.buf
        prefix = self.draft if self.draft and not self.draft.startswith("/") else ""
        i = self.hidx
        while True:
            i += d
            if i < 0:
                return
            if i >= len(self.history):
                self.hidx, self.buf = len(self.history), self.draft or ""
                break
            if self.history[i].startswith(prefix) and self.history[i] != self.buf:
                self.hidx, self.buf = i, self.history[i]
                break
        self.pos = len(self.buf)

    # ── menu ─────────────────────────────────────────────────────────────────
    def _menu(self):
        if self.menu_hidden or not self.menu_fn:
            return None
        key = (self.buf, self.pos)
        if self._menu_key != key:
            self._menu_key = key
            try:
                self._menu_val = self.menu_fn(self.buf, self.pos)
            except Exception:
                self._menu_val = None
            if not self._menu_val or not self._menu_val[1]:
                self._menu_val = None
            self.sel = 0
        return self._menu_val

    def _accept(self, menu):
        start, items = menu
        it = items[min(self.sel, len(items) - 1)]
        self.buf = self.buf[:start] + it.insert + self.buf[self.pos:]
        self.pos = start + len(it.insert)
        return it

    # ── drawing ──────────────────────────────────────────────────────────────
    def _style_at(self, i):
        b = self.buf
        if b.startswith("/"):
            sp = b.find(" ")
            if sp < 0 or i < sp:
                return "cmd"
        elif b.startswith("!"):
            return "shell"
        for s, e in self._paste_spans:
            if s <= i < e:
                return "paste"
        return "text"

    STYLES = {"cmd": ("petal", True), "shell": ("stamen", False), "paste": ("mist", False), "text": ("ink", False)}

    def _layout(self, w, pw):
        rows, col, cursor = [[]], pw, None
        for i, ch in enumerate(self.buf):
            if ch == "\n":
                if i == self.pos:
                    cursor = (len(rows) - 1, col)
                rows.append([])
                col = pw
                continue
            wch = _cw(ch)
            if col + wch > w:
                rows.append([])
                col = pw
            if i == self.pos:
                cursor = (len(rows) - 1, col)
            rows[-1].append((ch, i))
            col += wch
        if cursor is None:
            if col >= w:
                rows.append([])
                col = pw
            cursor = (len(rows) - 1, col)
        return rows, cursor

    def _paint(self, row):
        out, cur = [], None
        for ch, i in row:
            st = self._style_at(i)
            if st != cur:
                color, bold = self.STYLES[st]
                out.append(RESET + code(color) + (BOLD if bold else ""))
                cur = st
            out.append(ch)
        return "".join(out) + RESET

    def _render(self, final=False):
        cols = shutil.get_terminal_size((100, 24)).columns
        w = max(20, cols - 1)
        pw = vlen(self.glyph)
        self._paste_spans = []
        for key in self.pastes:
            j = self.buf.find(key)
            if j >= 0:
                self._paste_spans.append((j, j + len(key)))
        rows, (cr, cc) = self._layout(w, pw)
        glyph = c(self.glyph, "stamen" if self.buf.startswith("!") else "bloom")
        body = [(glyph if k == 0 else " " * pw) + self._paint(r) for k, r in enumerate(rows)]
        if final:
            lines, cursor_line, cc = body, len(body) - 1, None
        else:
            rule = petal_rule(w)
            lines = [rule] + body + [rule]
            cursor_line = 1 + cr
            menu = self._menu()
            if menu:
                lines += self._menu_lines(menu, w)
            else:
                lines.append(self._footer(w))
        s = "\r" + (f"\033[{self.cur_line}A" if self.cur_line else "") + "\033[J" + "\n".join(lines)
        up = len(lines) - 1 - cursor_line
        if up:
            s += f"\033[{up}A"
        if cc is not None:
            s += "\r" + (f"\033[{cc}C" if cc else "")
        self.cur_line = cursor_line
        _out(s)

    def _menu_lines(self, menu, w):
        _, items = menu
        n = len(items)
        top = max(0, min(self.sel - MENU_ROWS // 2, n - MENU_ROWS))
        shown = items[top:top + MENU_ROWS]
        lw = min(max(vlen(it.label) for it in shown) + 2, max(12, w // 2))
        out = []
        for k, it in enumerate(shown):
            on = top + k == self.sel
            mark = c(G["sub"] + " ", "bloom") if on else "  "
            label = c(trunc(it.label, lw - 1).ljust(lw), "petal" if on else "ink", bold=on)
            room = w - lw - 4
            desc = c(trunc(it.desc, room), "ink" if on else "mist") if room > 8 and it.desc else ""
            out.append(" " + mark + label + desc)
        if n > MENU_ROWS:
            out.append(c(f"   {self.sel + 1}/{n}  ↑↓ to see more", "mist"))
        return out

    def _footer(self, w):
        if self.buf:
            hint = "⏎ send · alt+⏎ or \\⏎ newline · esc clear"
        else:
            hint = "/ commands · @ files · ! shell · ↑ history"
        right = ""
        if self.footer_fn:
            try:
                right = self.footer_fn() or ""
            except Exception:
                right = ""
        gap = w - 2 - vlen(hint) - vlen(right)
        if gap < 2:
            return "  " + c(trunc(right or hint, w - 2), "mist")
        return "  " + c(hint, "mist") + " " * gap + right

    # ── main loop ────────────────────────────────────────────────────────────
    def read(self, prefill=""):
        """Read one entry. Raises EOFError (Ctrl+D on an empty line) or KeyboardInterrupt."""
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[0] &= ~(termios.ICRNL | termios.IXON)
        new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
        self.buf, self.pos = prefill, len(prefill)
        self.hidx, self.draft = len(self.history), None
        self.sel, self.cur_line, self.menu_hidden = 0, 0, False
        self._menu_key = self._menu_val = None
        self.pastes, self._paste_spans = {}, []

        def enter():
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            _out("\033[?2004h")

        def leave():
            _out("\033[?2004l")
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        enter()
        try:
            self._render()
            while True:
                kind, val = self._key(fd)
                res = self._on(kind, val)
                if res is SUBMIT:
                    break
                if res == "suspend":
                    leave()
                    os.kill(0, signal.SIGTSTP)
                    enter()
                    self.cur_line = 0
                self._render()
            self._render(final=True)
            _out("\n")
        except (KeyboardInterrupt, EOFError):
            self._render(final=True)
            _out("\n")
            raise
        finally:
            leave()
        text = self.buf
        if self.buf.strip():
            self._remember(self.buf)
        for key, body in self.pastes.items():
            text = text.replace(key, body)
        return text

    def _on(self, kind, val):
        menu = self._menu()
        if kind not in ("up", "down", "tab", "shift-tab", "enter", "esc"):
            self.menu_hidden = False
        if kind == "char":
            self._insert(val)
        elif kind == "paste":
            lines = val.count("\n") + 1
            if len(val) > 1000 or lines > 12:
                key = PASTE_RE_FMT.format(n=len(self.pastes) + 1, lines=lines)
                self.pastes[key] = val
                self._insert(key)
            else:
                self._insert(val)
        elif kind == "enter":
            if menu:
                before = self.buf
                it = self._accept(menu)
                if not it.run and self.buf != before:  # needs more input (an argument, or a folder to open)
                    return None
                self.buf = self.buf.rstrip(" ")
                self.pos = len(self.buf)
                return SUBMIT
            if self.buf[:self.pos].endswith("\\"):
                self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
                self.pos -= 1
                self._insert("\n")
                return None
            return SUBMIT
        elif kind == "newline":
            self._insert("\n")
        elif kind == "tab":
            if menu:
                self._accept(menu)
        elif kind in ("up", "shift-tab") and menu:
            self.sel = (self.sel - 1) % len(menu[1])
        elif kind == "down" and menu:
            self.sel = (self.sel + 1) % len(menu[1])
        elif kind == "up":
            if not self._vertical(-1):
                self._history(-1)
        elif kind == "down":
            if not self._vertical(1):
                self._history(1)
        elif kind == "esc":
            if menu:
                self.menu_hidden = True
            else:
                self.buf, self.pos = "", 0
        elif kind == "left":
            self.pos = max(0, self.pos - 1)
        elif kind == "right":
            self.pos = min(len(self.buf), self.pos + 1)
        elif kind == "home":
            self.pos = self._line_bounds()[0]
        elif kind == "end":
            self.pos = self._line_bounds()[1]
        elif kind == "word-left":
            self.pos = self._word_left()
        elif kind == "word-right":
            self.pos = self._word_right()
        elif kind == "backspace":
            if self.pos:
                for key in self.pastes:  # a placeholder goes in one keystroke
                    if self.buf[:self.pos].endswith(key):
                        self.buf = self.buf[:self.pos - len(key)] + self.buf[self.pos:]
                        self.pos -= len(key)
                        break
                else:
                    self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
                    self.pos -= 1
        elif kind == "delete":
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif kind == "word-back":
            i = self._word_left()
            self.buf, self.pos = self.buf[:i] + self.buf[self.pos:], i
        elif kind == "kill-left":
            s, _ = self._line_bounds()
            self.buf, self.pos = self.buf[:s] + self.buf[self.pos:], s
        elif kind == "kill-right":
            _, e = self._line_bounds()
            self.buf = self.buf[:self.pos] + self.buf[e:]
        elif kind == "redraw":
            _out("\033[H\033[2J")
            self.cur_line = 0
        elif kind == "ctrl-c":
            if self.buf:
                self.buf, self.pos = "", 0
            else:
                raise KeyboardInterrupt
        elif kind == "ctrl-d":
            if not self.buf:
                raise EOFError
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif kind == "suspend":
            return "suspend"
        return None
