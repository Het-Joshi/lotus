"""lotus command line."""
import argparse
import base64
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, automation, mcp, memory, plugins, theme
from . import tools as T
from .agent import Agent
from .config import home, load as load_cfg, save as save_cfg
from .ollama import Ollama, OllamaError
from .render import UI, StreamRenderer, banner_animated, petal_meter, table_lines
from .theme import G, c, gradient, trunc, width
from .tools import browser, core, system, web  # noqa: F401  (registers built-in tools)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

try:
    import readline
    HAVE_RL = True
except ImportError:
    HAVE_RL = False


def _k(n):
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}k"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def rl_safe(s):
    return re.sub(r"(\x1b\[[0-9;]*m)", "\001\\1\002", s) if HAVE_RL else s


_pending = [""]


def prefill(text):
    """Queue text for the next prompt's edit buffer (typeahead, or /undo's message)."""
    _pending[0] += text or ""


def take_prefill():
    text, _pending[0] = _pending[0], ""
    return text


# ── command registry ─────────────────────────────────────────────────────────
# One table drives the "/" menu, /help, and argument completion.
# (name, arguments, what it does, needs an argument)

COMMANDS = [
    ("help", "", "commands and keyboard shortcuts", False),
    ("model", "[name]", "switch model (no name: pick from a list)", False),
    ("models", "", "installed models with context size and abilities", False),
    ("pull", "<name>", "download a model", True),
    ("think", "[mode]", "reasoning: on, off, low, medium, high; show or hide it", False),
    ("tools", "[pack | -pack]", "list tool packs, or turn one on or off", False),
    ("context", "", "what is using the context window", False),
    ("compact", "[what to keep]", "summarise history now, optionally saying what matters", False),
    ("clear", "", "start a fresh conversation", False),
    ("retry", "", "regenerate the last reply", False),
    ("undo", "", "drop the last exchange and edit your message", False),
    ("todo", "[clear]", "the agent's current plan", False),
    ("pin", "[note | -n]", "pin a note the agent sees every turn; alone lists pins", False),
    ("copy", "", "copy the last reply to the clipboard", False),
    ("export", "[file.md]", "save the conversation as markdown", False),
    ("init", "", "start LOTUS.md here: lotus looks around and notes what the project is", False),
    ("note", "[-i | -p] [text]", "add to LOTUS.md (a memory; -i an instruction, -p about the project); alone shows it", False),
    ("rem", "[save|clear]", "where the last session in this folder left off (.LOTUS_REM.txt)", False),
    ("mem", "", "facts remembered across sessions", False),
    ("remember", "<fact>", "save a fact for future sessions", True),
    ("forget", "<n | text>", "delete a remembered fact", True),
    ("file", "<path>", "attach a file or image to your next message", True),
    ("image", "<path>", "attach an image to your next message", True),
    ("ls", "[path]", "list a folder", False),
    ("cd", "<path>", "change the working directory", True),
    ("explore", "", "browse files: preview, attach, cd", False),
    ("perm", "[mode]", "approval for tools that change things: ask, auto, readonly", False),
    ("theme", "[mode]", "colors: auto follows the terminal, or dark, light", False),
    ("tor", "[on|off]", "route web tools through Tor", False),
    ("browser", "[close|show|hide]", "the browser lotus drives: tabs, mode; close it or switch window/headless", False),
    ("session", "[save|load|list] [name]", "save and resume conversations", False),
    ("recipe", "[name] [input]", "list or run a saved recipe", False),
    ("plugins", "", "loaded plugins and load errors", False),
    ("doctor", "", "check Ollama, models, Tor, Playwright", False),
    ("exit", "", "quit (Ctrl+D also works)", False),
]
ALIASES = {"quit": "exit", "q": "exit", "h": "help", "?": "help", "new": "clear", "attach": "file", "files": "explore",
           "plan": "todo"}
ARG_CHOICES = {
    "think": {"on": "reason before answering", "off": "answer directly", "auto": "the model's default",
              "low": "light reasoning", "medium": "moderate reasoning", "high": "deep reasoning",
              "show": "stream the reasoning", "hide": "collapse the reasoning"},
    "theme": {"auto": "follow the terminal background", "dark": "dark palette", "light": "light palette"},
    "perm": {"ask": "ask before shell, writes, apps", "auto": "allow everything", "readonly": "never change anything"},
    "tor": {"on": "route web tools through Tor", "off": "direct connections"},
    "session": {"save": "save under a name", "load": "load a saved session", "list": "saved sessions"},
    "todo": {"clear": "drop the current plan"},
    "rem": {"save": "write it now", "clear": "forget where we left off"},
    "note": {"-i": "an instruction for lotus in this project", "-p": "about the project: layout, build, test"},
    "browser": {"close": "close the browser", "show": "use a visible window from the next launch",
                "hide": "run headless from the next launch"},
}
PATH_ARGS = {"image", "file", "ls", "cd", "export"}


def all_commands():
    seen = {n for n, *_ in COMMANDS}
    extra = [(n, "", h or "plugin command", False) for n, (_, h) in sorted(T.COMMANDS.items()) if n not in seen]
    seen |= {n for n, *_ in extra}
    recipes = [(n, "[input]", "recipe: " + (d or n), False) for n, d in sorted(automation.list_all().items()) if n not in seen]
    return COMMANDS + extra + recipes


def _rank(entries, q, key=lambda e: e[0], desc=lambda e: ""):
    """Prefix matches first, then substring, then (for longer queries) description matches."""
    q = q.lower()
    tiers = ([], [], [])
    for e in entries:
        k = key(e).lower()
        if k.startswith(q):
            tiers[0].append(e)
        elif q in k:
            tiers[1].append(e)
        elif len(q) >= 3 and q in desc(e).lower():
            tiers[2].append(e)
    return tiers[0] + tiers[1] + tiers[2]


