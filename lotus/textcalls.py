"""Tool calling for models that don't do it natively, plus rescue parsing for models that
claim native support but still emit calls as text (common with small Qwen/Llama/Mistral)."""
import ast
import json
import re

TEXT_PROTOCOL = """To use a tool, reply with a tool block and stop:
<tool>{"name": "tool_name", "args": {"arg": "value"}}</tool>
You may send several blocks at once. Results come back inside <result> tags. When you have what you need, answer normally with no tool block.
Available tools:"""


def repair_json(s):
    """Best-effort JSON parse for the shapes small models actually produce:
    trailing commas, Python literals, single quotes, missing closing braces, prose around it."""
    if s is None:
        return None
    s = re.sub(r"^```\w*\s*|\s*```$", "", s.strip())
    m = re.search(r"[{\[].*[}\]]", s, re.S)
    bodies = [s] + ([m.group(0)] if m and m.group(0) != s else [])
    for body in bodies:
        no_trail = re.sub(r",\s*([}\]])", r"\1", body)
        jsonish = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", re.sub(r"\bNone\b", "null", no_trail)))
        closed = jsonish + "}" * max(0, jsonish.count("{") - jsonish.count("}")) + "]" * max(0, jsonish.count("[") - jsonish.count("]"))
        for cand in (body, no_trail, jsonish, closed):
            try:
                return json.loads(cand)
            except ValueError:
                pass
        for cand in (body, no_trail):
            try:
                v = ast.literal_eval(cand)
                if isinstance(v, (dict, list)):
                    return v
            except Exception:
                pass
        try:
            return json.loads(closed.replace("'", '"'))
        except ValueError:
            pass
    return None


def _norm(obj):
    if isinstance(obj, list):
        return [c for o in obj for c in _norm(o)]
    if not isinstance(obj, dict):
        return []
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else {}
    name = obj.get("name") or obj.get("tool") or fn.get("name")
    args = obj.get("args", obj.get("arguments", obj.get("parameters", fn.get("arguments", {}))))
    if isinstance(args, str):
        args = repair_json(args) or {"input": args}
    if not isinstance(name, str):
        return []
    return [{"name": name.strip(), "args": args if isinstance(args, dict) else {}}]


TAGGED = re.compile(r"<(tool|tool_call|function_call)>\s*(.*?)\s*</\1>", re.S)
FENCED = re.compile(r"```(?:tool|tool_call|json)?\s*\n(.*?)```", re.S)


def extract_calls(text, known):
    calls = []
    for m in TAGGED.finditer(text):
        calls += _norm(repair_json(m.group(2)))
    if calls:
        return calls
    m = re.search(r"<\|python_tag\|>(.*)", text, re.S)
    if m:
        calls = _norm(repair_json(m.group(1)))
    if not calls:  # fenced or bare JSON only counts if it names a real tool
        for m in FENCED.finditer(text):
            calls += [c for c in _norm(repair_json(m.group(1))) if c["name"] in known]
        s = text.strip()
        if not calls and s.startswith("{") and s.endswith("}"):
            calls = [c for c in _norm(repair_json(s)) if c["name"] in known]
    return calls


class Splitter:
    """Routes a content stream into text / think / tool channels as tags arrive."""
    TAGS = {"think": "think", "thinking": "think", "tool": "tool", "tool_call": "tool", "function_call": "tool"}

    def __init__(self):
        self.buf, self.mode, self.close = "", "text", ""
        self.kept = []  # what we store in history: text + tool blocks, no think

    def _emit(self, out, kind, s):
        if s:
            out.append((kind, s))
            if kind != "think":
                self.kept.append(s)

    def feed(self, s):
        self.buf += s
        out = []
        while self.buf:
            if self.mode == "text":
                i = self.buf.find("<")
                if i < 0:
                    self._emit(out, "text", self.buf)
                    self.buf = ""
                    break
                self._emit(out, "text", self.buf[:i])
                self.buf = self.buf[i:]
                m = re.match(r"<(think|thinking|tool|tool_call|function_call)>", self.buf)
                if m:
                    self.mode, self.close = self.TAGS[m.group(1)], f"</{m.group(1)}>"
                    if self.mode == "tool":
                        self.kept.append(m.group(0))
                    self.buf = self.buf[m.end():]
                    continue
                if len(self.buf) < 16 and any(f"<{t}>".startswith(self.buf) for t in self.TAGS):
                    break
                self._emit(out, "text", "<")
                self.buf = self.buf[1:]
            else:
                j = self.buf.find(self.close)
                if j < 0:
                    safe = len(self.buf) - (len(self.close) - 1)
                    if safe > 0:
                        self._emit(out, self.mode, self.buf[:safe])
                        self.buf = self.buf[safe:]
                    break
                self._emit(out, self.mode, self.buf[:j])
                if self.mode == "tool":
                    self.kept.append(self.close)
                self.buf = self.buf[j + len(self.close):]
                self.mode = "text"
        return out

    def flush(self):
        out = []
        self._emit(out, self.mode, self.buf)
        if self.mode == "tool":
            self.kept.append(self.close)
        self.buf = ""
        return out

    def stored(self):
        return "".join(self.kept).strip()
