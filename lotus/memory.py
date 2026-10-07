"""Memory, all plain text you can edit by hand:
- ~/.lotus/memory.md   facts about you, one "- fact" per line. Only the few relevant to the
                       current message are injected, so memory never crowds a small window.
- LOTUS.md             the project's notes, in the folder lotus was launched in: your
                       instructions, what the project is, and facts lotus learned there
                       (project_note, /note, /init). AGENTS.md is read if there's no LOTUS.md.
- .LOTUS_REM.txt       where the last session in that folder left off. Rewritten when a
                       session ends (and when history is compacted), read when the next starts."""
import datetime
import re
import time
from pathlib import Path

from .config import home

STOP = set("the and for you your are was with that this have has not but can what when how why who "
           "from about into they them their there then than just like want need make get use".split())


def _path():
    return home() / "memory.md"


def facts():
    p = _path()
    if not p.exists():
        return []
    return [l[2:].strip() for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]


def _write(items):
    _path().write_text("".join(f"- {f}\n" for f in items), encoding="utf-8")


def add(fact):
    fact = " ".join(fact.split())
    items = facts()
    if fact and fact not in items:
        items.append(fact)
        _write(items)
    return fact


def forget(selector):
    items = facts()
    sel = str(selector).strip()
    if sel.isdigit() and 1 <= int(sel) <= len(items):
        gone = items.pop(int(sel) - 1)
    else:
        hits = [f for f in items if sel.lower() in f.lower()]
        if not hits:
            return None
        gone = hits[0]
        items.remove(gone)
    _write(items)
    return gone


def _words(s):
    return {w for w in re.findall(r"[a-z0-9]{3,}", s.lower()) if w not in STOP}


def relevant(query, k=5):
    items = facts()
    if len(items) <= k:
        return items
    q = _words(query)
    scored = sorted(((len(q & _words(f)), i, f) for i, f in enumerate(items)), key=lambda x: (-x[0], x[1]))
    return [f for s, _, f in scored[:k] if s > 0]


def search(query, k=10):
    q = _words(query)
    items = facts()
    scored = [(len(q & _words(f)), f) for f in items]
    return [f for s, f in sorted(scored, key=lambda x: -x[0]) if s > 0][:k]


# ── LOTUS.md ────────────────────────────────────────────────────────────────

NOTES_FILE = "LOTUS.md"
REM_FILE = ".LOTUS_REM.txt"
SECTIONS = {"instructions": "Instructions", "project": "Project", "memory": "Memory"}
TEMPLATE = """# LOTUS.md

Notes lotus reads at the start of every session in this folder. Edit them freely.

## Instructions
<!-- how lotus should work here: conventions, commands to use, things to avoid -->

## Project
<!-- what this is, how it's laid out, how to build, run and test it -->

## Memory
<!-- facts and decisions lotus saved while working here -->
"""
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def notes_path(root):
    return Path(root) / NOTES_FILE


def _split(text):
    """[(heading or '', [lines])], in file order. The part before the first ## has heading ''."""
    out = [("", [])]
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*\S)\s*$", line)
        if m:
            out.append((m.group(1), []))
        else:
            out[-1][1].append(line)
    return out


def _join(parts):
    text = []
    for head, lines in parts:
        if head:
            text.append(f"## {head}")
        text.extend(lines)
    return "\n".join(text).rstrip() + "\n"


def note_items(root):
    """{section key: [bullet text]} from LOTUS.md."""
    p = notes_path(root)
    if not p.is_file():
        return {}
    out = {}
    for head, lines in _split(p.read_text(encoding="utf-8", errors="replace")):
        key = next((k for k, v in SECTIONS.items() if v.lower() == head.lower()), None)
        if key:
            out[key] = [l[2:].strip() for l in lines if l.startswith("- ")]
    return out


def add_note(root, note, section="memory"):
    """Add a bullet under a section of LOTUS.md, creating the file or section if needed.
    Returns (path, created, added)."""
    note = " ".join(str(note).split()).lstrip("- ").strip()
    section = section if section in SECTIONS else "memory"
    p = notes_path(root)
    created = not p.exists()
    text = TEMPLATE if created else p.read_text(encoding="utf-8", errors="replace")
    parts = _split(text)
    head = SECTIONS[section]
    i = next((k for k, (h, _) in enumerate(parts) if h.lower() == head.lower()), None)
    if i is None:
        parts.append((head, [""]))
        i = len(parts) - 1
    lines = parts[i][1]
    if any(l[2:].strip().lower() == note.lower() for l in lines if l.startswith("- ")):
        return p, created, False
    while lines and not lines[-1].strip():  # append after the last non-blank line
        lines.pop()
    lines.append(f"- {note}")
    lines.append("")
    p.write_text(_join(parts), encoding="utf-8")
    return p, created, True


