"""Tiny MCP (Model Context Protocol) stdio client, so any MCP server becomes a tool pack.

~/.lotus/config.json:
  "mcp": {"fs": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/me"],
                 "description": "files in my home folder", "trust": false}}

Servers start only when their pack is loaded (by you with /tools mcp:fs, or by the model)."""
import json
import os
import re
import subprocess
import threading

from . import __version__
from . import tools as T

SERVERS = {}


class MCPServer:
    def __init__(self, name, spec):
        cmd = spec["command"]
        if isinstance(cmd, str):
            cmd = cmd.split()
        self.name = name
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", bufsize=1, env={**os.environ, **spec.get("env", {})},
                                     shell=(os.name == "nt"))
        self.lock, self.next_id = threading.Lock(), 0
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "lotus", "version": __version__}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"MCP server '{self.name}' exited")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"MCP {method}: {msg['error'].get('message', msg['error'])}")
                    return msg.get("result", {})

    def tools(self):
        return self.request("tools/list", {}).get("tools", [])

    def call(self, name, args):
        r = self.request("tools/call", {"name": name, "arguments": args})
        parts = []
        for item in r.get("content", []):
            if item.get("type") == "text":
                parts.append(item["text"])
            elif item.get("type") == "resource":
                parts.append(json.dumps(item.get("resource", {}))[:4000])
            else:
                parts.append(f"[{item.get('type')} content]")
        text = "\n".join(parts)
        return ("error: " + text) if r.get("isError") else text

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def register_packs(cfg):
    for name, spec in (cfg.get("mcp") or {}).items():
        T.PACKS[f"mcp:{name}"] = spec.get("description") or f"tools from MCP server '{name}'"


def activate(name, cfg):
    spec = (cfg.get("mcp") or {}).get(name)
    if not spec:
        raise RuntimeError(f"no MCP server '{name}' in config")
    srv = SERVERS.get(name) or MCPServer(name, spec)
    SERVERS[name] = srv
    names = []
    for t in srv.tools():
        tname = re.sub(r"[^a-zA-Z0-9_]", "_", f"{name}_{t['name']}")[:60]
        schema = t.get("inputSchema") or {}

        def fn(_server=srv, _tool=t["name"], **kw):
            return _server.call(_tool, kw)

        T.TOOLS[tname] = T.Tool(tname, (t.get("description") or "")[:300], fn, schema.get("properties", {}),
                                schema.get("required", []), f"mcp:{name}", danger=not spec.get("trust", False))
        names.append(tname)
    return names


def shutdown():
    for s in SERVERS.values():
        s.close()
