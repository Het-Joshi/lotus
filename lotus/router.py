"""Pick tool packs for a request before the model starts on it.

A small model's worst mistakes come on the first step: it loads the wrong pack, or reaches
for shell because the right tool isn't in front of it. The router takes that choice away.

1. Rules: cheap and deterministic. Words like "price", "near me" or a URL turn on web.
2. If no rule fires, one short call to the same model with Ollama's `format` set to a JSON
   schema. Decoding is constrained to that schema, so the answer can only be real pack names.

The router only ever adds packs; the model can still load others itself."""
import json
import re

from . import tools as T

W = r"(?<![\w-])(?:{})(?![\w-])"

# pack -> pattern. Browser costs ~15 tool schemas, so it needs a clear signal.
RULES = {
    "web": W.format(
        r"prices?|pricing|costs?|cheap(?:er|est)?|afford\w*|budget|buy(?:ing)?|deals?|discounts?|on sale|"
        r"in stock|availab\w+|near me|nearby|stores?|shops?|shopping|retailers?|"
        r"news|latest|newest|recent(?:ly)?|today|tonight|tomorrow|yesterday|right now|currently|"
        r"this (?:week|month|year)|20[2-3]\d|weather|forecast|scores?|exchange rate|stock price|"
        r"release(?:d| date)?|reviews?|rated|ratings|best .{1,40} (?:for|under|in)|under \$?\d+|\$\s?\d+|"
        r"look (?:it )?up|search(?: for| the web| online)?|google|online|websites?|web ?pages?|"
        r"who is|what happened|how much") + r"|https?://\S+",
    "browser": W.format(
        r"use (?:the|a|my) browser|in (?:the|a) browser|browse to|log ?in(?:to)?|sign ?in|fill (?:in|out)|"
        r"submit (?:the|a) form|click|add to cart|check ?out|book (?:a|the)|screenshot (?:of )?(?:the|this|that) (?:site|page)|"
        r"\S+\.onion|(?:on|at|from) (?:www\.)?[\w-]+\.(?:com|net|org|io|co|ca|co\.uk|de|in)"),
    "research": W.format(
        r"papers?|arxiv|pubmed|preprints?|citations?|cite|bibtex|doi|journals?|peer.reviewed|"
        r"literature|studies|meta.analysis|academic"),
    "security": W.format(
        r"cve-\d{4}-\d+|cves?|vulnerabilit\w+|vulnerable|osv|audit (?:my |the )?(?:deps|dependencies)|"
        r"secrets? scan|leaked (?:keys|secrets)|tls|ssl|certificate|security headers|open ports?|port scan"),
    "system": W.format(
        r"clipboard|copy (?:it|this|that) to|paste|notif(?:y|ication)|remind me|open (?:the )?app|launch|"
        r"system info|battery|cpu usage|ram usage"),
}
COMPILED = {p: re.compile(rx, re.I) for p, rx in RULES.items()}

ASK = """You decide which tool packs an assistant needs to handle a user's request. The assistant always has core (files, shell, memory).
Packs:
{packs}
Return only the packs whose tools the request clearly needs. Return [] when it can be answered from general knowledge or with core alone."""


def by_rules(text, candidates):
    return [p for p in candidates if p in COMPILED and COMPILED[p].search(text)]


def by_model(agent, text, candidates):
    lines = []
    for p in candidates:
        names = [t.name for t in T.pack_tools(p)][:6]
        lines.append(f"- {p}: {T.PACKS[p]}" + (f" [{', '.join(names)}]" if names else ""))
    schema = {"type": "object", "required": ["packs"],
              "properties": {"packs": {"type": "array", "items": {"type": "string", "enum": candidates}}}}
    msgs = [{"role": "system", "content": ASK.format(packs="\n".join(lines))},
            {"role": "user", "content": text[-1500:]}]
    # the same num_ctx the turn itself will ask for: a different size makes Ollama reload the model
    num_ctx = agent.ctx.num_ctx or agent.ctx.choose(agent._prepare()[2] + agent.ctx.text(text), agent.cfg["reserve_tokens"])
    r = agent.client.chat(agent.model, msgs, stream=False, fmt=schema,
                          options={"num_ctx": num_ctx, "temperature": 0, "num_predict": 80},
                          think=False if "thinking" in agent.caps else None, keep_alive=agent.cfg.get("keep_alive"))
    content = re.sub(r"<think>.*?</think>", "", (r.get("message") or {}).get("content") or "", flags=re.S)
    try:
        picked = json.loads(content).get("packs") or []
    except (ValueError, AttributeError):
        return []
    return [p for p in candidates if p in picked]


def route(agent, text):
    """Packs to turn on for this request, and how they were chosen ("rules" or "model")."""
    mode = agent.cfg.get("router", "auto")
    if mode in (False, None, "off") or not text.strip():
        return [], ""
    candidates = [p for p in agent.idle_packs() if not p.startswith("mcp:") and p != "agents"]
    if not candidates:
        return [], ""
    picked = by_rules(text, candidates)
    if picked:
        return picked, "rules"
    # the model fallback costs a short call, so only for the main agent and real requests
    if mode != "auto" or agent.depth or len(text.split()) < 4:
        return [], ""
    try:
        return by_model(agent, text, candidates), "model"
    except Exception:
        return [], ""
