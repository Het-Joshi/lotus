"""Web search and page reading, optionally routed through Tor. No API keys.
Search uses DuckDuckGo's HTML endpoint by default, or your own SearXNG instance."""
import html
import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import pack, tool
from .. import safety

pack("web", "search the web and read pages (Tor optional)")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def http(url, tor=False, proxy="socks5h://127.0.0.1:9050", data=None, timeout=25):
    body = urllib.parse.urlencode(data).encode() if data else None
    if tor or ".onion" in urllib.parse.urlparse(url).netloc:
        curl = shutil.which("curl")
        if not curl:
            raise RuntimeError("Tor routing needs curl on PATH")
        hp = proxy.split("://")[-1]
        cmd = [curl, "-sSL", "--max-time", str(timeout), "--socks5-hostname", hp, "-A", UA]
        if body:
            cmd += ["--data", body.decode()]
        r = subprocess.run(cmd + [url], capture_output=True, timeout=timeout + 10)
        if r.returncode:
            raise RuntimeError(f"request over Tor failed: {r.stderr.decode(errors='replace').strip()[:200]} (is Tor running at {hp}?)")
        return r.stdout.decode("utf-8", "replace")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read(4_000_000).decode(charset, "replace")


def _tor(ctx, via_tor):
    return bool(via_tor or ctx.tor), ctx.cfg["tor"]["proxy"]


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "ul", "ol", "header", "footer", "main", "pre", "blockquote", "form"}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.links = base, [], []
        self.skip, self.in_title, self.title, self.href = 0, False, "", None

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n\n## ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.href = urllib.parse.urljoin(self.base, href)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            if self.href not in self.links:
                self.links.append(self.href)
            self.out.append(f" [{self.links.index(self.href) + 1}]")
            self.href = None
        elif tag in self.BLOCK or tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n")

    def handle_data(self, d):
        if self.in_title:
            self.title += d
        elif not self.skip:
            self.out.append(d)

    def text(self):
        t = "".join(self.out)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r" *\n *", "\n", t)
        return re.sub(r"\n{3,}", "\n\n", t).strip()


def page_text(raw, url):
    p = _Text(url)
    try:
        p.feed(raw)
    except Exception:
        pass
    return p.title.strip(), p.text(), p.links


@tool(pack="web")
def fetch_url(url: str, offset: int = 0, via_tor: bool = False, _ctx=None):
    """Read a web page as plain text (not in the browser). Links appear as [n] with a numbered list at the end. Use offset to read further.
    url: http(s) or .onion URL
    offset: character offset for long pages
    via_tor: route this request through Tor"""
    if not re.match(r"^https?://", url):
        if "://" in url or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)", url):
            return "error: only http and https addresses can be fetched"
        url = safety.with_scheme(url)
    why = safety.check_url(url, _ctx.cfg)
    if why and not _ctx.approve("fetch_url", f"{url}\nThis address is listed as {why}.", key="fetch_url:unsafe", force=True):
        return f"error: {urllib.parse.urlparse(url).hostname} is listed as {why}; it was not fetched. Tell the user."
    tor, proxy = _tor(_ctx, via_tor)
    raw = http(url, tor, proxy)
    if raw.lstrip().startswith(("{", "[")):
        warn = safety.guard_text(raw[:20000])
        return (warn + "\n" if warn else "") + raw[offset:offset + 8000]
    title, text, links = page_text(raw, url)
    chunk = text[offset:offset + 7000]
    more = f"\n[chars {offset}-{offset + len(chunk)} of {len(text)}; call again with offset={offset + len(chunk)} for more]" if offset + len(chunk) < len(text) else ""
    linkpart = "\n\nLinks:\n" + "\n".join(f"[{i + 1}] {l}" for i, l in enumerate(links[:40])) if links else ""
    warn = safety.guard_text(text)
    if safety.is_challenge(title, text):
        warn = (warn + "\n" if warn else "") + ("Note: this is a bot check / captcha page (sites often show these to Tor). "
                                                "Try another page on the same site, or open it with browser_open and "
                                                "browser_handoff so the user can solve it.")
    head = safety.UNTRUSTED + ("\n" + warn if warn else "")
    return f"# {title or url}\n{url}{' (via Tor)' if tor else ''}\n\n{head}\n{chunk}{more}{linkpart}"


def _strip(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _ddg(query, n, tor, proxy):
    raw = http("https://html.duckduckgo.com/html/", tor, proxy, data={"q": query, "kl": "wt-wt"})
    titles = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', raw, re.S)
    results = []
    for i, (href, title) in enumerate(titles):
        if "uddg=" in href:
            q = urllib.parse.urlparse(href if href.startswith("http") else "https:" + href).query
            href = urllib.parse.parse_qs(q).get("uddg", [href])[0]
        if "duckduckgo.com/y.js" in href:
            continue
        results.append((_strip(title), href, _strip(snippets[i]) if i < len(snippets) else ""))
        if len(results) >= n:
            break
    if not results and ("anomaly" in raw or "captcha" in raw.lower()):
        raise RuntimeError("DuckDuckGo is rate-limiting this connection. Set search.searxng_url in ~/.lotus/config.json to use SearXNG instead.")
    return results


def _searx(base, query, n, tor, proxy):
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    data = json.loads(http(url, tor, proxy))
    return [(r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in data.get("results", [])[:n]]


@tool(pack="web")
def web_search(query: str, n: int = 6, via_tor: bool = False, _ctx=None):
    """Search the web. Returns titles, URLs and snippets; use fetch_url to read a result.
    query: search terms
    n: number of results
    via_tor: route the search through Tor"""
    tor, proxy = _tor(_ctx, via_tor)
    sc = _ctx.cfg.get("search", {})
    results, note = None, ""
    base = sc.get("searxng_url") or ""
    if base:
        host = (urllib.parse.urlparse(base).hostname or "")
        local = host.endswith((".lan", ".local", ".internal", ".home")) or host in ("localhost", "127.0.0.1")
        if tor and local:
            note = f"(your SearXNG at {host} can't be reached through Tor; searched DuckDuckGo over Tor instead)\n"
        else:
            try:
                results = _searx(base, query, n, tor, proxy)
            except Exception as e:
                note = f"(your SearXNG at {host} isn't reachable: {str(e)[:80]}; searched DuckDuckGo instead)\n"
    if results is None:
        results = _ddg(query, n, tor, proxy)
    if not results:
        return note + "no results"
    rows = []
    for i, (t, u, snip) in enumerate(results):
        why = safety.check_url(u, _ctx.cfg) if u.startswith("http") else None
        flag = f"   WARNING: listed as {why}; don't open it\n" if why else ""
        rows.append(f"{i + 1}. {t}\n   {u}\n{flag}   {snip[:220]}")
    return note + ("(via Tor)\n" if tor else "") + "\n".join(rows)


def tor_reachable(proxy):
    import socket
    hp = proxy.split("://")[-1]
    host, _, port = hp.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 9050)), timeout=2):
            return True
    except OSError:
        return False