def menu_source(agent, client):
    """Returns fn(buffer, cursor) -> (replace_from, [Item]) for the prompt's live menu."""
    from .lineedit import Item
    models = []

    def paths(prefix):
        head, stem = os.path.split(prefix)
        folder = Path(os.path.expanduser(head or "."))
        folder = folder if folder.is_absolute() else Path(agent.cwd) / folder
        try:
            items = sorted(folder.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError:
            return []
        return [(os.path.join(head, it.name), it.is_dir()) for it in items
                if it.name.lower().startswith(stem.lower()) and (stem.startswith(".") or not it.name.startswith("."))][:100]

    def path_items(prefix, at=False, run_files=False, run_dirs=False):
        """Folders end in / so Tab (or Enter, unless run_dirs) opens them; files finish the token."""
        out = []
        for p, is_dir in paths(prefix):
            ins = ("@" if at else "") + p + ("/" if is_dir else (" " if not run_files else ""))
            run = run_dirs if is_dir else run_files
            out.append(Item(ins, p + ("/" if is_dir else ""), "folder" if is_dir else "", run))
        return out

    def source(buf, pos):
        before = buf[:pos]
        m = re.search(r"(?:^|\s)@(\S*)$", before)
        if m:
            return pos - len(m.group(1)) - 1, path_items(m.group(1), at=True)
        if not buf.startswith("/") or "\n" in before:
            return None
        parts = before[1:].split(" ")
        if len(parts) == 1:
            ranked = _rank(all_commands(), parts[0], desc=lambda e: e[2])
            return 0, [Item("/" + n + (" " if need else ""), "/" + n + ("  " + args if args else ""), desc, not need)
                       for n, args, desc, need in ranked]
        cmd = ALIASES.get(parts[0].lower(), parts[0].lower())
        arg, start = parts[-1], pos - len(parts[-1])
        if len(parts) > 2 and not (cmd == "session" and len(parts) == 3):
            return None
        if cmd in PATH_ARGS:
            return start, path_items(arg, run_files=cmd != "export", run_dirs=cmd in ("cd", "ls"))
        if cmd == "model":
            if not models:
                try:
                    models.extend((x["name"], f"{x.get('size', 0) / 1e9:.1f} GB") for x in client.models())
                except OllamaError:
                    pass
            pool = [(n, ("current · " if n == agent.model else "") + d) for n, d in models]
        elif cmd == "tools":
            pool = [(p, ("on · " if p in agent.active else "") + d) for p, d in sorted(T.PACKS.items())]
            pool += [("-" + p, "turn off") for p in sorted(agent.active - {"core"})]
        elif cmd == "recipe":
            pool = sorted(automation.list_all().items())
        elif cmd == "session" and len(parts) == 3 and parts[1] == "load":
            d = home() / "sessions"
            files = sorted(d.glob("*.json"), key=lambda p: -p.stat().st_mtime) if d.exists() else []
            pool = [(f.stem, time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))) for f in files[:30]]
        elif cmd == "pin":
            pool = [(f"-{i}", "unpin: " + p) for i, p in enumerate(agent.pins, 1)]
        else:
            pool = list(ARG_CHOICES.get(cmd, {}).items())
        hits = _rank(pool, arg, desc=lambda e: e[1])
        if cmd in ("recipe", "note"):  # leave room for the input that follows
            return start, [Item(v + " ", v, d, False) for v, d in hits]
        return start, [Item(v, v, d, True) for v, d in hits]

    return source


def footer(agent):
    bits = [c(agent.model, "petal")]
    if agent.last_stats:
        frac = agent.ctx.used() / agent.ctx.limit
        bits.append(petal_meter(frac, 5) + c(f" {frac * 100:.0f}% context", "mist"))
    if agent.plan:
        done = sum(1 for s, _ in agent.plan if s == "done")
        bits.append(c(f"plan {done}/{len(agent.plan)}", "leaf" if done == len(agent.plan) else "mist"))
    if agent.pins:
        bits.append(c(f"{len(agent.pins)} pinned", "mist"))
    if agent.permission != "ask":
        bits.append(c(f"perm {agent.permission}", "stamen"))
    if browser._S.get("context"):
        bits.append(c(f"{G['web']} browser", "pond"))
    return c(" · ", "mist").join(bits)


def setup_completion(agent, client):
    """Tab completion for the plain readline prompt (used when the live menu isn't available)."""
    if not HAVE_RL:
        return
    source = menu_source(agent, client)
    cache = {}

    def complete(text, state):
        try:
            if state == 0:
                r = source(readline.get_line_buffer(), readline.get_endidx())
                cache["hits"] = [it.insert for it in r[1]] if r else []
            hits = cache.get("hits", [])
            if state >= len(hits):
                return None
            hit = hits[state]
            return hit if hit.endswith(("/", " ")) or len(hits) > 1 else hit + " "
        except Exception:
            return None

    readline.set_completer(complete)
    readline.set_completer_delims(" \t\n")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
        readline.parse_and_bind("set completion-ignore-case on")
        readline.parse_and_bind("set show-all-if-ambiguous on")


# ── setup ────────────────────────────────────────────────────────────────────

def boot(cfg):
    mcp.register_packs(cfg)
    plugins.load_all(cfg, os.getcwd())


def badges(info):
    caps = info["caps"]
    out = []
    for cap, label, col in (("tools", "tools", "leaf"), ("vision", "vision", "pond"), ("thinking", "think", "stamen")):
        if cap in caps:
            out.append(c(label, col))
    return " ".join(out)


def pick_model(client, cfg, ui, wanted=None):
    names = [m["name"] for m in client.models()]
    if not names:
        raise OllamaError("No models installed yet. Try: ollama pull qwen3:4b  (or lotus pull qwen3:4b)")
    want = wanted or cfg.get("model")
    if want:
        for n in names:
            if n == want or n.split(":")[0] == want or n.startswith(want):
                return n
        ui.warn(f"model '{want}' isn't installed; picking another")
    for n in names:  # prefer something that can call tools
        try:
            if "tools" in client.info(n)["caps"]:
                return n
        except OllamaError:
            continue
    return names[0]


