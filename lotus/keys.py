"""Staying in control while the agent works.

During a turn the terminal is put in cbreak mode and a small thread watches the keys:
- Esc stops the turn wherever it is: the reply stream is closed, a running shell command
  or browser action is abandoned, sub-agents are cancelled. Ctrl+C does the same.
- Anything you type is kept. Enter queues it as your next message; an unfinished line
  is put back into the prompt when the turn ends.

Approval prompts read single keys through the same watcher, so the two never fight over
stdin. POSIX only; elsewhere Ctrl+C still works through the normal SIGINT."""
import codecs
import contextlib
import os
import select
import signal
import sys
import threading
import time


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


def _cbreak(fd):
    """Raw enough to see single keys, but Ctrl+C still raises SIGINT."""
    import termios
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[0] &= ~(termios.ICRNL | termios.IXON)
    new[3] &= ~(termios.ICANON | termios.ECHO | termios.IEXTEN)
    new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
    termios.tcsetattr(fd, termios.TCSANOW, new)
    return old


def _restore(fd, old):
    import termios
    try:
        termios.tcsetattr(fd, termios.TCSANOW, old)
    except termios.error:
        pass


def read_key(fd, abort=None):
    """One keypress from a terminal already in cbreak mode: a character, 'enter' or 'esc'.
    Polls so that abort() (e.g. a cancelled turn) can end the wait. Returns None on abort."""
    while True:
        if abort and abort():
            return None
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            continue
        b = os.read(fd, 1)
        if b == b"\x1b":
            r, _, _ = select.select([fd], [], [], 0.03)
            if r:  # an escape sequence (arrow key); swallow it
                os.read(fd, 32)
                continue
            return "esc"
        if b in (b"\r", b"\n"):
            return "enter"
        if b == b"\x03":
            return "esc"
        return b.decode("latin-1").lower()


def ask_key(abort=None):
    """Read one key with the terminal briefly in cbreak mode (no watcher running)."""
    fd = sys.stdin.fileno()
    old = _cbreak(fd)
    try:
        return read_key(fd, abort)
    finally:
        _restore(fd, old)


class Watch:
    def __init__(self, on_change=None):
        self.fd = sys.stdin.fileno()
        self.typed = ""        # the line being typed while the agent works
        self.queued = []       # lines sent with Enter, run after the turn
        self.fired = False
        self.on_change = on_change
        self._stop = threading.Event()
        self._paused = False
        self._io = threading.Lock()
        self._dec = codecs.getincrementaldecoder("utf-8")("ignore")
        self._main = threading.main_thread().ident
        self._old = None
        self._th = None

    def __enter__(self):
        self._old = _cbreak(self.fd)
        self._th = threading.Thread(target=self._loop, name="lotus-keys", daemon=True)
        self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._th:
            self._th.join(0.5)
        _restore(self.fd, self._old)
        return False

    @contextlib.contextmanager
    def paused(self):
        """Hand stdin to someone else (an approval prompt) for a moment."""
        with self._io:
            self._paused = True
        try:
            yield self.fd
        finally:
            self._paused = False

    def interrupt(self):
        if self.fired:
            return
        self.fired = True
        try:
            # pthread_kill wakes the main thread even when it's blocked reading a socket
            signal.pthread_kill(self._main, signal.SIGINT)
        except (AttributeError, OSError):
            os.kill(os.getpid(), signal.SIGINT)

    def _loop(self):
        while not self._stop.is_set():
            data = b""
            with self._io:
                if not self._paused:
                    try:
                        r, _, _ = select.select([self.fd], [], [], 0.05)
                        if r:
                            data = os.read(self.fd, 1024)
                            # a lone Esc may be the start of an arrow key; give it a moment
                            if data == b"\x1b":
                                r, _, _ = select.select([self.fd], [], [], 0.03)
                                if r:
                                    data += os.read(self.fd, 64)
                    except OSError:
                        return
            if self._paused:
                time.sleep(0.05)
            if data:
                self._feed(data)

    def _feed(self, data):
        i, n, changed = 0, len(data), False
        while i < n:
            b = data[i]
            if b == 0x1b:
                if i + 1 >= n:
                    self.interrupt()
                    return
                if data[i + 1] in (0x5b, 0x4f):  # ESC [ ... or ESC O x: skip the sequence
                    j = i + 2
                    while j < n and not (0x40 <= data[j] <= 0x7e):
                        j += 1
                    i = j + 1
                    continue
                i += 2  # Alt+key: ignore
                continue
            if b in (0x0d, 0x0a):
                if self.typed.strip():
                    self.queued.append(self.typed)
                self.typed, changed = "", True
            elif b in (0x7f, 0x08):
                self.typed, changed = self.typed[:-1], True
            elif b == 0x15:  # Ctrl+U
                self.typed, changed = "", True
            elif b == 0x17:  # Ctrl+W
                self.typed, changed = self.typed.rstrip().rpartition(" ")[0], True
            elif b >= 0x20:
                j = i
                while j < n and data[j] >= 0x20 and data[j] != 0x7f:
                    j += 1
                self.typed += self._dec.decode(data[i:j])
                i, changed = j, True
                continue
            i += 1
        if changed and self.on_change:
            self.on_change()
