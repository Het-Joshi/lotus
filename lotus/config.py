"""Config lives in ~/.lotus/config.json (override the folder with LOTUS_HOME)."""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "host": "http://localhost:11434",
    "model": "",
    "vision_model": "",          # used automatically when you attach an image to a text-only model
    "permission": "ask",          # ask | auto | readonly  (applies to tools that change things)
    "think": "auto",              # auto | on | off | low | medium | high
    "show_thinking": True,
    "theme": "auto",              # auto (follow the terminal background) | dark | light
    "tool_mode": "auto",          # auto | native | text
    "packs": ["core", "render"],  # tool packs active at start; others load on demand
    "ctx_max": 32768,             # never ask Ollama for more than this (protects RAM/VRAM)
    "ctx_min": 4096,
    "reserve_tokens": 2048,       # room kept free for the reply
    "tool_output_share": 0.2,     # max share of the window one tool result may take
    "max_steps": 15,
    "think_budget": "auto",       # tokens of reasoning per step before it's cut short; auto scales with /think, 0 = no limit
    "max_output_tokens": 0,       # cap on one reply (Ollama num_predict); 0 = the model's default
    "repeat_penalty": None,       # e.g. 1.1 if a model keeps repeating itself; None = the model's default
    "temperature": None,
    "keep_alive": "30m",
    "shell": "",                  # e.g. "powershell" or "/bin/zsh"; empty = system default
    "subagents": {"parallel": 2, "max_steps": 8},
    "search": {"engine": "duckduckgo", "searxng_url": ""},
    "tor": {"enabled": False, "proxy": "socks5h://127.0.0.1:9050"},  # also routes the browser when on
    "safe_browsing": {
        "enabled": True,          # check pages against malware and phishing lists before they load
        "lists": ["urlhaus", "openphish"],  # add "phishing-database" for ~400k more phishing domains (11 MB)
        "google_api_key": "",     # optional: also ask Google Safe Browsing
        "allow_executables": False,  # let the browser download programs and scripts
        "ignore": [],             # sites to never flag, e.g. ["my-test-site.dev"]
    },
    "browser": {
        "headless": None,         # None: a window when there's a display, headless otherwise
        "cdp_url": "",            # e.g. http://localhost:9222 to drive your own Chrome
        "channel": "",            # "chrome" or "msedge" to use an installed browser
        "allow": [],              # if set, only these sites may be opened (e.g. ["wikipedia.org"])
        "block": [],              # sites that may never be opened
        "confirm_risky": True,    # ask before clicking buy / send / delete / submit-like buttons
        "dialogs": "dismiss",     # confirm() / prompt() dialogs: dismiss or accept (alerts are always accepted)
        "timeout": 10,            # seconds per click or type
        "nav_timeout": 45,        # seconds per page load
        "max_text": 4000,         # page text characters shown to the model
        "max_elements": 120,
    },
    "project": {
        "notes": True,            # the model may save project facts to LOTUS.md in the launch folder
        "rem": True,              # keep .LOTUS_REM.txt (where the last session left off) in the launch folder
        "rem_by_model": True,     # let the model write that note at exit; false writes a plain recap
    },
    "mcp": {},                    # {"name": {"command": ["npx", "-y", "pkg"], "env": {}, "trust": false}}
    "plugin_dirs": [],
}


def home():
    p = Path(os.environ.get("LOTUS_HOME") or (Path.home() / ".lotus"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    cfg = copy.deepcopy(DEFAULTS)
    path = home() / "config.json"
    if path.exists():
        try:
            _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
        except ValueError as e:
            print(f"lotus: ignoring broken {path}: {e}")
    else:
        path.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
    if os.environ.get("OLLAMA_HOST"):
        cfg["host"] = os.environ["OLLAMA_HOST"]
    if os.environ.get("LOTUS_MODEL"):
        cfg["model"] = os.environ["LOTUS_MODEL"]
    return cfg


def save(cfg):
    (home() / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