def forget_note(root, selector):
    """Remove a bullet by its number in /note's listing, or by matching text."""
    p = notes_path(root)
    if not p.is_file():
        return None
    parts = _split(p.read_text(encoding="utf-8", errors="replace"))
    bullets = [(i, j) for i, (_, lines) in enumerate(parts) for j, l in enumerate(lines) if l.startswith("- ")]
    sel = str(selector).strip()
    hit = None
    if sel.isdigit() and 1 <= int(sel) <= len(bullets):
        hit = bullets[int(sel) - 1]
    else:
        hit = next(((i, j) for i, j in bullets if sel.lower() in parts[i][1][j].lower()), None)
    if hit is None:
        return None
    gone = parts[hit[0]][1].pop(hit[1])[2:]
    p.write_text(_join(parts), encoding="utf-8")
    return gone


def ensure_notes(root):
    p = notes_path(root)
    if p.exists():
        return p, False
    p.write_text(TEMPLATE, encoding="utf-8")
    return p, True


def _fit(text, max_chars, path):
    """Shrink notes for the prompt. Instructions get up to 40% and the project description
    up to 30% of the room; memories fill the rest, newest first (they're appended)."""
    # drop the template's comments, its intro and empty sections: tokens are scarce
    keep = []
    for head, lines in _split(COMMENT_RE.sub("", text)):
        body = "\n".join(lines).strip()
        if not head:
            body = "\n".join(l for l in body.splitlines()
                             if l.strip() not in ("# LOTUS.md", TEMPLATE.splitlines()[2])).strip()
        if body:
            keep.append(f"## {head}\n{body}" if head else body)
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(keep)).strip()
    if len(text) <= max_chars:
        return text
    parts = {h.lower(): "\n".join(l).strip() for h, l in _split(text)}
    room = max_chars - 120
    ins = parts.get("instructions", "")[:int(room * 0.4)]
    proj = parts.get("project", "")[:int(room * 0.3)]
    left = room - len(ins) - len(proj)
    kept = []
    for line in reversed(parts.get("memory", "").splitlines()):
        if len(line) + 1 > left:
            break
        kept.insert(0, line)
        left -= len(line) + 1
    out = [f"## {h}\n{b}" for h, b in (("Instructions", ins), ("Project", proj), ("Memory", "\n".join(kept))) if b]
    return "\n\n".join(out) + f"\n…[shortened; read_file('{path}') for all of it]"


def project_notes(roots, max_chars=3000):
    """Notes for the system prompt from the first of roots (the launch folder, then the
    working directory and its git root) that has LOTUS.md, AGENTS.md or .lotus/notes.md."""
    seen, order = set(), []
    for r in ([roots] if isinstance(roots, (str, Path)) else roots):
        p = Path(r)
        git = next((q for q in (p, *p.parents) if (q / ".git").exists()), None)
        for cand in (p, git) if git else (p,):
            if cand not in seen:
                seen.add(cand)
                order.append(cand)
    for root in order:
        for name in (NOTES_FILE, "AGENTS.md", ".lotus/notes.md"):
            f = root / name
            if f.is_file():
                text = _fit(f.read_text(encoding="utf-8", errors="replace"), max_chars, f)
                return f"({f.name})\n{text}" if text else ""
    return ""


# ── .LOTUS_REM.txt ──────────────────────────────────────────────────────────

def rem_path(root):
    return Path(root) / REM_FILE


def load_rem(root):
    """(note body, saved at as a unix time) or None."""
    p = rem_path(root)
    if not p.is_file():
        return None
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    while lines and (lines[0].startswith("# lotus") or lines[0].startswith("updated ") or not lines[0].strip()):
        lines.pop(0)
    body = "\n".join(lines).strip()
    return (body, p.stat().st_mtime) if body else None


def save_rem(root, body, model=""):
    p = rem_path(root)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    head = f"# lotus · where we left off\nupdated {stamp}" + (f" · {model}" if model else "") + f" · {Path(root).resolve()}\n\n"
    p.write_text(head + body.strip() + "\n", encoding="utf-8")
    return p


def clear_rem(root):
    p = rem_path(root)
    if p.exists():
        p.unlink()
        return True
    return False


def ago(ts):
    s = max(0, time.time() - ts)
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= n:
            k = int(s // n)
            return f"{k} {unit}{'s' if k > 1 else ''} ago"
    return "just now"
