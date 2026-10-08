"""Built-in tools: files, shell, memory, paging, tool packs, sub-agents, images, rendering."""
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import PACKS, TOOLS, pack, pack_tools, signature, tool
from .. import memory

pack("core", "files, shell, memory")
pack("agents", "hand tasks to sub-agents that work in parallel with a fresh context")
pack("render", "draw charts, graphs and tables in the terminal")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".idea", "dist", "build", ".next"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _p(ctx, path):
    p = Path(os.path.expanduser(str(path)))
    return p if p.is_absolute() else Path(ctx.cwd) / p


def _is_binary(p):
    try:
        with open(p, "rb") as f:
            return b"\0" in f.read(2048)
    except OSError:
        return False


def _size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}T"


# ── files ────────────────────────────────────────────────────────────────────

@tool()
def read_file(path: str, start: int = 1, lines: int = 200, _ctx=None):
    """Read a text file with line numbers. Use start/lines to page through big files.
    path: file path, relative to the working directory or absolute
    start: first line to show (1-based)
    lines: number of lines to show"""
    p = _p(_ctx, path)
    if not p.exists():
        return f"error: {p} does not exist"
    if p.is_dir():
        return f"error: {p} is a directory; use list_dir"
    if p.suffix.lower() in IMAGE_EXT:
        return f"{p} is an image; use view_image to look at it"
    if _is_binary(p):
        return f"{p} is a binary file ({_size(p.stat().st_size)})"
    data = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start)
    sel = data[start - 1:start - 1 + max(1, lines)]
    body = "\n".join(f"{i:>5}  {l}" for i, l in enumerate(sel, start))
    end = start + len(sel) - 1
    more = f"\n[lines {start}-{end} of {len(data)}]" if end < len(data) or start > 1 else ""
    return body + more


@tool(danger=True)
def write_file(path: str, content: str, _ctx=None):
    """Create or overwrite a file with the given content.
    path: file path
    content: full file content"""
    p = _p(_ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    return f"{'overwrote' if existed else 'created'} {p} ({len(content)} chars, {content.count(chr(10)) + 1} lines)"


@tool(danger=True)
def edit_file(path: str, old: str, new: str, _ctx=None):
    """Replace one exact snippet in a file. `old` must appear exactly once; include surrounding lines to make it unique.
    path: file path
    old: exact text to find
    new: replacement text"""
    p = _p(_ctx, path)
    if not p.exists():
        return f"error: {p} does not exist"
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(old)
    if n == 0:
        sample = old.strip().splitlines()[0][:60] if old.strip() else ""
        hint = ""
        if sample:
            for i, l in enumerate(text.splitlines(), 1):
                if sample.strip()[:20] and sample.strip()[:20] in l:
                    hint = f" A similar line is at {i}: {l.strip()[:80]!r}. Re-read the file and copy the text exactly."
                    break
        return f"error: text not found in {p}.{hint}"
    if n > 1:
        return f"error: text appears {n} times in {p}; include more surrounding lines so it is unique"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    _ctx.ui.diff(old, new)
    return f"edited {p}"


@tool()
def list_dir(path: str = ".", depth: int = 1, _ctx=None):
    """List a directory as a tree with sizes.
    path: directory
    depth: how many levels deep (1-4)"""
    root = _p(_ctx, path)
    if not root.is_dir():
        return f"error: {root} is not a directory"
    out, count = [f"{root}/"], 0

    def walk(d, prefix, level):
        nonlocal count
        try:
            items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            out.append(prefix + "(permission denied)")
            return
        items = [i for i in items if not (i.name.startswith(".") and i.name not in (".env.example", ".github"))]
        for it in items:
            if count >= 250:
                out.append(prefix + "… (truncated)")
                return
            count += 1
            if it.is_dir():
                skipped = it.name in SKIP_DIRS
                out.append(f"{prefix}{it.name}/" + (" (skipped)" if skipped else ""))
                if level < depth and not skipped:
                    walk(it, prefix + "  ", level + 1)
            else:
                try:
                    out.append(f"{prefix}{it.name}  {_size(it.stat().st_size)}")
                except OSError:
                    out.append(f"{prefix}{it.name}")

    walk(root, "  ", 1)
    return "\n".join(out)


@tool()
def find_files(pattern: str, path: str = ".", _ctx=None):
    """Find files by glob pattern, e.g. '*.py' or 'test_*'.
    pattern: glob matched against file names
    path: directory to search"""
    root = _p(_ctx, path)
    hits = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(os.path.join(dirpath, f), pattern):
                hits.append(os.path.relpath(os.path.join(dirpath, f), root))
                if len(hits) >= 150:
                    return "\n".join(hits) + "\n… (more results; narrow the pattern)"
    return "\n".join(hits) or "no matches"


@tool()
def grep(pattern: str, path: str = ".", glob: str = "", _ctx=None):
    """Search file contents with a regular expression. Returns file:line: text.
    pattern: regex to search for
    path: file or directory
    glob: optional file-name filter like '*.py'"""
    root = _p(_ctx, path)
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "--no-heading", "--max-count", "20", "-M", "200", pattern, str(root)]
        if glob:
            cmd[1:1] = ["-g", glob]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        lines = r.stdout.splitlines()
    else:
        rx = re.compile(pattern)
        lines = []
        files = [root] if root.is_file() else []
        if root.is_dir():
            for dirpath, dirnames, fs in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                files += [Path(dirpath) / f for f in fs if not glob or fnmatch.fnmatch(f, glob)]
        for f in files:
            if _is_binary(f) or f.stat().st_size > 2_000_000:
                continue
            try:
                for i, l in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(l):
                        lines.append(f"{f}:{i}:{l.strip()[:200]}")
            except OSError:
                continue
            if len(lines) > 300:
                break
    cwd = str(_ctx.cwd) + os.sep
    lines = [l.replace(cwd, "") for l in lines]
    if len(lines) > 200:
        return "\n".join(lines[:200]) + f"\n… {len(lines) - 200} more matches"
    return "\n".join(lines) or "no matches"


