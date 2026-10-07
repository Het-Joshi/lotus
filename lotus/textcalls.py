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


class LoopGuard:
    """Notices a model going round in circles while it streams.

    Small models, reasoning ones especially, sometimes fall into a loop: the same passage
    over and over, or the same few sentences ("Wait, but the user said...") with small
    variations. Two checks over the recent text:
    - exact: the text ends in a passage of 50+ characters repeated `reps` times in a row,
      covering at least `min_span` characters
    - near (reasoning only): one sentence of 30+ characters seen 4+ times
    Both are cheap, and only run every ~120 characters."""

    def __init__(self, reps=4, near=True, window=8000, min_span=240):
        self.reps, self.near, self.window, self.min_span = reps, near, window, min_span
        self.period = 0
        self.text, self.total, self._since = "", 0, 0

    def feed(self, s):
        """Add streamed text; returns a reason string when it's looping, else None."""
        if not s:
            return None
        self.text = (self.text + s)[-self.window:]
        self.total += len(s)
        self._since += len(s)
        if self._since < 120:
            return None
        self._since = 0
        return self._exact() or (self._sentences() if self.near else None)

    def _exact(self):
        t, n = self.text, len(self.text)
        for p in range(50, min(800, n // self.reps) + 1):
            tail = t[n - p:]
            if len(set(tail.strip())) <= 4:  # rules, dots, whitespace: not a thought
                continue
            if p * self.reps < self.min_span:
                continue
            if all(t[n - (k + 1) * p:n - k * p] == tail for k in range(1, self.reps)):
                self.period = p
                return f"repeated the same passage {self.reps} times"
        return None

    def _sentences(self):
        seen = {}
        for s in re.split(r"(?<=[.!?])\s+|\n+", self.text):
            key = " ".join(s.lower().split())
            if len(key) >= 30:
                seen[key] = seen.get(key, 0) + 1
                if seen[key] >= 4:
                    return "kept coming back to the same thought"
        return None

    def trim(self, text):
        """Cut text back to one copy of the repeated passage, from where the repeating began."""
        p = self.period
        if not p or len(text) < 2 * p:
            return text
        unit = text[-p:]
        p = next((q for q in range(1, p) if p % q == 0 and unit == unit[:q] * (p // q)), p)  # smallest repeating unit
        j = len(text) - p - 1
        while j >= 0 and text[j] == text[j + p]:
            j -= 1
        start = j + 1
        return text[:start + p].rstrip() if len(text) - start >= 2 * p else text
