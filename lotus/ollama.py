"""Ollama client over the native /api endpoints (not the OpenAI-compat /v1 layer, which
ignores per-request num_ctx and mangles streamed tool calls). Stdlib only."""
import json
import re
import urllib.error
import urllib.request


class OllamaError(Exception):
    pass


class Ollama:
    def __init__(self, host="http://localhost:11434"):
        host = host.strip() or "http://localhost:11434"
        if not host.startswith("http"):
            host = "http://" + host
        if host.startswith("http://") and host.count(":") == 1:  # no port given
            host += ":11434"
        self.host = host.rstrip("/")
        self._info = {}

    def _open(self, path, payload=None, timeout=900):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.host + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(body).get("error", body)
            except ValueError:
                pass
            raise OllamaError(f"Ollama returned {e.code}: {body[:400]}")
        except urllib.error.URLError as e:
            raise OllamaError(f"Can't reach Ollama at {self.host} ({e.reason}). Start it with `ollama serve`, or set OLLAMA_HOST.")

    def _json(self, path, payload=None, timeout=60):
        with self._open(path, payload, timeout) as r:
            return json.loads(r.read() or b"{}")

    def _stream(self, path, payload):
        resp = self._open(path, payload)
        with resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                d = json.loads(raw)
                if d.get("error"):
                    raise OllamaError(d["error"])
                yield d

    def version(self):
        return self._json("/api/version").get("version", "?")

    def models(self):
        return self._json("/api/tags").get("models", [])

    def loaded(self):
        return self._json("/api/ps").get("models", [])

    def info(self, model):
        """Capabilities and true context length for a model (cached)."""
        if model in self._info:
            return self._info[model]
        d = self._json("/api/show", {"model": model})
        mi = d.get("model_info") or {}
        ctx = next((v for k, v in mi.items() if k.endswith(".context_length")), None)
        caps = list(d.get("capabilities") or [])
        if not caps:  # older Ollama: infer from template/families
            tmpl = d.get("template", "")
            caps = ["completion"]
            if ".Tools" in tmpl or "tools" in tmpl.lower():
                caps.append("tools")
            fams = (d.get("details") or {}).get("families") or []
            if d.get("projector_info") or "clip" in fams or "mllama" in fams:
                caps.append("vision")
        m = re.search(r"num_ctx\s+(\d+)", d.get("parameters", "") or "")
        det = d.get("details") or {}
        info = {
            "name": model,
            "caps": caps,
            "ctx": int(ctx) if ctx else 4096,
            "modelfile_ctx": int(m.group(1)) if m else None,
            "family": det.get("family", ""),
            "size": det.get("parameter_size", ""),
            "quant": det.get("quantization_level", ""),
        }
        self._info[model] = info
        return info

    def chat(self, model, messages, tools=None, options=None, think=None, stream=True, keep_alive=None, fmt=None):
        p = {"model": model, "messages": messages, "stream": stream}
        if tools:
            p["tools"] = tools
        if fmt:
            p["format"] = fmt  # "json" or a JSON schema the reply is constrained to
        if options:
            p["options"] = options
        if think is not None:
            p["think"] = think
        if keep_alive:
            p["keep_alive"] = keep_alive
        if stream:
            return self._stream("/api/chat", p)
        return self._json("/api/chat", p, timeout=900)

    def pull(self, model):
        return self._stream("/api/pull", {"model": model, "stream": True})
