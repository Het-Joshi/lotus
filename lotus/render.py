"""Output layer: a streaming markdown renderer (headings, lists, code, tables, charts)
and the UI object the agent talks to. Output is line-oriented; the only cursor tricks are
the one status line at the bottom (the spinner) and the banner's opening animation."""
import contextlib
import json
import random
import re
import sys
import threading
import time

from .theme import (ASCII, BLOOM_FRAMES, BOLD, COLOR, G, RESET, THINK_WORD, WAIT_WORDS, breathe, c,
                    code, gradient, pad, plain_word, shimmer, strip_ansi, trunc, vlen, width)

SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [x.strip().replace("**", "").replace("`", "") for x in s.split("|")]


def table_lines(rows, header=True):
    rows = [[str(x) for x in r] if isinstance(r, (list, tuple)) else [str(r)] for r in rows]
    if not rows:
        return []
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = [max(vlen(r[i]) for r in rows) for i in range(ncol)]
    avail = width() - 2
    while sum(widths) + 3 * ncol + 1 > avail and max(widths) > 6:
        widths[widths.index(max(widths))] -= 1
    b = lambda s: c(s, "bloom")
    top = b("╭" + "┬".join("─" * (w + 2) for w in widths) + "╮")
    mid = b("├" + "┼".join("─" * (w + 2) for w in widths) + "┤")
    bot = b("╰" + "┴".join("─" * (w + 2) for w in widths) + "╯")
    out = [top]
    for i, r in enumerate(rows):
        is_head = header and i == 0
        cells = [c(pad(trunc(x, w), w), "petal" if is_head else "ink", bold=is_head) for x, w in zip(r, widths)]
        out.append(b("│ ") + b(" │ ").join(cells) + b(" │"))
        if is_head and len(rows) > 1:
            out.append(mid)
    out.append(bot)
    return out


def md_table_lines(lines):
    rows = [_cells(l) for l in lines if not SEP_RE.match(l)]
    has_head = len(lines) > 1 and SEP_RE.match(lines[1]) is not None
    return table_lines(rows, header=has_head)


class Inline:
    """Streams **bold** and `code` styling across chunk boundaries."""

    def __init__(self):
        self.bold = self.code = False
        self.pending = ""

    def base(self):
        if self.code:
            return code("stamen")
        return (code("petal") + BOLD) if self.bold else code("ink")

    def style(self, text, final):
        text, self.pending = self.pending + text, ""
        if not final and text.endswith("*") and not self.code:
            k = len(text) - len(text.rstrip("*"))
            self.pending, text = text[-k:], text[:-k]
        if not COLOR:
            return text.replace("**", "") if not self.code else text
        out, i = [self.base()], 0
        while i < len(text):
            if not self.code and text.startswith("**", i):
                self.bold = not self.bold
                out.append(RESET + self.base())
                i += 2
                continue
            if text[i] == "`":
                self.code = not self.code
                out.append(RESET + self.base())
                i += 1
                continue
            out.append(text[i])
            i += 1
        out.append(RESET)
        return "".join(out)

    def end_line(self):
        self.bold = self.code = False
        tail, self.pending = self.pending, ""
        return tail


