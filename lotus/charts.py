"""Terminal charts from a tiny JSON spec. Models emit ```chart blocks or call show_chart.

{"type": "bar",  "title": "...", "labels": [...], "values": [...]}
{"type": "line", "labels": [...], "series": {"cpu": [...], "mem": [...]}}   (or "values")
{"type": "spark", "values": [...]}
{"type": "pie",  "labels": [...], "values": [...]}
{"type": "graph", "edges": [["a", "b"], ["b", "c"]]}   (or {"nodes": {"a": ["b"]}})
{"type": "table", "rows": [["h1", "h2"], ["a", "b"]]}
"""
from .theme import c, code, RESET, COLOR, mix, vlen, width, pad, trunc

SERIES = ["bloom", "stamen", "leaf", "pond", "petal", "thorn"]
EIGHTHS = " ▏▎▍▌▋▊▉█"
SPARK = "▁▂▃▄▅▆▇█"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt(v):
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:.1f}k"
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.3g}"


def _title(spec):
    t = spec.get("title")
    return [" " + c(t, "petal", bold=True)] if t else []


def _rgb(text, rgb):
    return f"{code(rgb)}{text}{RESET}" if COLOR else text


def bar(spec):
    values = [_num(v) for v in spec.get("values", [])]
    labels = [str(x) for x in (spec.get("labels") or [])] or [str(i + 1) for i in range(len(values))]
    W = width()
    lw = min(max((vlen(l) for l in labels), default=1), W // 3)
    vmax = max([abs(v) for v in values] + [1e-9])
    bw = max(10, W - lw - 14)
    lines = _title(spec)
    for label, v in zip(labels, values):
        frac = abs(v) / vmax
        cells = frac * bw
        full, part = int(cells), int((cells - int(cells)) * 8)
        body = "█" * full + (EIGHTHS[part] if part else "")
        lines.append(f" {pad(trunc(label, lw), lw)} {c('│', 'mist')}{_rgb(body, mix('petal', 'bloom', frac))} {c(_fmt(v), 'mist')}")
    return lines


def spark_str(values):
    vals = [_num(v) for v in values]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    return "".join(SPARK[min(7, int((v - lo) / span * 7.999))] for v in vals)


def spark(spec):
    vals = [_num(v) for v in spec.get("values", [])]
    if not vals:
        return []
    s = spark_str(vals)
    return _title(spec) + [f" {c(s, 'bloom')}  {c(f'min {_fmt(min(vals))}  max {_fmt(max(vals))}  last {_fmt(vals[-1])}', 'mist')}"]


def line(spec):
    series = spec.get("series")
    if isinstance(series, list):
        series = {f"s{i + 1}": s for i, s in enumerate(series)} if series and isinstance(series[0], list) else {"": series}
    if not series:
        series = {"": spec.get("values", [])}
    series = {k: [_num(v) for v in vs] for k, vs in series.items() if vs}
    if not series:
        return ["(no data)"]
    n = max(len(v) for v in series.values())
    labels = [str(x) for x in (spec.get("labels") or [])]
    H = max(4, min(int(_num(spec.get("height", 10)) or 10), 30))
    allv = [v for vs in series.values() for v in vs]
    lo, hi = min(allv), max(allv)
    if hi == lo:
        hi = lo + 1
    yw = max(len(_fmt(hi)), len(_fmt(lo)), len(_fmt((hi + lo) / 2)))
    plot_w = max(10, width() - yw - 6)
    if n > 1 and (n - 1) + 1 > plot_w:
        idxs = [round(i * (n - 1) / (plot_w - 1)) for i in range(plot_w)]
        step = 1
    else:
        idxs = list(range(n))
        step = max(1, min(6, plot_w // max(1, n - 1))) if n > 1 else 1
    cols = (len(idxs) - 1) * step + 1
    grid = [[None] * cols for _ in range(H)]

    def row(v):
        return H - 1 - round((v - lo) / (hi - lo) * (H - 1))

    for si, (_, vals) in enumerate(series.items()):
        col = SERIES[si % len(SERIES)]
        pts = [(k * step, row(vals[i])) for k, i in enumerate(idxs) if i < len(vals)]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            for x in range(x0 + 1, x1):
                y = round(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
                if grid[y][x] is None:
                    grid[y][x] = ("·", col)
            if abs(y1 - y0) > 1 and step == 1:
                for y in range(min(y0, y1) + 1, max(y0, y1)):
                    if grid[y][x1] is None:
                        grid[y][x1] = ("│", col)
        for x, y in pts:
            grid[y][x] = ("●" if step > 1 else "•", col)
    lines = _title(spec)
    for r in range(H):
        lab = _fmt(hi) if r == 0 else _fmt(lo) if r == H - 1 else _fmt((hi + lo) / 2) if r == H // 2 else ""
        cells = "".join(c(cell[0], cell[1]) if cell else " " for cell in grid[r])
        lines.append(f" {lab:>{yw}} {c('┤' if lab else '│', 'mist')}{cells}")
    lines.append(" " * (yw + 2) + c("└" + "─" * cols, "mist"))
    if labels:
        first, last = labels[0], labels[min(len(labels), n) - 1]
        gap = max(1, cols - vlen(first) - vlen(last))
        lines.append(" " * (yw + 3) + c(first + " " * gap + last, "mist"))
    if len(series) > 1 or list(series)[0]:
        legend = "   ".join(c("●", SERIES[i % len(SERIES)]) + " " + c(k, "mist") for i, k in enumerate(series))
        lines.append(" " * (yw + 3) + legend)
    return lines


def pie(spec):
    values = [max(0.0, _num(v)) for v in spec.get("values", [])]
    labels = [str(x) for x in (spec.get("labels") or [])] or [str(i + 1) for i in range(len(values))]
    total = sum(values) or 1
    W = width() - 4
    lines = _title(spec)
    barline, used = [], 0
    for i, v in enumerate(values):
        w = round(v / total * W) if i < len(values) - 1 else W - used
        used += w
        barline.append(c("█" * max(0, w), SERIES[i % len(SERIES)]))
    lines.append(" " + "".join(barline))
    for i, (l, v) in enumerate(zip(labels, values)):
        lines.append(f" {c('■', SERIES[i % len(SERIES)])} {pad(l, 18)} {c(f'{v / total * 100:5.1f}%', 'mist')}  {c(_fmt(v), 'mist')}")
    return lines


def graph(spec):
    adj = {}
    if isinstance(spec.get("nodes"), dict):
        adj = {str(k): [str(x) for x in v] for k, v in spec["nodes"].items()}
    for e in spec.get("edges", []):
        if isinstance(e, (list, tuple)) and len(e) >= 2:
            adj.setdefault(str(e[0]), []).append(str(e[1]))
            adj.setdefault(str(e[1]), [])
    if not adj:
        return ["(empty graph)"]
    targets = {t for vs in adj.values() for t in vs}
    roots = [n for n in adj if n not in targets] or [next(iter(adj))]
    lines, seen = _title(spec), set()

    def walk(node, prefix, last, top):
        branch = "" if top else ("└── " if last else "├── ")
        if node in seen:
            lines.append(" " + c(prefix + branch, "mist") + c(f"{node} ↺", "mist"))
            return
        seen.add(node)
        lines.append(" " + c(prefix + branch, "mist") + c(node, "petal" if top else "ink", bold=top))
        kids = adj.get(node, [])
        for i, k in enumerate(kids):
            walk(k, prefix + ("" if top else ("    " if last else "│   ")), i == len(kids) - 1, False)

    for r in roots:
        walk(r, "", True, True)
    for n in adj:  # disconnected cycles
        if n not in seen:
            walk(n, "", True, True)
    return lines


def render(spec):
    from .render import table_lines
    if not isinstance(spec, dict):
        return [c("chart spec must be a JSON object", "thorn")]
    kind = str(spec.get("type", "bar")).lower()
    try:
        if kind == "table":
            return _title(spec) + table_lines(spec.get("rows", []))
        fn = {"bar": bar, "line": line, "spark": spark, "sparkline": spark, "pie": pie,
              "donut": pie, "graph": graph, "tree": graph}.get(kind)
        if not fn:
            return [c(f"unknown chart type '{kind}' (bar, line, spark, pie, graph, table)", "thorn")]
        return fn(spec)
    except Exception as e:  # never let a bad spec crash the session
        return [c(f"could not draw chart: {e}", "thorn")]