def find_vision(client, cfg):
    if cfg.get("vision_model"):
        return cfg["vision_model"]
    for m in client.models():
        try:
            if "vision" in client.info(m["name"])["caps"]:
                return m["name"]
        except OllamaError:
            pass
    return None


# ── input helpers ────────────────────────────────────────────────────────────

def expand_mentions(text, agent, ui):
    """@path attaches a file (text inline, images as images). Dragged-in image paths work too."""
    images, blocks = [], []
    budget = int(agent.ctx.limit * agent.ctx.ratio * 0.35)

    def attach(raw):
        p = Path(os.path.expanduser(raw.strip("\"'")))
        if not p.is_absolute():
            p = Path(agent.cwd) / p
        if not p.exists():
            return False
        if p.is_dir():
            blocks.append(f"<dir path=\"{p}\">\n{core.list_dir(str(p), 2, _ctx=agent)}\n</dir>")
        elif p.suffix.lower() in IMAGE_EXT:
            images.append(str(p))
        else:
            body = p.read_text(encoding="utf-8", errors="replace")
            if len(body) > budget:
                body = body[:budget] + f"\n…[truncated; read_file('{p}') for the rest]"
            blocks.append(f"<file path=\"{p}\">\n{body}\n</file>")
        ui.info(f"attached {p.name}")
        return True

    for m in re.finditer(r'(?<!\S)@("[^"]+"|\S+)', text):
        attach(m.group(1))
    for m in re.finditer(r"""(?:^|\s)(['"]?)(/[^'"\n]+?\.(?:png|jpe?g|webp|gif|bmp)|[A-Za-z]:\\[^'"\n]+?\.(?:png|jpe?g|webp|gif|bmp))\1(?=\s|$)""", text, re.I):
        path = m.group(2)
        if os.path.exists(path) and path not in images:
            images.append(path)
            ui.info(f"attached {Path(path).name}")
    for a in agent.attachments:
        if Path(a).suffix.lower() in IMAGE_EXT:
            images.append(a)
        else:
            attach(a)
    agent.attachments = []
    if blocks:
        text = text + "\n\n" + "\n\n".join(blocks)
    return text, images


def read_input(prompt):
    line = input(prompt)
    if line.strip() == '"""':
        lines = []
        while True:
            l = input(c("  … ", "mist"))
            if l.strip() == '"""':
                break
            lines.append(l)
        return "\n".join(lines)
    while line.endswith("\\"):
        line = line[:-1] + "\n" + input(c("  … ", "mist"))
    return line


def status(agent, ui):
    s = agent.last_stats
    if not s:
        return
    used = agent.ctx.used()
    frac = used / agent.ctx.limit
    parts = [c(agent.model, "petal"),
             c(f"{_k(s['prompt'])} in" + (f" · {_k(s['out'])} out" if s["out"] else ""), "mist")]
    if s.get("tps"):
        parts.append(c(f"{s['tps']:.0f} tok/s", "mist"))
    if agent.turn_secs >= 1:
        t = agent.turn_secs
        parts.append(c(f"{t:.1f}s" if t < 60 else f"{int(t // 60)}m {int(t % 60):02d}s", "mist"))
    parts.append(petal_meter(frac) + c(f" {frac * 100:.0f}% of {_k(agent.ctx.limit)}", "mist"))
    ui.meta(c("  ╰ ", "mist") + c("   ", "mist").join(parts))


# ── slash commands ───────────────────────────────────────────────────────────

SHORTCUTS = [
    ("/", "command menu: type to filter, ↑↓ pick, Tab complete, Enter run, Esc close"),
    ("@path", "attach a file, folder or image (Tab/Enter in the menu completes paths)"),
    ("!command", "run a shell command yourself; output is shared with the model"),
    ("Alt+Enter, \\ Enter", "new line; big pastes collapse into a [pasted #n] placeholder"),
    ("↑ ↓", "history (what you've typed filters it), or move between lines"),
    ("Esc (while working)", "stop the turn: the reply, a running command, the browser, sub-agents"),
    ("type while working", "Enter queues it as your next message; an unfinished line comes back in the prompt"),
    ("Ctrl+C", "stop a reply; clear the line; twice on an empty line quits"),
    ("Ctrl+A/E  Ctrl+W/U/K  Ctrl+L", "line start/end, delete word/to start/to end, redraw"),
]


def help_lines():
    rows = []
    for n, args, desc, _ in all_commands():
        rows.append("  " + c(("/" + n + (" " + args if args else "")).ljust(34), "petal") + c(desc, "mist"))
    rows.append("")
    rows += ["  " + c(k.ljust(34), "stamen") + c(v, "mist") for k, v in SHORTCUTS]
    return rows


def cmd_models(client, ui, current=None):
    rows = [["#", "model", "size", "context", "abilities"]]
    names = []
    for i, m in enumerate(client.models(), 1):
        try:
            info = client.info(m["name"])
            ctx, ab = _k(info["ctx"]), ", ".join(x for x in info["caps"] if x != "completion")
        except OllamaError:
            ctx, ab = "?", "?"
        mark = f"{G['bloom']} " if m["name"] == current else ""
        rows.append([str(i), mark + m["name"], f"{m.get('size', 0) / 1e9:.1f} GB", ctx, ab])
        names.append(m["name"])
    ui.block(table_lines(rows))
    return names


def cmd_pull(client, ui, name):
    last = ""
    try:
        for ev in client.pull(name):
            st = ev.get("status", "")
            if ev.get("total"):
                frac = ev.get("completed", 0) / ev["total"]
                bar = gradient("█" * int(frac * 30)) + c("░" * (30 - int(frac * 30)), "mist")
                sys.stdout.write(f"\r  {bar} {frac * 100:5.1f}%  {st[:30]:30}")
                sys.stdout.flush()
            elif st != last:
                sys.stdout.write("\r\033[K")
                ui.info(st)
            last = st
        sys.stdout.write("\n")
        ui.ok(f"pulled {name}")
    except OllamaError as e:
        ui.error(str(e))