class StreamRenderer:
    SPECIAL = ("chart", "graph", "table")

    def __init__(self, ui):
        self.ui = ui
        self.buf = ""
        self.mode = "start"
        self.block = []
        self.lang = ""
        self.inl = Inline()

    def w(self, s):
        self.ui.write(s)

    def feed(self, text):
        self.buf += text
        self._run(final=False)

    def close(self):
        self._run(final=True)
        if self.mode == "prose":
            self.w(self.inl.style(self.buf, True) + self.inl.end_line() + "\n")
        elif self.mode == "table":
            self._end_table()
        elif self.mode == "fence":
            self._end_fence()
        elif self.buf.strip():
            self.w(self.inl.style(self.buf, True) + "\n")
        self.buf, self.mode = "", "start"

    def _run(self, final):
        while self.buf:
            nl = self.buf.find("\n")
            head = self.buf if nl < 0 else self.buf[:nl]
            s = head.lstrip()
            if self.mode == "start":
                if nl < 0 and len(head) < 4 and not final:
                    return
                if s.startswith("```"):
                    if nl < 0 and not final:
                        return
                    self.lang = s[3:].strip().lower()
                    self.block = []
                    self.mode = "fence"
                    self.buf = self.buf[nl + 1:] if nl >= 0 else ""
                    if self.lang not in self.SPECIAL:
                        self.w(c("╭─ " + (self.lang or "code"), "mist") + "\n")
                    continue
                if s.startswith("|"):
                    if nl < 0 and not final:
                        return
                    self.mode = "table"
                    self.block = []
                    continue
                if s.startswith("#"):
                    if nl < 0 and not final:
                        return
                    level = len(s) - len(s.lstrip("#"))
                    txt = s.lstrip("#").strip().replace("**", "")
                    self.w(("\n" if level <= 2 else "") + (gradient(txt) if level == 1 else c(txt, "petal", bold=True)) + "\n")
                    self.buf = self.buf[nl + 1:] if nl >= 0 else ""
                    continue
                if nl == 0:
                    self.w("\n")
                    self.buf = self.buf[1:]
                    continue
                if re.match(r"^\s*([-*_]\s*){3,}$", head) and nl >= 0:
                    self.w(c("─" * min(40, width() - 4), "mist") + "\n")
                    self.buf = self.buf[nl + 1:]
                    continue
                m = re.match(r"(\s*)([-*+]|\d+[.)])\s+", head)
                if m:
                    ind = " " * len(m.group(1))
                    mark = m.group(2)
                    glyph = c(G["bud"] if mark in "-*+" else mark, "bloom")
                    self.w(ind + glyph + " ")
                    self.buf = self.buf[m.end():]
                    self.mode = "prose"
                    continue
                if s.startswith(">"):
                    self.w(c("│ ", "pond"))
                    self.buf = self.buf[len(head) - len(s) + 1:].lstrip(" ")
                    self.mode = "prose"
                    continue
                self.mode = "prose"
                continue
            if self.mode == "prose":
                chunk = self.buf if nl < 0 else self.buf[:nl]
                done = nl >= 0
                self.w(self.inl.style(chunk, done))
                if done:
                    self.w(self.inl.end_line() + "\n")
                    self.buf = self.buf[nl + 1:]
                    self.mode = "start"
                else:
                    self.buf = ""
                    return
                continue
            if self.mode == "fence":
                if nl < 0:
                    if final and self.buf:
                        head, self.buf = self.buf, ""
                    else:
                        return
                else:
                    self.buf = self.buf[nl + 1:]
                if head.strip().startswith("```"):
                    self._end_fence()
                    continue
                if self.lang in self.SPECIAL:
                    self.block.append(head)
                else:
                    self.w(c("│ ", "mist") + c(head, "stamen") + "\n")
                continue
            if self.mode == "table":
                if s.startswith("|"):
                    if nl < 0 and not final:
                        return
                    self.block.append(head)
                    self.buf = self.buf[nl + 1:] if nl >= 0 else ""
                    continue
                if not self.buf.strip() and not final:
                    return
                self._end_table()
                continue

    def _end_table(self):
        for l in md_table_lines(self.block):
            self.w(l + "\n")
        self.block, self.mode = [], "start"

    def _end_fence(self):
        if self.lang in self.SPECIAL:
            from .charts import render
            from .textcalls import repair_json
            raw = "\n".join(self.block)
            spec = repair_json(raw)
            if spec is None and self.lang == "table":
                lines = md_table_lines([l for l in self.block if l.strip()])
            elif spec is None:
                lines = [c("could not parse chart JSON", "thorn")]
            else:
                if self.lang in ("graph", "table") and "type" not in spec:
                    spec["type"] = self.lang
                lines = render(spec)
            for l in lines:
                self.w(l + "\n")
        else:
            self.w(c("╰─", "mist") + "\n")
        self.block, self.mode, self.lang = [], "start", ""