# ── shell ────────────────────────────────────────────────────────────────────

@tool(danger=True)
def shell(command: str, timeout: int = 120, _ctx=None):
    """Run a shell command in the working directory and return its output and exit code. `cd` changes the working directory for later calls.
    command: the command line to run
    timeout: seconds before the command is stopped"""
    m = re.fullmatch(r"\s*cd\s+(.+?)\s*", command)
    if m:
        target = _p(_ctx, m.group(1).strip("\"'"))
        if target.is_dir():
            _ctx.cwd = str(target.resolve())
            return f"cwd is now {_ctx.cwd}"
        return f"error: no such directory {target}"
    if GUI_BROWSER.search(command):
        return "error: not run. " + _web_hint(_ctx)
    sh = _ctx.cfg.get("shell") or None
    if sh and "powershell" in sh.lower():
        args, use_shell = [sh, "-NoProfile", "-Command", command], False
    elif sh:
        args, use_shell = [sh, "-c", command], False
    else:
        args, use_shell = command, True
    out, note = _run_proc(args, use_shell, _ctx.cwd, timeout, getattr(_ctx, "cancel", None))
    if note == "exit 127":  # command not found: usually a made-up CLI standing in for a missing tool
        out += "\nThat command isn't installed; don't guess others, use a tool from the packs in your instructions."
        if "web_search" in TOOLS:
            out += " For the web: call web_search(query), then fetch_url(url)."
    return f"{out.rstrip()}\n[{note}]"


# a GUI browser started from shell opens a window the model can't see or read
GUI_BROWSER = re.compile(
    r"(?:^|[;&|(]\s*)(?:(?:timeout\s+\S+|nohup|setsid|exec|env(?:\s+\w+=\S+)*)\s+)*"
    r"(?:\S*/)?(?:firefox(?:-esr)?|chromium(?:-browser)?|google-chrome(?:-stable)?|brave(?:-browser)?|"
    r"microsoft-edge(?:-stable)?|librewolf|opera|vivaldi)(?=\s|$|[;&|)])")


def _web_hint(ctx):
    s = "A browser started from shell opens a window you can't see or read."
    if "web_search" in TOOLS:
        s += " To look something up call web_search(query), then fetch_url(url) to read a result."
    if "browser_open" in TOOLS and not getattr(ctx, "depth", 0):
        s += " To click or type on a site call browser_open(url), then browser_snapshot."
    if "open_path" in TOOLS:
        s += " To show the user a page in their own browser call open_path(url)."
    return s


