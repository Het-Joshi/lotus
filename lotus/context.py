"""Context budgeting.

Ollama loads models at a small default context and silently drops whatever doesn't fit.
Lotus instead reads the model's real maximum, estimates the prompt, and asks for a num_ctx
step that fits. It only ever grows within a session, because each change forces Ollama to
reload the model, and it compacts history before the window overflows."""
import json

STEPS = [2048, 4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 262144]
IMAGE_TOKENS = 768


class Context:
    def __init__(self, model_max, cap=32768, floor=4096):
        self.model_max = model_max or 4096
        self.limit = max(1024, min(self.model_max, cap))
        self.floor = min(floor, self.limit)
        self.num_ctx = None
        self.ratio = 3.6  # chars per token; calibrated from Ollama's real counts
        self.last_prompt = 0
        self.last_est = 0

    def chars(self, n):
        return int(n / self.ratio) + (1 if n else 0)

    def text(self, s):
        return self.chars(len(s or ""))

    def messages(self, msgs):
        chars, imgs = 0, 0
        for m in msgs:
            chars += len(m.get("content") or "") + 12
            if m.get("tool_calls"):
                chars += len(json.dumps(m["tool_calls"]))
            imgs += len(m.get("images") or [])
        return self.chars(chars) + imgs * IMAGE_TOKENS

    def choose(self, est_prompt, reserve):
        need = est_prompt + reserve
        size = next((s for s in STEPS if s >= need), STEPS[-1])
        size = min(max(size, self.floor), self.limit)
        if self.num_ctx is None or size > self.num_ctx:
            self.num_ctx = size
        self.last_est = est_prompt
        return self.num_ctx

    def pressure(self, est_prompt, reserve=0):
        return (est_prompt + reserve) / self.limit

    def calibrate(self, prompt_chars, prompt_tokens):
        if prompt_tokens and prompt_tokens > 200:
            r = prompt_chars / prompt_tokens
            if 2.0 <= r <= 6.0:
                self.ratio = self.ratio * 0.6 + r * 0.4
            self.last_prompt = prompt_tokens

    def used(self):
        return max(self.last_prompt, self.last_est)