def explore(agent, ui):
    here = Path(agent.cwd)
    while True:
        try:
            items = sorted(here.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            ui.error("permission denied")
            here = here.parent
            continue
        items = [p for p in items if not p.name.startswith(".")][:60]
        ui.block([c(f"  {here}", "petal", bold=True)] +
                 [f"  {c(f'{i:>3}', 'mist')} " + (c(p.name + '/', 'bloom') if p.is_dir() else c(p.name, 'ink')) for i, p in enumerate(items, 1)])
        try:
            ans = input(rl_safe(c("  explore ", "bloom") + c("number=open  ..=up  a N=attach  cd=work here  q=quit › ", "mist"))).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if ans in ("q", ""):
            return
        if ans == "..":
            here = here.parent
        elif ans == "cd":
            agent.cwd = str(here)
            ui.ok(f"working directory is now {here}")
        elif ans.startswith("a ") and ans[2:].strip().isdigit():
            i = int(ans[2:]) - 1
            if 0 <= i < len(items):
                agent.attachments.append(str(items[i]))
                ui.ok(f"{items[i].name} will be attached to your next message")
        elif ans.isdigit() and 0 < int(ans) <= len(items):
            p = items[int(ans) - 1]
            if p.is_dir():
                here = p
            else:
                out = core.read_file(str(p), 1, 40, _ctx=agent)
                ui.block([c("  ╭─ " + p.name, "mist")] + [c("  │ ", "mist") + l for l in out.splitlines()] + [c("  ╰─", "mist")])


def theme_preview():
    swatch = "  ".join(c("██", k) + " " + c(k, "mist") for k in theme.PAL)
    return ["  " + c(f"theme {theme.MODE}", "petal", bold=True) + c(f"  ({theme.PREF}; {theme.SOURCE})", "mist"),
            "  " + swatch]


def copy_text(text):
    """The system clipboard, or OSC 52 over SSH or when there's no clipboard tool
    (the terminal does the copying, so it lands on the machine you're sitting at)."""
    remote = os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")
    if not remote:
        try:
            if not str(system.clipboard_set(text)).startswith("error"):
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    if not sys.stdout.isatty():
        return False
    seq = "\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a"
    if os.environ.get("TMUX"):
        seq = "\033Ptmux;" + seq.replace("\033", "\033\033") + "\033\\"
    sys.stdout.write(seq)
    sys.stdout.flush()
    return True


def handle(line, agent, client, cfg, ui):
    parts = line[1:].split(None, 1)
    name, arg = (parts[0].lower() if parts else ""), (parts[1].strip() if len(parts) > 1 else "")
    name = ALIASES.get(name, name)
    if name == "exit":
        raise EOFError
    if name == "help":
        ui.block(help_lines())
    elif name == "models":
        cmd_models(client, ui, agent.model)
    elif name == "model":
        target = arg
        if not target:
            names = cmd_models(client, ui, agent.model)
            try:
                pick = input(rl_safe(c("  model number › ", "bloom"))).strip()
            except (EOFError, KeyboardInterrupt):
                return
            if pick.isdigit() and 0 < int(pick) <= len(names):
                target = names[int(pick) - 1]
        if target:
            try:
                target = pick_model(client, {"model": target}, ui, target)
                agent.set_model(target)
                cfg["model"] = target
                save_cfg(cfg)
                ui.ok(f"{target}  {badges(agent.info)}  {c('context ' + _k(agent.info['ctx']), 'mist')}" +
                      ("" if agent.native else c("  (text tool protocol)", "mist")))
            except OllamaError as e:
                ui.error(str(e))
    elif name == "pull":
        cmd_pull(client, ui, arg) if arg else ui.warn("usage: /pull <model>")
    elif name == "think":
        if arg in ("show", "hide"):
            ui.show_thinking = arg == "show"
        elif arg in ("on", "off", "auto", "low", "medium", "high"):
            agent.think = arg
        note = "" if "thinking" in agent.caps else " (this model has no native reasoning; 'on' adds a plan-first prompt)"
        ui.info(f"think: {agent.think}, {'shown' if ui.show_thinking else 'hidden'}{note}")
    elif name == "tools":
        if arg.startswith("-"):
            agent.active.discard(arg[1:])
            ui.ok(f"{arg[1:]} off")
        elif arg:
            ui.info(core.load_tools(arg, _ctx=agent).splitlines()[0])
        else:
            rows = [["", "pack", "tools", "what it's for"]]
            for p, d in sorted(T.PACKS.items()):
                n = len(T.pack_tools(p))
                rows.append([G["bloom"] if p in agent.active else "", p, str(n) if n or not p.startswith("mcp:") else "on load", d])
            ui.block(table_lines(rows))
            ui.info(f"tool calling: {'native' if agent.native else 'text protocol'}")
    elif name == "context":
        b = agent.breakdown()
        total = sum(b.values())
        rows = [["part", "tokens", "share"]] + [[k, _k(v), f"{v / agent.ctx.limit * 100:.0f}%"] for k, v in b.items()]
        rows.append(["total", _k(total), f"{total / agent.ctx.limit * 100:.0f}%"])
        ui.block(table_lines(rows))
        ui.info(f"window {_k(agent.ctx.limit)} (model max {_k(agent.ctx.model_max)}, cap ctx_max={_k(cfg['ctx_max'])}); "
                f"allocated num_ctx {_k(agent.ctx.num_ctx or 0)}; ~{agent.ctx.ratio:.1f} chars/token")
    elif name == "compact":
        agent.compact(force=True, focus=arg)
    elif name == "clear":
        agent.reset()
        ui.ok("fresh start" + (f" (kept {len(agent.pins)} pinned note(s); /pin to see them)" if agent.pins else ""))
    elif name == "todo":
        if arg == "clear":
            agent.plan = []
            ui.ok("plan cleared")
        elif agent.plan:
            ui.block(agent.plan_lines())
        else:
            ui.info("no plan yet; the agent makes one with the todo tool on multi-step tasks")
    elif name == "pin":
        if re.fullmatch(r"-\d+", arg):
            i = int(arg[1:]) - 1
            if 0 <= i < len(agent.pins):
                ui.ok("unpinned: " + agent.pins.pop(i))
            else:
                ui.warn("no such pin")
        elif arg:
            agent.pins.append(arg)
            ui.ok("pinned; the agent sees this every turn, even after compaction")
        else:
            ui.block([f"  {c(str(i), 'mist')} {p}" for i, p in enumerate(agent.pins, 1)]
                     or [c("  nothing pinned. /pin <note> keeps a goal or constraint in front of the agent", "mist")])
    elif name == "undo":
        prompt = agent.undo()
        if prompt is None:
            ui.warn("nothing to undo")
        else:
            ui.ok("removed: " + trunc(prompt.replace("\n", " "), 60))
            prefill(prompt if "\n" not in prompt else "")
    elif name == "retry":
        if agent._last_prompt() is None:
            ui.warn("nothing to retry")
        else:
            ui.write("\n")
            agent.retry()
            status(agent, ui)
    elif name == "copy":
        if not agent.last_reply.strip():
            ui.warn("no reply to copy yet")
        elif copy_text(agent.last_reply.strip()):
            ui.ok(f"copied the last reply ({len(agent.last_reply.strip())} chars)")
        else:
            ui.error("couldn't reach a clipboard")
    elif name == "export":
        target = Path(os.path.expanduser(arg or f"lotus-{time.strftime('%Y%m%d-%H%M')}.md"))
        target = target if target.is_absolute() else Path(agent.cwd) / target
        target.write_text(agent.export_markdown(), encoding="utf-8")
        ui.ok(f"saved {target}")
    elif name == "theme":
        if arg in ("auto", "dark", "light"):
            theme.set_theme(arg)
            cfg["theme"] = arg
            save_cfg(cfg)
        elif arg:
            ui.warn("usage: /theme auto|dark|light")
        ui.block(theme_preview())
    elif name == "init":
        cmd_init(agent, ui)
    elif name == "note":
        cmd_note(arg, agent, ui)
    elif name == "rem":
        cmd_rem(arg, agent, cfg, ui)
    elif name == "mem":
        fs = memory.facts()
        ui.block([f"  {c(str(i), 'mist')} {f}" for i, f in enumerate(fs, 1)] or [c("  nothing remembered yet", "mist")])
    elif name == "remember":
        ui.ok("remembered: " + memory.add(arg)) if arg else ui.warn("usage: /remember <fact>")
    elif name == "forget":
        gone = memory.forget(arg)
        ui.ok(f"forgot: {gone}") if gone else ui.warn("no match")
    elif name in ("image", "file"):
        p = Path(os.path.expanduser(arg.strip("\"'")))
        p = p if p.is_absolute() else Path(agent.cwd) / p
        if p.exists():
            agent.attachments.append(str(p))
            ui.ok(f"{p.name} will be attached to your next message")
        else:
            ui.error(f"{p} not found")
    elif name == "ls":
        ui.block(core.list_dir(arg or ".", 1, _ctx=agent).splitlines())
    elif name == "cd":
        ui.info(core.shell(f"cd {arg or '~'}", _ctx=agent))
    elif name == "explore":
        explore(agent, ui)
    elif name == "tor":
        if arg in ("on", "off"):
            agent.tor = arg == "on"
        up = web.tor_reachable(cfg["tor"]["proxy"])
        ui.info(f"Tor routing {'on' if agent.tor else 'off'}; proxy {cfg['tor']['proxy']} is {'reachable' if up else 'not reachable'}")
        if agent.tor and not up:
            ui.warn("start Tor (tor service, or Tor Browser on port 9150) and set tor.proxy in config")
    elif name == "browser":
        cmd_browser(arg, cfg, ui)
    elif name == "perm":
        if arg in ("ask", "auto", "readonly"):
            agent.permission = arg
        ui.info(f"permission: {agent.permission}")
    elif name == "session":
        sub, _, nm = arg.partition(" ")
        d = home() / "sessions"
        if sub == "save":
            ui.ok(f"saved {agent.save(nm or time.strftime('%Y%m%d-%H%M'))}")
        elif sub == "load":
            try:
                data = agent.load(nm or "last")
                ui.ok(f"loaded {len(agent.messages)} messages from {data.get('saved', '')}")
            except FileNotFoundError:
                ui.error("no such session")
        else:
            files = sorted(d.glob("*.json"), key=lambda p: -p.stat().st_mtime) if d.exists() else []
            ui.block([f"  {c(f.stem, 'petal')}  {c(time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime)), 'mist')}" for f in files[:20]] or [c("  no sessions", "mist")])
    elif name == "recipe":
        rname, _, rin = arg.partition(" ")
        if not rname:
            items = automation.list_all()
            ui.block([f"  {c(k, 'petal')}  {c(v, 'mist')}" for k, v in items.items()] or [c(f"  no recipes in {automation.recipes_dir()}", "mist")])
        else:
            run_recipe(rname, rin, agent, cfg, ui)
    elif name == "plugins":
        ui.block([c("  " + p, "ink") for p in plugins.LOADED] or [c(f"  no plugins; drop .py files in {home() / 'plugins'}", "mist")])
        for e in plugins.ERRORS:
            ui.error(e)
    elif name == "doctor":
        doctor(cfg, ui, client)
    elif name in T.COMMANDS:
        fn, _ = T.COMMANDS[name]
        out = fn(agent, arg)
        if out:
            ui.block(str(out).splitlines())
    elif name in automation.list_all():
        run_recipe(name, arg, agent, cfg, ui)
    else:
        close = difflib.get_close_matches(name, [n for n, *_ in all_commands()], n=3, cutoff=0.5)
        hint = ("; did you mean " + ", ".join("/" + n for n in close) + "?") if close else ""
        ui.warn(f"unknown command /{name}{hint}  (type / to browse)")


def show_md(ui, text):
    r = StreamRenderer(ui)
    r.feed(text.rstrip() + "\n")
    r.close()


def note_lines(root):
    """LOTUS.md as numbered bullets under their sections (the numbers /note -N uses)."""
    items = memory.note_items(root)
    out, n = [], 0
    for key, head in memory.SECTIONS.items():
        bullets = items.get(key, [])
        out.append("  " + c(head, "petal", bold=True) + c(f"  {len(bullets)}", "mist"))
        for b in bullets:
            n += 1
            out.append(f"  {c(f'{n:>3}', 'mist')} {b}")
    return out


def cmd_note(arg, agent, ui):
    path = memory.notes_path(agent.root)
    if arg in ("-i", "-p", "-m"):
        ui.warn(f"usage: /note {arg} <text>")
        return
    if re.fullmatch(r"-\d+", arg):
        gone = memory.forget_note(agent.root, arg[1:])
        ui.ok(f"removed from LOTUS.md: {gone}") if gone else ui.warn("no such note")
        return
    m = re.match(r"^-([ipm])\s+(.+)$", arg, re.S)
    section, text = ({"i": "instructions", "p": "project", "m": "memory"}[m.group(1)], m.group(2)) if m else ("memory", arg)
    if text:
        p, created, added = memory.add_note(agent.root, text, section)
        if not added:
            ui.info(f"already in {p.name}")
        else:
            ui.ok(f"{'created' if created else 'added to'} {p} ({section})")
        return
    if not path.exists():
        ui.info(f"no LOTUS.md in {agent.root} yet. /init writes one, /note <text> starts it with a note")
        return
    ui.block([c(f"  {path}", "mist")] + note_lines(agent.root)
             + [c("  /note <text> adds a memory · -i an instruction · -p about the project · /note -N removes one", "mist")])


def cmd_init(agent, ui):
    path, created = memory.ensure_notes(agent.root)
    ui.ok(f"{'created' if created else 'found'} {path}")
    if memory.note_items(agent.root).get("project"):
        ui.info("it already describes the project; /note shows it, /note -p <text> adds to it")
        return
    ui.write("\n")
    agent.turn("Look around this project so you can describe it in LOTUS.md: list the folder, then read the README and "
               "any build or package files (pyproject.toml, package.json, Makefile, Cargo.toml, ...). Then save 3 to 8 "
               'short facts with project_note(section="project"): what the project is, its main folders and files, and the '
               "exact commands to install, run and test it. Only note commands you saw evidence for. Finish with one line "
               "saying what you saved.")
    status(agent, ui)


def cmd_rem(arg, agent, cfg, ui):
    if arg == "clear":
        gone = memory.clear_rem(agent.root)
        agent.rem = None
        ui.ok("forgot where we left off") if gone else ui.info("nothing to forget")
        return
    if arg == "save":
        agent._rem_dirty = True
        p = agent.save_rem(use_model=(cfg.get("project") or {}).get("rem_by_model", True))
        ui.ok(f"saved {p}") if p else ui.info("nothing to save yet")
        return
    rem = memory.load_rem(agent.root)
    if not rem:
        ui.info(f"no {memory.REM_FILE} in {agent.root} yet; it's written when a session here ends")
        return
    ui.block([c(f"  {memory.rem_path(agent.root)}  ·  {memory.ago(rem[1])}", "mist")])
    show_md(ui, rem[0])


def cmd_browser(arg, cfg, ui):
    if arg == "close":
        browser.close()
        ui.ok("browser closed")
        return
    if arg in ("show", "hide"):
        cfg.setdefault("browser", {})["headless"] = arg == "hide"
        browser.close()
        ui.ok(f"the browser will open {'headless' if arg == 'hide' else 'in a window'} next time it's used (this session)")
        return
    try:
        d = browser.describe()
    except Exception as e:  # the browser died underneath us
        ui.error(f"browser: {e}")
        return
    if d is None:
        bc = cfg.get("browser", {})
        where = f"your Chrome at {bc['cdp_url']}" if bc.get("cdp_url") else ("headless" if bc.get("headless") else "a window (headless without a display)")
        ui.info(f"the browser isn't running; it opens on first use, in {where}. /tools browser lets the model drive it")
        return
    mode, rows = d
    out = ["  " + c(f"{G['web']} browser", "petal", bold=True) + c(f"  {mode}", "mist")]
    for cur, k, title, url in rows:
        out.append("  " + (c(G["bloom"], "bloom") if cur else " ") + c(f" {k}. ", "mist") + c(trunc(title or "(untitled)", 40), "ink")
                   + "  " + c(trunc(url, max(20, width() - 56)), "pond"))
    bc = cfg.get("browser", {})
    if bc.get("allow") or bc.get("block"):
        out.append(c(f"  allow: {', '.join(bc.get('allow') or ['anything'])}   block: {', '.join(bc.get('block') or ['nothing'])}", "mist"))
    ui.block(out)


def run_recipe(rname, rin, agent, cfg, ui):
    try:
        meta, prompt, packs = automation.build(rname, rin, cfg)
    except FileNotFoundError as e:
        ui.error(str(e))
        return
    for p in packs:
        agent.active.add(p)
    ui.write("\n")
    agent.turn(prompt)
    status(agent, ui)


def run_bang(line, agent, ui):
    cmd = line[1:].strip()
    try:
        out = core.shell(cmd, timeout=300, _ctx=agent)
    except T.Interrupted as e:
        out = e.partial
        ui.interrupted("command stopped")
    ui.block([c("  " + l, "ink") for l in out.splitlines()[-60:]])
    agent.notes.append(f"<shell command=\"{cmd}\">\n{agent._fit(out)}\n</shell>")


# ── modes ────────────────────────────────────────────────────────────────────

def repl(agent, client, cfg, ui):
    from . import lineedit
    hist = home() / "history"
    editor = None
    if lineedit.available():
        editor = lineedit.Editor(home() / "history.jsonl", menu_fn=menu_source(agent, client),
                                 footer_fn=lambda: footer(agent))
    elif HAVE_RL:
        try:
            readline.read_history_file(str(hist))
        except Exception:
            pass
        setup_completion(agent, client)
    banner_animated(__version__)
    tool_note = "native tools" if agent.native else "text tool protocol"
    ui.meta(f"  {c(agent.model, 'petal', bold=True)}  {badges(agent.info)}  "
            + c(f"context {_k(agent.info['ctx'])}, using up to {_k(agent.ctx.limit)}  {tool_note}  perm {agent.permission}", "mist"))
    ui.meta(c("  type / for commands, @ to attach files, ! to run a shell command; esc stops lotus while it works", "mist"))
    if plugins.ERRORS:
        ui.warn(f"{len(plugins.ERRORS)} plugin(s) failed to load; /plugins for details")
    pc = cfg.get("project") or {}
    agent.keep_rem = pc.get("rem", True)
    notes = memory.note_items(agent.root)
    if notes:
        counts = ", ".join(f"{len(v)} {k}" for k, v in notes.items() if v) or "empty"
        ui.meta(c(f"  {G['bud']} ", "bloom") + c("LOTUS.md", "petal") + c(f"  {counts} · /note to see it", "mist"))
    if agent.keep_rem and not agent.messages:
        agent.rem = memory.load_rem(agent.root)
        if agent.rem:
            ui.meta(c(f"  {G['bud']} ", "bloom") + c("picking up where you left off", "petal")
                    + c(f" ({memory.ago(agent.rem[1])}) · /rem to read it, /rem clear to start fresh", "mist"))
    last_ctrl_c = 0.0
    queued = []  # messages typed (and sent with Enter) while lotus was working
    while True:
        queued += ui.queued
        prefill(ui.typeahead)
        ui.queued, ui.typeahead = [], ""
        _, typed = theme.refresh()  # follow the terminal if it switched light/dark
        prefill(typed)
        try:
            if queued:
                line = queued.pop(0)
                sys.stdout.write("\n" + c(G["bloom"] + " ", "bloom") + c(line, "ink") + c("  (queued)", "mist") + "\n")
            elif editor:
                sys.stdout.write("\n")
                line = editor.read(take_prefill())
            else:
                text = take_prefill()
                if HAVE_RL and text:
                    def hook(text=text):
                        readline.insert_text(text)
                        readline.redisplay()
                        readline.set_pre_input_hook(None)
                    readline.set_pre_input_hook(hook)
                line = read_input(rl_safe("\n" + c(G["bloom"] + " ", "bloom")))
        except EOFError:
            break
        except KeyboardInterrupt:
            if time.time() - last_ctrl_c < 2:
                break
            last_ctrl_c = time.time()
            ui.info("press Ctrl+C again to quit")
            continue
        if not line.strip():
            continue
        try:
            if line.startswith("!"):
                run_bang(line, agent, ui)
                continue
            if line.startswith("/") and not line.startswith("//"):
                handle(line.strip(), agent, client, cfg, ui)
                continue
            text, images = expand_mentions(line, agent, ui)
            ui.write("\n")
            if images and "vision" not in agent.caps:
                vm = find_vision(client, cfg)
                if vm:
                    ui.info(f"{agent.model} can't see images; using {vm} for this message")
                    with agent.using(vm):
                        agent.turn(text, images)
                    status(agent, ui)
                    continue
                ui.warn("no vision model installed (try: ollama pull qwen2.5vl:3b); sending text only")
                images = []
            agent.turn(text, images)
            status(agent, ui)
        except EOFError:
            break
        except KeyboardInterrupt:
            ui.warn("interrupted")
        except OllamaError as e:
            ui.error(str(e))
    if HAVE_RL and not editor:
        try:
            readline.write_history_file(str(hist))
        except Exception:
            pass
    if agent.keep_rem:
        try:
            p = agent.save_rem(use_model=pc.get("rem_by_model", True))
            if p:
                ui.meta(c(f"  {G['bud']} ", "bloom") + c(f"noted where we left off in {p.name}", "mist"))
        except (OSError, KeyboardInterrupt) as e:
            ui.warn(f"couldn't save {memory.REM_FILE}: {e}")
    mcp.shutdown()
    browser.close()
    ui.meta("\n  " + c(G["bud"], "bloom") + c(" namaste", "petal") + c(" · session saved; resume with lotus -r\n", "mist"))
    return 0


def doctor(cfg, ui, client=None):
    client = client or Ollama(cfg["host"])
    rows = [["check", "result"]]
    try:
        rows.append(["ollama", f"{client.version()} at {client.host}"])
        ms = client.models()
        rows.append(["models", str(len(ms)) + (" (none: ollama pull qwen3:4b)" if not ms else "")])
        for m in ms[:12]:
            info = client.info(m["name"])
            rows.append(["  " + m["name"], f"ctx {_k(info['ctx'])}  " + ", ".join(x for x in info["caps"] if x != "completion")])
    except OllamaError as e:
        rows.append(["ollama", f"not reachable: {e}"])
    rows.append(["python", sys.version.split()[0]])
    rows.append(["curl (Tor)", shutil.which("curl") or "missing"])
    rows.append(["tor proxy", f"{cfg['tor']['proxy']} {'up' if web.tor_reachable(cfg['tor']['proxy']) else 'down'}"])
    rows.append(["ripgrep", shutil.which("rg") or "missing (grep falls back to Python)"])
    try:
        import playwright  # noqa: F401
        rows.append(["playwright", "installed"])
    except ImportError:
        rows.append(["playwright", "missing: pip install playwright && python -m playwright install chromium"])
    rows.append(["theme", f"{theme.MODE} ({theme.PREF}, from {theme.SOURCE})"])
    rows.append(["config", str(home() / "config.json")])
    rows.append(["plugins", f"{len(plugins.LOADED)} loaded, {len(plugins.ERRORS)} failed"])
    ui.block(table_lines(rows))
    return 0


def sub_run(argv, cfg, watch=False):
    ap = argparse.ArgumentParser(prog=f"lotus {'watch' if watch else 'run'}")
    ap.add_argument("recipe")
    ap.add_argument("input", nargs="*")
    ap.add_argument("-m", "--model")
    ap.add_argument("-y", "--yes", action="store_true", help="allow tools that change things without asking")
    if watch:
        ap.add_argument("--every", default="")
        ap.add_argument("--notify", action="store_true")
    a = ap.parse_args(argv)
    ui = UI()
    boot(cfg)
    client = Ollama(cfg["host"])
    user_input = " ".join(a.input)
    if not sys.stdin.isatty():
        user_input += "\n\n<stdin>\n" + sys.stdin.read() + "\n</stdin>"
    try:
        meta, prompt, packs = automation.build(a.recipe, user_input, cfg)
        model = pick_model(client, cfg, ui, a.model or meta.get("model"))
    except (FileNotFoundError, OllamaError) as e:
        ui.error(str(e))
        return 1
    every = automation.interval(a.every or meta.get("every", "1h")) if watch else 0
    while True:
        agent = Agent(client, cfg, model, ui, packs=cfg["packs"] + packs)
        agent.permission = "auto" if a.yes else agent.permission
        reply = agent.turn(prompt)
        if not ui.plain:
            ui.write("\n")
        automation.append_log(a.recipe, reply)
        if not watch:
            return 0
        if a.notify:
            system.notify(f"lotus: {a.recipe}", reply[:180])
        ui.info(f"next run in {a.every or meta.get('every', '1h')} (log: {automation.log_path(a.recipe)}); Ctrl+C to stop")
        try:
            time.sleep(every)
        except KeyboardInterrupt:
            return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = load_cfg()
    want = cfg.get("theme", "auto")
    if "--theme" in argv[:-1]:
        want = argv[argv.index("--theme") + 1]
    prefill(theme.set_theme(want))
    if argv and argv[0] in ("run", "watch"):
        return sub_run(argv[1:], cfg, watch=argv[0] == "watch")
    if argv and argv[0] == "doctor":
        boot(cfg)
        return doctor(cfg, UI())
    if argv and argv[0] == "models":
        try:
            cmd_models(Ollama(cfg["host"]), UI())
            return 0
        except OllamaError as e:
            UI().error(str(e))
            return 1
    if argv and argv[0] == "pull" and len(argv) > 1:
        cmd_pull(Ollama(cfg["host"]), UI(), argv[1])
        return 0
    if argv and argv[0] == "recipes":
        for k, v in automation.list_all().items():
            print(f"{k}\t{v}")
        return 0

    ap = argparse.ArgumentParser(prog="lotus", description="A lightweight local agent harness for Ollama, built for small models.",
                                 epilog="subcommands: run <recipe>, watch <recipe>, models, pull <model>, recipes, doctor")
    ap.add_argument("prompt", nargs="*", help="one-shot prompt (stdin is appended when piped)")
    ap.add_argument("-m", "--model")
    ap.add_argument("--host", help="Ollama URL (default $OLLAMA_HOST or localhost:11434)")
    ap.add_argument("-y", "--yes", action="store_true", help="allow tools that change things without asking")
    ap.add_argument("--readonly", action="store_true", help="never run tools that change things")
    ap.add_argument("--packs", help="comma-separated tool packs to start with, e.g. web,system")
    ap.add_argument("--think", choices=["auto", "on", "off", "low", "medium", "high"])
    ap.add_argument("--tor", action="store_true", help="route web tools through Tor")
    ap.add_argument("--theme", choices=["auto", "dark", "light"], help="colors (auto follows the terminal background)")
    ap.add_argument("--text-tools", action="store_true", help="force the text tool protocol")
    ap.add_argument("--ctx", type=int, help="cap the context window (tokens)")
    ap.add_argument("-r", "--resume", nargs="?", const="last", help="resume a saved session (default: the last one)")
    ap.add_argument("-v", "--version", action="version", version=f"lotus {__version__}")
    a = ap.parse_args(argv)

    if a.host:
        cfg["host"] = a.host
    if a.packs:
        cfg["packs"] = ["core"] + [p.strip() for p in a.packs.split(",") if p.strip()]
    if a.think:
        cfg["think"] = a.think
    if a.tor:
        cfg["tor"]["enabled"] = True
    if a.text_tools:
        cfg["tool_mode"] = "text"
    if a.ctx:
        cfg["ctx_max"] = a.ctx
    if a.yes:
        cfg["permission"] = "auto"
    if a.readonly:
        cfg["permission"] = "readonly"

    ui = UI()
    ui.show_thinking = cfg.get("show_thinking", True)
    boot(cfg)
    client = Ollama(cfg["host"])
    try:
        model = pick_model(client, cfg, ui, a.model)
        agent = Agent(client, cfg, model, ui)
    except OllamaError as e:
        ui.error(str(e))
        return 1
    if a.resume:
        try:
            agent.load(a.resume)
        except FileNotFoundError:
            ui.warn(f"no session '{a.resume}'")

    piped = "" if sys.stdin.isatty() else sys.stdin.read()
    prompt = " ".join(a.prompt)
    if prompt or piped:
        text = prompt + (f"\n\n<stdin>\n{piped}\n</stdin>" if piped else "")
        text, images = expand_mentions(text, agent, ui)
        if images and "vision" not in agent.caps:
            vm = find_vision(client, cfg)
            if vm:
                with agent.using(vm):
                    agent.turn(text, images)
                return 0
        agent.turn(text, images)
        if ui.plain:
            sys.stdout.write("\n")
        else:
            status(agent, ui)
        return 0
    return repl(agent, client, cfg, ui)


if __name__ == "__main__":
    sys.exit(main())