def _kill(p):
    """Stop a command and everything it started (it runs in its own process group)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            import signal
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(1.5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _run_proc(args, use_shell, cwd, timeout, cancel=None):
    """Run a command that Esc / Ctrl+C can stop cleanly. The child gets its own process
    group, so the terminal's Ctrl+C reaches lotus (not a half-killed pipeline), and lotus
    then stops the whole group. Returns (output, note) or raises Interrupted."""
    from . import Interrupted
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(args, shell=use_shell, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, text=True, errors="replace", **kw)
    deadline, why = time.monotonic() + timeout, None
    try:
        while True:
            try:
                out, err = p.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    why = "cancelled"
                elif time.monotonic() > deadline:
                    why = f"stopped after {timeout}s"
                if why:
                    _kill(p)
                    out, err = p.communicate()
                    break
    except KeyboardInterrupt:
        _kill(p)
        try:
            out, err = p.communicate(timeout=2)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out, err = "", ""
        text = (out or "") + (("\n[stderr]\n" + err) if (err or "").strip() else "")
        raise Interrupted(text.rstrip()[-4000:] + "\n[stopped by the user]")
    text = (out or "") + (("\n[stderr]\n" + err) if (err or "").strip() else "")
    return text, why or f"exit {p.returncode}"


# ── memory & paging ──────────────────────────────────────────────────────────

@tool()
def remember(fact: str):
    """Save a durable fact about the user or their setup, for sessions in any folder. Only for things that will still matter later; facts about this project go in project_note.
    fact: one short sentence"""
    return f"remembered: {memory.add(fact)}"


@tool()
def project_note(note: str, section: str = "memory", _ctx=None):
    """Save something worth knowing about this project in its LOTUS.md, which is read at the start of every session here: commands that work, conventions, decisions, where things live.
    note: one short line
    section: memory (facts, decisions) or project (what it is, layout, how to build and test)"""
    if not (_ctx.cfg.get("project") or {}).get("notes", True):
        return "error: project notes are turned off (project.notes in config)"
    if _ctx.permission == "readonly":
        return "error: read-only mode; tell the user what you would have noted instead"
    if section not in ("memory", "project"):  # instructions are the user's to write
        section = "memory"
    path, created, added = memory.add_note(_ctx.root, note, section)
    if not added:
        return f"already in {path}"
    return f"{'created' if created else 'updated'} {path} ({section})"


@tool()
def recall(query: str):
    """Search saved memories.
    query: keywords"""
    hits = memory.search(query)
    return "\n".join(f"- {h}" for h in hits) or "nothing saved about that"


@tool()
def page_output(id: int, offset: int = 0, length: int = 3000, _ctx=None):
    """Read more of a long tool result that was shortened. The shortened result tells you its id.
    id: output id
    offset: character offset to start at
    length: characters to return"""
    if not 0 <= id < len(_ctx.stash):
        return f"error: no saved output #{id}"
    full = _ctx.stash[id]
    chunk = full[offset:offset + length]
    return f"{chunk}\n[chars {offset}-{offset + len(chunk)} of {len(full)}]"


@tool()
def load_tools(pack: str, _ctx=None):
    """Turn on a tool pack so its tools become available. The system prompt lists the packs.
    pack: pack name"""
    p = pack.strip().lower()
    if p in TOOLS and p not in PACKS:  # load_tools("web_search") means its pack
        p = TOOLS[p].pack
    if p.startswith("mcp:") or p in _ctx.cfg.get("mcp", {}):
        from .. import mcp
        name = p.split(":", 1)[-1]
        names = mcp.activate(name, _ctx.cfg)
        _ctx.active.add(f"mcp:{name}")
        return f"loaded MCP server {name}: " + ", ".join(names)
    if p not in PACKS:
        return f"error: no pack '{p}'. Packs: {', '.join(sorted(PACKS))}"
    if p == "browser" and _ctx.depth > 0:
        return "error: only the main agent can drive the browser"
    _ctx.active.add(p)
    sigs = [signature(t) for t in pack_tools(p)]
    out = f"pack '{p}' loaded:\n" + "\n".join(sigs)
    idle = _ctx.idle_packs() if hasattr(_ctx, "idle_packs") else []
    if idle:
        out += "\nIf none of these fit the task, load another pack: " + ", ".join(idle)
    return out


@tool(params={"items": {"type": "array", "items": {"type": "string"},
                        "description": 'e.g. ["[x] find the config", "[>] fix the bug", "[ ] run the tests"]'}})
def todo(items: list, _ctx=None):
    """Write or update your plan for a multi-step task. Send the whole list every time, marking each step [x] done, [>] in progress or [ ] to do. The plan stays in front of you even after old messages are summarised.
    items: the steps in order"""
    return _ctx.set_plan(items)


# ── images ───────────────────────────────────────────────────────────────────

@tool()
def view_image(path: str, _ctx=None):
    """Look at an image file (needs a vision model).
    path: image path"""
    p = _p(_ctx, path)
    if not p.exists():
        return f"error: {p} does not exist"
    return _ctx.queue_image(str(p))


# ── sub-agents ───────────────────────────────────────────────────────────────

@tool(pack="agents", params={"tasks": {"description": "list of self-contained task descriptions"}})
def delegate(tasks: list, packs: str = "", _ctx=None):
    """Hand one or more independent tasks to sub-agents. Each starts with a fresh, empty context and returns only its final answer, which keeps your own context small. Make each task self-contained.
    tasks: list of task descriptions
    packs: comma-separated extra tool packs for the sub-agents, e.g. 'web'"""
    if _ctx.depth > 0:
        return "error: sub-agents cannot delegate further"
    extra = [p.strip() for p in packs.split(",") if p.strip() and p.strip() != "browser"]
    return _ctx.spawn([str(t) for t in tasks][:6], extra)


# ── rendering ────────────────────────────────────────────────────────────────

@tool(pack="render", params={"spec": {"description": 'e.g. {"type":"bar","title":"t","labels":["a","b"],"values":[3,5]}. types: bar, line (series), pie, spark, graph (edges)'}})
def show_chart(spec: dict, _ctx=None):
    """Draw a chart in the user's terminal.
    spec: chart spec object"""
    from ..charts import render
    _ctx.ui.block(render(spec))
    return "chart shown to the user; don't repeat the numbers"


@tool(pack="render", params={"rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                                      "description": "rows of cells; the first row is the header"}})
def show_table(rows: list, _ctx=None):
    """Draw a table in the user's terminal.
    rows: list of rows, header first"""
    from ..render import table_lines
    _ctx.ui.block(table_lines(rows))
    return "table shown to the user; don't repeat it"
