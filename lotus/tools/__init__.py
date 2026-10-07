"""Tool registry. Tools are plain functions; the decorator reads their signature and
docstring to build the schema, so plugins are a few lines:

    from lotus.tools import tool, pack

    pack("weather", "current weather")

    @tool(pack="weather")
    def weather(city: str, _ctx=None):
        '''Current weather for a city.
        city: city name'''
        ...

A parameter named _ctx receives the running agent (cwd, ui, config, memory...).
Tools are grouped into packs; only active packs are sent to the model, which keeps the
prompt small. The model can switch packs on itself with load_tools(pack)."""
import inspect
import json
from dataclasses import dataclass, field

TYPE_MAP = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object",
            "str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}


@dataclass
class Tool:
    name: str
    desc: str
    fn: object
    params: dict
    required: list
    pack: str = "core"
    danger: bool = False
    wants_ctx: bool = False
    meta: dict = field(default_factory=dict)


TOOLS = {}
PACKS = {}
COMMANDS = {}


class Interrupted(KeyboardInterrupt):
    """Raised by a tool the user stopped; partial carries whatever output it had so far."""

    def __init__(self, partial=""):
        super().__init__()
        self.partial = partial


def pack(name, description):
    PACKS[name] = description


def _parse_doc(doc):
    lines = [l.strip() for l in (doc or "").strip().splitlines()]
    desc, params = [], {}
    for l in lines:
        if ":" in l and l.split(":", 1)[0].isidentifier() and " " not in l.split(":", 1)[0]:
            k, v = l.split(":", 1)
            params[k.strip()] = v.strip()
        elif l and l.lower() not in ("args:", "arguments:", "params:"):
            if not params:
                desc.append(l)
    return " ".join(desc), params


def tool(pack="core", danger=False, name=None, params=None):
    def deco(fn):
        desc, pdocs = _parse_doc(fn.__doc__)
        sig = inspect.signature(fn)
        props, req, wants = {}, [], False
        for p in sig.parameters.values():
            if p.name == "_ctx":
                wants = True
                continue
            if p.name.startswith("_") or p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
                continue
            ann = p.annotation if p.annotation is not p.empty else (type(p.default) if p.default not in (p.empty, None) else str)
            t = TYPE_MAP.get(ann, "string")
            props[p.name] = {"type": t, "description": pdocs.get(p.name, "")}
            if t == "array":
                props[p.name]["items"] = {"type": "string"}
            if p.default is p.empty:
                req.append(p.name)
        if params:
            for k, v in params.items():
                props.setdefault(k, {}).update(v)
        n = name or fn.__name__
        TOOLS[n] = Tool(n, desc, fn, props, req, pack, danger, wants)
        PACKS.setdefault(pack, pack)
        return fn
    return deco


def command(name, help=""):
    """Register a slash command for the REPL: fn(agent, arg_string)."""
    def deco(fn):
        COMMANDS[name.lstrip("/")] = (fn, help)
        return fn
    return deco


def pack_tools(p):
    return [t for t in TOOLS.values() if t.pack == p]


def schema(t):
    return {"type": "function", "function": {
        "name": t.name, "description": t.desc,
        "parameters": {"type": "object", "properties": t.params, "required": t.required}}}


def signature(t):
    args = ", ".join(f"{k}{'' if k in t.required else '?'}: {v.get('type', 'string')}" for k, v in t.params.items())
    short = t.desc.split(". ")[0].rstrip(".")
    return f"- {t.name}({args}) — {short}"


def preview(args):
    if not args:
        return ""
    if len(args) == 1:
        v = next(iter(args.values()))
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return json.dumps(args, ensure_ascii=False)


def coerce(t, args):
    """Forgive the argument mistakes small models make instead of failing the call."""
    from ..textcalls import repair_json
    args = dict(args or {})
    unknown = [k for k in args if k not in t.params]
    missing = [r for r in t.required if r not in args]
    if len(missing) == 1 and len(unknown) == 1:  # {"cmd": "ls"} for shell(command)
        args[missing[0]] = args.pop(unknown[0])
    elif len(missing) == 1 and len(args) == 0:
        pass
    out = {}
    for k, v in args.items():
        if k not in t.params:
            continue
        typ = t.params[k].get("type", "string")
        try:
            if typ == "integer":
                v = int(float(v))
            elif typ == "number":
                v = float(v)
            elif typ == "boolean":
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "y")
            elif typ == "array" and not isinstance(v, list):
                parsed = repair_json(v) if isinstance(v, str) else None
                v = parsed if isinstance(parsed, list) else [v]
            elif typ == "object" and not isinstance(v, dict):
                parsed = repair_json(v) if isinstance(v, str) else None
                v = parsed if isinstance(parsed, dict) else {"value": v}
            elif typ == "string" and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        except (TypeError, ValueError):
            return out, f"argument '{k}' should be {typ}, got {v!r}"
        out[k] = v
    missing = [r for r in t.required if r not in out]
    if missing:
        return out, f"missing required argument(s): {', '.join(missing)}"
    return out, None


def call(t, args, ctx):
    if t.wants_ctx:
        return t.fn(**args, _ctx=ctx)
    return t.fn(**args)
