"""Recipes: reusable prompts you can run headless, pipe into, or schedule.

~/.lotus/recipes/standup.md
    ---
    model: qwen3:8b
    packs: web, system
    ---
    Summarise what changed in this repo since yesterday: {{input}}

Run it:   lotus run standup "focus on the api folder"
Pipe it:  git log --since=yesterday | lotus run standup
Repeat:   lotus watch standup --every 1h   (or put `lotus run ...` in cron / Task Scheduler)
"""
import datetime
import re
import sys
import time
from pathlib import Path

from .config import home


def recipes_dir():
    d = home() / "recipes"
    d.mkdir(exist_ok=True)
    return d


def find(name):
    p = Path(name)
    if p.is_file():
        return p
    for cand in (Path.cwd() / ".lotus" / "recipes" / f"{name}.md", recipes_dir() / f"{name}.md"):
        if cand.is_file():
            return cand
    return None


def parse(path):
    text = Path(path).read_text(encoding="utf-8")
    meta = {}
    if text.startswith("---"):
        head, _, body = text[3:].partition("\n---")
        for line in head.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        text = body.lstrip("-\n")
    return meta, text.strip()


def list_all():
    seen = {}
    for d in (recipes_dir(), Path.cwd() / ".lotus" / "recipes"):
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                meta, body = parse(f)
                seen[f.stem] = meta.get("description") or body.splitlines()[0][:70] if body else ""
    return seen


def interval(s):
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", str(s).strip().lower())
    if not m:
        raise ValueError(f"bad interval {s!r}; use e.g. 90s, 15m, 2h, 1d")
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def build(name, user_input, cfg):
    path = find(name)
    if not path:
        raise FileNotFoundError(f"no recipe '{name}' (looked in {recipes_dir()} and ./.lotus/recipes)")
    meta, body = parse(path)
    prompt = body.replace("{{input}}", user_input) if "{{input}}" in body else (body + ("\n\n" + user_input if user_input else ""))
    packs = [p.strip() for p in meta.get("packs", "").split(",") if p.strip()]
    return meta, prompt, packs


def log_path(name):
    d = home() / "logs"
    d.mkdir(exist_ok=True)
    return d / f"{name}.log"


def append_log(name, text):
    with open(log_path(name), "a", encoding="utf-8") as f:
        f.write(f"\n## {datetime.datetime.now():%Y-%m-%d %H:%M}\n{text}\n")