class UI:
    """Everything the agent prints goes through here.

    Besides plain lines there is one ephemeral status line at the bottom: a blooming lotus
    with a word and a timer while the model thinks or a tool runs. Any other output
    erases it first and the spinner redraws it underneath, so it never mixes with text."""
    TICK = 0.09

    def __init__(self, plain=None):
        self.plain = (not sys.stdout.isatty()) if plain is None else plain
        self.lock = threading.RLock()
        self.show_thinking = True
        self.cancel = threading.Event()
        self.watch = None
        self.typeahead, self.queued = "", []  # what was typed during the last turn
        self._status = None        # (word, gloss, started) while something is in progress
        self._drawn = False        # the status line is on screen
        self._spin = None
        self._thinking = False
        self._think_hidden = False
        self._think_bol = True
        self._bol = True
        self._word = random.randrange(len(WAIT_WORDS))
        self.meta_stream = sys.stderr if self.plain else sys.stdout

    # ── raw output ───────────────────────────────────────────────────────────
    def _erase(self):
        if self._drawn:
            sys.stdout.write("\r\033[K")
            self._drawn = False

    def _w(self, s, stream=None):
        stream = stream or sys.stdout
        with self.lock:
            self._erase()
            stream.write(s)
            stream.flush()
            if stream is sys.stdout and s:
                self._bol = s.endswith("\n")

    def write(self, s):
        """Streamed reply text. It replaces the status line rather than sitting above it."""
        with self.lock:
            if s:
                self._status = None
            self._w(s)

    def meta(self, s):
        with self.lock:
            self._erase()
            if self.meta_stream is sys.stdout and not self._bol:
                self._w("\n")
            self._w(s + "\n", self.meta_stream)

    def info(self, s):
        self.meta(c("  " + s, "mist"))

    def ok(self, s):
        self.meta(c(f"  {G['ok']} ", "leaf") + c(s, "ink"))

    def warn(self, s):
        self.meta(c(f"  ! {s}", "stamen"))

    def error(self, s):
        self.meta(c(f"  {G['bad']} {s}", "thorn"))

    # ── the status line ──────────────────────────────────────────────────────
    def status(self, word, gloss=""):
        """Show (or replace) the animated status line. None clears it."""
        if self.plain:
            return
        with self.lock:
            if word is None:
                self._status = None
                self._erase()
                sys.stdout.flush()
                return
            if not self._bol:
                self._w("\n")
            keep = self._status[2] if self._status and self._status[0] == word else time.monotonic()
            self._status = (word, gloss, keep)
            self._draw()
        if self._spin is None or not self._spin.is_alive():
            self._spin = threading.Thread(target=self._spinner, name="lotus-spin", daemon=True)
            self._spin.start()

    def _spinner(self):
        while True:
            time.sleep(self.TICK)
            with self.lock:
                if self._status is None:
                    self._spin = None
                    return
                self._draw()

    def _draw(self):
        word, gloss, t0 = self._status
        now = time.monotonic()
        el = now - t0
        frame = BLOOM_FRAMES[int(now / self.TICK) % len(BLOOM_FRAMES)]
        if word == "…wait":  # the model is warming up: rotate through the waiting words
            sw, gl = WAIT_WORDS[(self._word + int(el // 6)) % len(WAIT_WORDS)]
        else:
            sw, gl = word, gloss
        sw = plain_word(sw)
        parts = [c(frame, breathe(now)) + " " + shimmer(sw, now)]
        if gl:
            parts.append(c(gl, "mist"))
        tail = [f"{el:.0f}s" if el < 60 else f"{int(el // 60)}m {int(el % 60):02d}s"]
        if self.watch is not None:
            tail.append("esc to stop")
        parts.append(c(" · ".join(tail), "mist"))
        line = "  " + c(" · ", "mist").join(parts)
        if self.watch is not None and (self.watch.typed or self.watch.queued):
            q = len(self.watch.queued)
            typed = c(f"{G['sub']} ", "pond") + c(self.watch.typed or "", "ink") + c("▏", "bloom")
            if q:
                typed += c(f"  ({q} queued)", "pond")
            line += "   " + typed
        room = width() - 1
        if vlen(line) > room:
            line = "  " + trunc(strip_ansi(line).strip(), room - 2)
            line = c(line, "mist")
        sys.stdout.write("\r\033[K" + line)
        sys.stdout.flush()
        self._drawn = True

    def wait(self):
        """The model is reading the prompt; nothing has streamed yet."""
        if self.plain:
            return
        self._word = random.randrange(len(WAIT_WORDS))
        self.status("…wait")

    # ── reasoning ────────────────────────────────────────────────────────────
    def think(self, text):
        if self.plain or not text:
            return
        with self.lock:
            if not self.show_thinking:
                if not self._thinking:
                    self._thinking, self._think_hidden = True, True
                    self.status(*THINK_WORD)
                return
            self._erase()
            self._status = None
            if not self._thinking:
                if not self._bol:
                    self._w("\n")
                self._w(c("  ┊ ", "petal") + c(plain_word(THINK_WORD[0]), "petal", italic=True)
                        + c(" · " + THINK_WORD[1], "mist", italic=True) + "\n")
                self._thinking, self._think_hidden, self._think_bol = True, False, True
            out = []
            for i, part in enumerate(text.split("\n")):
                if i > 0:
                    out.append("\n")
                    self._think_bol = True
                if part:
                    if self._think_bol:
                        out.append(c("  ┊ ", "petal"))
                        self._think_bol = False
                    out.append(c(part, "mist", italic=True))
            self._w("".join(out))

    def think_end(self):
        with self.lock:
            if not self._thinking:
                return
            if self._think_hidden:
                self.status(None)
            else:
                self._w(("" if self._think_bol else "\n") + "\n")
            self._thinking = False

    # ── tools ────────────────────────────────────────────────────────────────
    def tool_start(self, name, preview, depth=0):
        """A tool began: show it live with a timer until tool() prints the result line."""
        if self.plain or depth:
            return
        room = max(10, width() - len(name) - 34)
        self.status(name, trunc(preview.replace("\n", " "), room) if preview else "")

    def tool(self, name, preview, ok, secs, depth=0, label="", note=""):
        with self.lock:
            if not depth and self._status and self._status[0] == name:
                self._status = None
            ind = "  " + "  " * depth + (c(G["sub"] + " " + label + " ", "pond") if depth else "")
            mark = c(G["ok"], "leaf") if ok else c(G["bad"], "thorn")
            if ok is None:
                mark = c(G["stop"], "stamen")
            room = max(10, width() - vlen(strip_ansi(ind)) - len(name) - 16 - (vlen(note) + 3 if note else 0))
            line = f"{ind}{c(G['tool'], 'stamen')} {c(name, 'petal', bold=True)} {c(trunc(preview.replace(chr(10), ' '), room), 'mist')} {mark} {c(f'{secs:.1f}s', 'mist')}"
            if note:
                line += c(f"  {G['sub']} ", "pond") + c(trunc(note, max(10, width() - vlen(line) - 6)), "pond")
            self.meta(line)

    def interrupted(self, what="stopped"):
        self.meta(c(f"  {G['stop']} {what}", "stamen") + c(" · tell lotus what to do instead, or press enter to carry on", "mist"))

    def block(self, lines):
        with self.lock:
            self._erase()
            if not self._bol:
                self._w("\n")
            for l in lines:
                self._w(l + "\n")

    def diff(self, old, new, limit=12):
        rows = [c("  - " + l, "thorn") for l in old.splitlines()[:limit]]
        rows += [c("  + " + l, "leaf") for l in new.splitlines()[:limit]]
        self.block(rows)

    # ── keys ─────────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def watching(self):
        """Watch the keyboard for Esc and type-ahead while a turn runs."""
        from . import keys
        if self.plain or self.watch is not None or not keys.available():
            yield None
            return
        w = keys.Watch(on_change=self._redraw)
        self.watch = w
        try:
            with w:
                yield w
        finally:
            self.watch = None
            self.typeahead, self.queued = w.typed, list(w.queued)

    def _redraw(self):
        with self.lock:
            if self._status is not None:
                self._draw()

    def confirm(self, question, detail=""):
        """Ask before a tool changes something. Returns y (once), a (always), n (no), or x (stop the turn)."""
        from . import keys
        with self.lock:
            saved, self._status = self._status, None
            self._erase()
            if not self._bol:
                self._w("\n")
            body = [l for l in (detail or "").splitlines() if l.strip()][:8]
            self._w(c("  ╭─ ", "stamen") + c("approve ", "stamen", bold=True) + c(question, "ink") + "\n")
            for l in body:
                self._w(c("  │ ", "stamen") + c(trunc(l, width() - 6), "mist") + "\n")
            keys_hint = (c("y", "leaf", bold=True) + c(" yes · ", "mist") + c("a", "leaf", bold=True) + c(" always · ", "mist")
                         + c("n", "thorn", bold=True) + c(" no · ", "mist") + c("esc", "stamen", bold=True) + c(" stop", "mist"))
            self._w(c("  ╰─ ", "stamen") + keys_hint + " ")
            try:
                if self.watch is not None:
                    with self.watch.paused() as fd:
                        k = keys.read_key(fd, abort=self.cancel.is_set)
                elif keys.available():
                    k = keys.ask_key(abort=self.cancel.is_set)
                else:
                    k = (input().strip().lower() or "n")[:1]
            except (EOFError, KeyboardInterrupt):
                k = "esc"
            ans = {"y": "y", "a": "a", "esc": "x", None: "x"}.get(k, "n")
            verdict = {"y": c(f"{G['ok']} allowed", "leaf"), "a": c(f"{G['ok']} allowed from now on", "leaf"),
                       "n": c(f"{G['bad']} declined", "thorn"), "x": c(f"{G['stop']} stopped", "stamen")}[ans]
            self._w("\r\033[K" + c("  ╰─ ", "stamen") + verdict + "\n")
            if ans != "x":
                self._status = saved
            return ans


class SubUI(UI):
    """Quiet UI for sub-agents: only tool activity and renders reach the screen."""

    def __init__(self, parent, label):
        super().__init__(plain=parent.plain)
        self.parent, self.label = parent, label
        self.lock = parent.lock
        self.cancel = parent.cancel

    def write(self, s):
        pass

    def think(self, text):
        pass

    def think_end(self):
        pass

    def wait(self):
        pass

    def status(self, word, gloss=""):
        pass

    def tool_start(self, name, preview, depth=0):
        pass

    def tool(self, name, preview, ok, secs, depth=0, label="", note=""):
        self.parent.tool(name, preview, ok, secs, depth=max(1, depth), label=self.label, note=note)

    def interrupted(self, what="stopped"):
        pass

    def meta(self, s):
        self.parent.meta(s)

    def block(self, lines):
        self.parent.block(lines)

    @contextlib.contextmanager
    def watching(self):
        yield None

    def confirm(self, question, detail=""):
        return self.parent.confirm(f"[{self.label}] {question}", detail)


LOTUS_ART = [
    "            ,",
    "         .-/ \\-.",
    "     .-.( (   ) ).-.",
    "    (   \\ \\   / /   )",
    "     '-._\\_\\_/_/_.-'",
]


def _banner_lines(version, subtitle="", reveal=None, t=0.0):
    """The logo. reveal=k shows only the bottom k rows of the flower (it rises from the
    water); t shifts the ripples and the light across the petals."""
    rows = len(LOTUS_ART)
    k = rows if reveal is None else reveal
    lines = [""]
    for i, row in enumerate(LOTUS_ART):
        if i < rows - k:
            lines.append("")
            continue
        a, b = ("petal", "bloom") if i < 4 else ("bloom", "petal")
        lines.append("  " + (shimmer(row, t, a, "ink", speed=40, band=5) if reveal is not None else gradient(row, a, b)))
    wave = "~ " if ASCII else "∽ "
    if reveal is None:
        water = c("~" * 26, "pond")
    else:
        n = int(t * 12) % 2
        water = c((wave * 14)[n:n + 26], "pond")
    lines.append("  " + water)
    dot = c(" · ", "mist")
    name = c("lotus", "petal", bold=True) + c(f" {version}", "mist")
    word = "" if ASCII else dot + c("कमल", "bloom")
    lines.append("  " + name + word + dot + c(subtitle or "a local agent that fits small models", "mist"))
    lines.append("")
    return lines


def banner(version, subtitle=""):
    return "\n".join(_banner_lines(version, subtitle))


def banner_animated(version, subtitle=""):
    """Write the banner, letting the lotus rise out of the water first. Falls back to the
    still banner when output isn't an interactive color terminal or LOTUS_NO_ANIM is set."""
    import os
    still = banner(version, subtitle)
    if not (COLOR and sys.stdout.isatty()) or os.environ.get("LOTUS_NO_ANIM"):
        sys.stdout.write(still + "\n")
        return
    n = len(_banner_lines(version, subtitle))
    sys.stdout.write("\033[?25l")  # hide the cursor while drawing
    try:
        frames = list(range(0, len(LOTUS_ART) + 1)) + [len(LOTUS_ART)] * 6
        for f, k in enumerate(frames):
            if f:
                sys.stdout.write(f"\033[{n - 1}A\r")
            sys.stdout.write("\n".join("\033[K" + l for l in _banner_lines(version, subtitle, reveal=k, t=f * 0.07)))
            sys.stdout.flush()
            time.sleep(0.055)
        sys.stdout.write(f"\033[{n - 1}A\r" + "\n".join("\033[K" + l for l in still.split("\n")) + "\n")
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def petal_meter(frac, slots=8):
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * slots)
    color = "bloom" if frac < 0.75 else "stamen" if frac < 0.9 else "thorn"
    return c(G["bloom"] * filled, color) + c(G["dot"] * (slots - filled), "mist")
