"""Plugin loader. Any .py file in ~/.lotus/plugins/ or ./.lotus/plugins/ is imported at
startup; @tool and @command decorators inside it register themselves."""
import importlib.util
from pathlib import Path

from .config import home

LOADED, ERRORS = [], []


def plugin_dirs(cfg, cwd):
    dirs = [home() / "plugins", Path(cwd) / ".lotus" / "plugins"]
    dirs += [Path(d).expanduser() for d in cfg.get("plugin_dirs", [])]
    return dirs


def load_all(cfg, cwd):
    for d in plugin_dirs(cfg, cwd):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"lotus_plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                LOADED.append(str(f))
            except Exception as e:
                ERRORS.append(f"{f.name}: {type(e).__name__}: {e}")
    return LOADED, ERRORS
