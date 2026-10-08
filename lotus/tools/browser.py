"""Browser control through Playwright (optional: pip install playwright && python -m playwright install chromium).

Pages are shown to the model as text plus numbered interactive elements, e.g.
  [3] button Sign in
  [4] input:email Email  (value="me@x.org")
so even a small model can act with browser_click("3") instead of writing selectors.
Set browser.cdp_url (e.g. http://localhost:9222) to drive your own running Chrome.

Control:
- Playwright runs on its own thread. Esc / Ctrl+C abandons a slow action at once without
  corrupting the browser, and the next action simply waits for it to finish.
- Clicks on buttons that look like they spend money, send, publish or delete ask first
  (unless permission is auto). browser.allow / browser.block limit which sites it may open.
- Dialogs are handled (alerts accepted, confirms dismissed and reported), popups become the
  current tab, downloads are saved to ~/.lotus/downloads.
- After an action, the page text is only resent if it changed, which keeps small contexts small.
- Safe browsing (see safety.py): known malware and phishing pages are blocked before they
  load, page text is marked as untrusted and checked for prompt injection, passwords and
  card numbers always need approval, and programs are never downloaded.
- Tor: with /tor on (or for any .onion address) Chromium is relaunched behind the Tor SOCKS
  proxy, with DNS resolved inside Tor, WebRTC kept off the network and QUIC disabled."""
import atexit
import concurrent.futures
import contextlib
import fnmatch
import os
import queue
import re
import sys
import threading
import time
from urllib.parse import urlparse

from . import Interrupted, pack, tool
from .. import safety
from ..config import home

pack("browser", "drive a real web browser (Tor and .onion capable): open, read, click, type, tabs, screenshot")

_S = {}  # browser state; only touched on the browser thread (reads from elsewhere are harmless)

RISKY = re.compile(r"\b(buy|purchase|pay|checkout|check out|place order|order now|add to cart|subscribe|unsubscribe|"
                   r"delete|remove|erase|send|post|publish|tweet|reply|submit|confirm|transfer|donate|book now|"
                   r"reserve|sign out|log ?out|deactivate|close account|cancel (?:subscription|account|order|plan))\b", re.I)

SNAP_JS = r"""
(opts) => {
  document.querySelectorAll('[data-lotus]').forEach(e => e.removeAttribute('data-lotus'));
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[onclick],[contenteditable=""],[contenteditable=true],' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=option],[role=combobox],[role=textbox]';
  const vh = innerHeight, out = [], labels = {}, secret = [];
  let n = 0, more = 0;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (r.bottom < -vh || r.top > vh * 3 || n >= opts.max) { more++; continue; }
    n++; e.setAttribute('data-lotus', String(n));
    const tag = e.tagName.toLowerCase();
    let kind = tag === 'input' ? 'input:' + (e.type || 'text') : (e.getAttribute('role') || tag);
    const field = tag === 'input' || tag === 'textarea' || tag === 'select';
    const lab = e.labels && e.labels[0] ? e.labels[0].innerText : '';
    let label = e.getAttribute('aria-label') || (field ? (lab || e.placeholder || e.name || e.title || e.id)
                                                        : (e.innerText || e.value || e.title || e.alt)) || '';
    if (!label.trim() && e.querySelector) { const img = e.querySelector('img[alt],svg[aria-label]'); if (img) label = img.getAttribute('alt') || img.getAttribute('aria-label') || ''; }
    label = label.trim().replace(/\s+/g, ' ').slice(0, 70);
    const bits = [];
    if (tag === 'input' || tag === 'textarea') {
      if (e.type === 'checkbox' || e.type === 'radio') bits.push(e.checked ? 'checked' : 'unchecked');
      else if (e.type === 'password') { if (e.value) bits.push('filled'); }
      else if (e.value) bits.push('value="' + e.value.slice(0, 40) + '"');
    }
    if (tag === 'select') {
      const o = e.options[e.selectedIndex];
      bits.push('selected="' + (o ? o.text.trim().slice(0, 30) : '') + '"');
      bits.push('options: ' + Array.from(e.options).slice(0, 8).map(o => o.text.trim().slice(0, 24)).join(' | ') + (e.options.length > 8 ? ' …' : ''));
    }
    if (e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true') bits.push('selected');
    const ex = e.getAttribute('aria-expanded'); if (ex) bits.push(ex === 'true' ? 'expanded' : 'collapsed');
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') bits.push('disabled');
    const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
    const ident = ((e.name || '') + ' ' + (e.id || '') + ' ' + label).toLowerCase();
    if (field && (e.type === 'password' || ac.startsWith('cc-') || ac.includes('password') || ac === 'one-time-code' ||
        /card.?number|cvv|cvc|security code|expir|iban|routing|account number|ssn|social security|\bpin\b|passcode/.test(ident))) {
      bits.push('sensitive'); secret.push(n);
    }
    if (r.top > vh || r.bottom < 0) bits.push('offscreen');
    if (tag === 'a') { try { const u = new URL(e.href); label += '  -> ' + (u.host === location.host ? '' : u.host) + u.pathname.slice(0, 50); } catch (x) {} }
    labels[n] = kind + ' ' + label;
    out.push('[' + n + '] ' + kind + ' ' + label + (bits.length ? '  (' + bits.join(', ') + ')' : ''));
  }
  const body = document.body, main = document.querySelector('main, [role=main], article');
  const useMain = main && main.innerText.trim().length > 400;
  let text = ((useMain ? main : body) || {innerText: ''}).innerText.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const sh = Math.max(document.documentElement.scrollHeight, body ? body.scrollHeight : 0);
  return {title: document.title, url: location.href, elements: out, labels, secret, text: text.slice(0, opts.chars), total: text.length,
          main: !!useMain, more, scroll: sh > vh ? Math.round(100 * scrollY / Math.max(1, sh - vh)) : 100,
          screens: Math.max(1, Math.round(sh / vh * 10) / 10)};
}
"""

FIND_JS = r"""
(q) => {
  const lines = (document.body ? document.body.innerText : '').split('\n').map(l => l.trim()).filter(Boolean);
  const hits = []; q = q.toLowerCase();
  for (let i = 0; i < lines.length && hits.length < 15; i++)
    if (lines[i].toLowerCase().includes(q)) hits.push((i > 0 ? lines[i - 1].slice(0, 80) + ' / ' : '') + lines[i].slice(0, 200));
  return hits;
}
"""

FLASH_JS = r"""
e => { const o = e.style.outline, f = e.style.outlineOffset;
       e.style.outline = '3px solid #e0569b'; e.style.outlineOffset = '2px';
       setTimeout(() => { e.style.outline = o; e.style.outlineOffset = f; }, 900); }
"""


# ── the browser thread ───────────────────────────────────────────────────────

class _Worker:
    """Runs every Playwright call on one thread. The caller waits in short slices, so a
    KeyboardInterrupt (Esc or Ctrl+C) lands between them instead of inside Playwright."""

    def __init__(self):
        self.q = queue.Queue()
        self.th = None

    def alive(self):
        return self.th is not None and self.th.is_alive()

    def call(self, fn, timeout=90):
        if not self.alive():
            self.th = threading.Thread(target=self._loop, name="lotus-browser", daemon=True)
            self.th.start()
        fut = concurrent.futures.Future()
        self.q.put((fn, fut))
        end = time.monotonic() + timeout
        try:
            while True:
                try:
                    return fut.result(timeout=0.1)
                except concurrent.futures.TimeoutError:
                    if time.monotonic() > end:
                        fut.cancel()
                        raise RuntimeError(f"the browser didn't finish within {timeout}s")
        except KeyboardInterrupt:
            fut.cancel()
            raise Interrupted("the browser action was abandoned; it may still finish in the background. "
                              "Call browser_snapshot to see where things stand.")

    def _loop(self):
        while True:
            fn, fut = self.q.get()
            if fn is None:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: B902 - handed to the waiting caller
                fut.set_exception(e)


_W = _Worker()


def _run(fn, timeout=90):
    try:
        return _W.call(fn, timeout)
    except Interrupted:
        raise
    except Exception as e:
        raise RuntimeError(_tidy(e)) from None


def _tidy(e):
    """Playwright errors carry a long call log; small models need one clear line."""
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    if "data-lotus" in str(e) or "waiting for locator" in str(e):
        msg += ". The element numbers may be stale: call browser_snapshot and use the new numbers."
    elif "Timeout" in msg:
        msg += ". The page may still be loading or the element is hidden; try browser_wait or browser_snapshot."
    return msg[:400]


def _stop():
    try:
        if _S.get("browser") and not _S.get("cdp"):
            _S["browser"].close()
        if _S.get("pw"):
            _S["pw"].stop()
    except Exception:
        pass
    _S.clear()


@atexit.register
def _shutdown():
    if _W.alive():
        try:
            _W.call(_stop, timeout=5)
        except BaseException:
            pass


# ── state (browser thread) ───────────────────────────────────────────────────

SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")  # "javascript:x", but not "localhost:3000"

_TRUSTED = set()  # hosts the user chose to open despite a safe-browsing warning (this session)


def _onion(url):
    return (urlparse(url).hostname or "").endswith(".onion")


def _bc(ctx, url=None, via_tor=False):
    """Browser settings for one call, plus whether it must run behind Tor: /tor on, via_tor,
    an .onion address, or a browser that is already on Tor. Once on Tor it stays on Tor until
    it is closed, so a later click can never quietly fall back to your real address."""
    bc = dict((ctx.cfg.get("browser") or {}) if ctx is not None else {})
    want = ctx is not None and (getattr(ctx, "tor", False) or via_tor or (url and _onion(url))
                                or (_S.get("context") is not None and bool(_S.get("tor"))))
    bc["_tor"] = _proxy(ctx) if want else None
    bc["_cfg"] = ctx.cfg if ctx is not None else {}
    return bc


def _host_ok(url, bc):
    host = (urlparse(url).hostname or "").lower()
    if not host or url.startswith(("about:", "data:", "chrome:")):
        return True

    def hit(pats):
        for p in pats or []:
            p = p.lower().strip()
            if host == p or host.endswith("." + p) or fnmatch.fnmatch(host, p):
                return True
        return False

    if hit(bc.get("block")):
        return False
    return not bc.get("allow") or hit(bc.get("allow"))


def _event(s):
    _S.setdefault("events", []).append(s)


def _wire(page, bc):
    def on_dialog(d):
        msg = f"the page showed a {d.type} dialog: {d.message[:200]!r}"
        accept = d.type in ("alert", "beforeunload") or bc.get("dialogs") == "accept"
        try:
            d.accept() if accept else d.dismiss()
        except Exception:
            pass
        _event(msg + (" (accepted)" if accept else " (dismissed; ask the user if it should have been accepted)"))

    page.on("dialog", on_dialog)
    page.on("download", lambda d: _S.setdefault("downloads", []).append(d))


def _proxy(ctx):
    """socks5://host:port for Chromium, from tor.proxy (Chromium resolves names through a SOCKS5 proxy)."""
    hp = ctx.cfg["tor"]["proxy"].split("://")[-1]
    return "socks5://" + hp


def _launch(bc):
    tor, cfg = bc.get("_tor"), bc.get("_cfg")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("browser tools need Playwright: pip install playwright && python -m playwright install chromium")
    pw = sync_playwright().start()
    _S["pw"] = pw
    if bc.get("cdp_url"):
        if tor:
            pw.stop()
            _S.clear()
            raise RuntimeError("Tor can't be applied to your own Chrome (browser.cdp_url); start that Chrome with "
                               f"--proxy-server={tor}, or clear cdp_url to let lotus launch a Tor-routed browser")
        b = pw.chromium.connect_over_cdp(bc["cdp_url"])
        context = b.contexts[0] if b.contexts else b.new_context()
        _S.update(cdp=True, mode=f"your Chrome at {bc['cdp_url']}")
    else:
        headless = bc.get("headless")
        if headless is None:
            headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        kw = {"headless": headless, "args": []}
        if bc.get("channel"):
            kw["channel"] = bc["channel"]  # "chrome" or "msedge" to use an installed browser
        ctx_kw = {"viewport": {"width": 1280, "height": 900}, "accept_downloads": True}
        if tor:
            kw["proxy"] = {"server": tor}
            proxy_host = tor.split("://")[-1].rsplit(":", 1)[0]
            kw["args"] += [
                # no DNS outside the proxy; only the proxy itself is reached directly
                f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {proxy_host} , EXCLUDE localhost",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",  # WebRTC can't reveal your IP
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-quic", "--dns-prefetch-disable", "--no-pings",
            ]
            ctx_kw.update(locale="en-US", timezone_id="UTC")  # like Tor Browser: don't give away where you are
        b = pw.chromium.launch(**kw)
        context = b.new_context(**ctx_kw)
        _S.update(cdp=False, mode=("headless" if headless else "window") + (" via Tor" if tor else ""))
    _S.update(browser=b, context=context, headless=_S["mode"].startswith("headless"), bc=bc, tor=tor, cfg=cfg)
    sb_on = safety._cfg(cfg)["enabled"]
    if bc.get("allow") or bc.get("block") or sb_on:
        def guard(route):
            req = route.request
            if req.is_navigation_request():
                host = urlparse(req.url).hostname
                if not _host_ok(req.url, bc):
                    _event(f"blocked navigation to {host} (not allowed by browser.allow / browser.block)")
                    return route.abort("blockedbyclient")
                why = safety.check_url(req.url, cfg, trusted=_TRUSTED) if sb_on else None
                if why:
                    _event(f"blocked {req.url[:100]}: it is listed as {why}. Tell the user; they can open it "
                           "deliberately with browser_open if they're sure")
                    return route.abort("blockedbyclient")
            return route.continue_()
        context.route("**/*", guard)

    def adopt(p):
        _wire(p, bc)
        if not _S.get("opening"):
            _event(f"a new tab opened ({p.url or 'loading'}) and is now the current tab")
        _S["page"] = p

    context.on("page", adopt)
    pages = context.pages
    for p in pages:
        _wire(p, bc)
    _S["page"] = pages[-1] if pages else _new_page()


def _new_page():
    _S["opening"] = True
    try:
        pg = _S["context"].new_page()
    finally:
        _S["opening"] = False
    _S["page"] = pg
    return pg


def _pages():
    return [p for p in _S["context"].pages if not p.is_closed()] if _S.get("context") else []


def _page(bc):
    tor = bc.get("_tor")
    if _S.get("context") and "_tor" in bc and _S.get("tor") != tor:
        # switching Tor on or off means a different browser: restart it, cleanly
        _stop()
        _event("the browser restarted " + ("behind Tor" if tor else "without Tor") + "; earlier tabs were closed")
    if not _S.get("context"):
        _launch(bc)
    pg = _S.get("page")
    if pg is None or pg.is_closed():
        rest = _pages()
        pg = rest[-1] if rest else _new_page()
        _S["page"] = pg
    return pg


def _settle(pg, quick=False):
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    if not quick:
        try:  # single-page apps: give fetches a moment, but never wait long
            pg.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass
    pg.wait_for_timeout(200)  # unlike time.sleep, this lets Playwright deliver events (popups, dialogs)
    if _S.get("page") is not pg and _S.get("page") is not None:  # a click opened a popup
        _settle(_S["page"], quick=True)


def _save_downloads():
    d = home() / "downloads"
    for dl in _S.pop("downloads", []):
        name = dl.suggested_filename
        if safety.is_executable(name) and not safety._cfg(_S.get("cfg")).get("allow_executables"):
            with contextlib.suppress(Exception):
                dl.cancel()
                dl.delete()
            _event(f"refused to download {name}: programs and scripts aren't downloaded (safe_browsing.allow_executables)")
            continue
        try:
            d.mkdir(exist_ok=True)
            path = d / dl.suggested_filename
            dl.save_as(str(path))
            _event(f"downloaded {path}")
        except Exception as e:
            _event(f"a download failed: {_tidy(e)}")


def _snapshot(bc, full=True):
    pg = _page(bc)
    _save_downloads()
    opts = {"max": int(bc.get("max_elements", 120)), "chars": int(bc.get("max_text", 4000))}
    d = pg.evaluate(SNAP_JS, opts)
    if _S.get("page") is not pg and _S.get("page") is not None:  # a popup took over while we were reading
        pg = _S["page"]
        _settle(pg, quick=True)
        d = pg.evaluate(SNAP_JS, opts)
        full = True
    _S["labels"] = d["labels"]
    _S["secret"] = {str(n) for n in d.get("secret", [])}
    key = (d["url"], hash(d["text"]))
    same = _S.get("last") == key
    _S["last"] = key
    pages = _pages()
    head = f"# {d['title'] or '(untitled)'}\n{d['url']}" + ("  (via Tor)" if _S.get("tor") else "")
    if len(pages) > 1 and pg in pages:
        head += f"   [tab {pages.index(pg) + 1} of {len(pages)}]"
    out = [head]
    events = _S.pop("events", [])
    u = urlparse(d["url"])
    if u.scheme == "http" and not (u.hostname or "").endswith(".onion") and u.hostname not in ("localhost", "127.0.0.1"):
        events.append("this page isn't encrypted (http); don't enter anything private here")
    if safety.is_challenge(d["title"], d["text"]):
        events.append("this is a bot check / captcha page, which sites often show to Tor and automated browsers. "
                      "Don't try to solve it. Try another page on the same site (e.g. a subdomain or a deeper link), "
                      "or call browser_handoff so the user can solve it in a window")
    if events:
        out.append("\n".join("Note: " + e for e in events))
    if same and not full:
        out.append("(page text unchanged since the last snapshot)")
    else:
        body = d["text"] or "(no text on the page)"
        if d["total"] > len(d["text"]):
            body += f"\n[showing the first {len(d['text'])} of {d['total']} chars; browser_find('words') searches the rest]"
        warn = safety.guard_text(d["text"])
        out.append(safety.UNTRUSTED + ("\n" + warn if warn else "") + "\n" + ("(main content)\n" if d["main"] else "") + body)
    pos = f"scrolled {d['scroll']}% of ~{d['screens']} screens" if d["screens"] > 1.1 else "the whole page fits on screen"
    els = "\n".join(d["elements"]) or "(none)"
    more = f"\n…and {d['more']} more further down (scroll to see them)" if d["more"] else ""
    out.append(f"Interactive elements ({pos}; use the number with browser_click / browser_type):\n{els}{more}")
    return "\n\n".join(out)


def _target(pg, target):
    t = str(target).strip().strip("[]")
    if t.isdigit():
        return pg.locator(f'[data-lotus="{t}"]').first
    if t.startswith(("css=", "#", ".", "//", "xpath=", "text=")):
        return pg.locator(t).first
    return pg.get_by_text(t, exact=False).first


def _flash(loc):
    if _S.get("headless"):
        return
    try:
        loc.evaluate(FLASH_JS, timeout=1500)
    except Exception:
        pass


def _label(target):
    t = str(target).strip().strip("[]")
    if t.isdigit():
        return (_S.get("labels") or {}).get(t, "")
    return t


# ── tools ────────────────────────────────────────────────────────────────────

@tool(pack="browser")
def browser_open(url: str, new_tab: bool = False, via_tor: bool = False, _ctx=None):
    """Open a URL in the browser and return the page text and numbered elements. For Tor set via_tor=true (.onion addresses always use Tor); never use third-party "Tor gateway" or proxy websites.
    url: address to open
    new_tab: open in a new tab instead of the current one
    via_tor: route the browser through Tor (it stays on Tor until closed)"""
    if "://" not in url and not SCHEME_RE.match(url):
        url = safety.with_scheme(url)
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https") and url != "about:blank":
        return f"error: only http and https pages can be opened (not {scheme}:)"
    bc = _bc(_ctx, url, via_tor)
    if not _host_ok(url, bc):
        return f"error: {urlparse(url).hostname} is not allowed by the browser.allow / browser.block settings"
    if bc["_tor"]:
        from .web import tor_reachable
        if not tor_reachable(_ctx.cfg["tor"]["proxy"]):
            return (f"error: Tor isn't reachable at {_ctx.cfg['tor']['proxy']}. Start Tor (the tor service, or Tor Browser "
                    "and set tor.proxy to socks5h://127.0.0.1:9150); nothing was opened")
    why = safety.check_url(url, _ctx.cfg, trusted=_TRUSTED)
    if why:
        host = urlparse(url).hostname
        if not _ctx.approve("browser_open", f"{url}\nThis address is listed as {why}.", key="browser_open:unsafe", force=True):
            return f"error: {host} is listed as {why}; it was not opened. Tell the user."
        _TRUSTED.add(host)

    def job():
        pg = _new_page() if new_tab and _S.get("context") else _page(bc)
        nav = int(bc.get("nav_timeout", 45)) * (2 if bc["_tor"] else 1)  # Tor circuits are slow
        pg.goto(url, wait_until="domcontentloaded", timeout=nav * 1000)
        if _onion(url):
            _S["onion"] = True
        _settle(pg)
        return _snapshot(bc)
    return _run(job, timeout=int(bc.get("nav_timeout", 45)) * 2 + 30)


@tool(pack="browser")
def browser_snapshot(_ctx=None):
    """Re-read the browser's current page: text plus numbered interactive elements. Pages read with fetch_url are not in the browser."""
    bc = _bc(_ctx)
    return _run(lambda: _snapshot(bc))


@tool(pack="browser")
def browser_click(target: str, _ctx=None):
    """Click an element by its number from the snapshot, or by its visible text.
    target: element number like '7', or visible text"""
    bc = _bc(_ctx)
    label = _label(target)
    if bc.get("confirm_risky", True) and RISKY.search(label.split("  -> ")[0]):
        if not _ctx.approve("browser_click", f"[{str(target).strip('[]')}] {label}\non {_S.get('last', ('?',))[0]}",
                            key="browser_click:risky"):
            return "error: the user declined this click. Ask them how to proceed."

    def job():
        pg = _page(bc)
        loc = _target(pg, target)
        _flash(loc)
        loc.click(timeout=int(bc.get("timeout", 10)) * 1000)
        _settle(pg)
        return _snapshot(bc, full=False)
    return _run(job)


@tool(pack="browser", danger=True)
def browser_type(target: str, text: str, submit: bool = False, _ctx=None):
    """Type into an input by its number from the snapshot (replaces what is there). Set submit to press Enter afterwards.
    target: element number or visible label
    text: text to type
    submit: press Enter after typing"""
    bc = _bc(_ctx)
    t = str(target).strip().strip("[]")
    if t in (_S.get("secret") or set()) and (_ctx.permission == "auto" or "browser_type" in _ctx.always):
        # passwords and card numbers are never typed without a fresh yes, whatever the permission mode
        if not _ctx.approve("browser_type", f"[{t}] {_label(t)}\nThis is a password or payment field on "
                            f"{_S.get('last', ('?',))[0]}", key="browser_type:secret", force=True):
            return "error: the user declined typing into this sensitive field."

    def job():
        pg = _page(bc)
        el = _target(pg, target)
        _flash(el)
        el.fill(text, timeout=int(bc.get("timeout", 10)) * 1000)
        if submit:
            el.press("Enter")
            _settle(pg)
        return _snapshot(bc, full=False)
    return _run(job)


KEYS = {"enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": " ",
        "backspace": "Backspace", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
        "right": "ArrowRight", "pagedown": "PageDown", "pageup": "PageUp", "home": "Home", "end": "End"}


@tool(pack="browser")
def browser_press(key: str, _ctx=None):
    """Press a key on the page, e.g. Enter, Escape, Tab, PageDown, or a combo like Control+a.
    key: key name"""
    bc = _bc(_ctx)
    k = "+".join(KEYS.get(p.strip().lower(), p.strip()[:1].upper() + p.strip()[1:] if len(p.strip()) > 1 else p.strip())
                 for p in key.split("+"))
    k = k.replace("Ctrl", "Control").replace("Cmd", "Meta")

    def job():
        pg = _page(bc)
        pg.keyboard.press(k)
        _settle(pg, quick=True)
        return _snapshot(bc, full=False)
    return _run(job)


@tool(pack="browser")
def browser_select(target: str, option: str, _ctx=None):
    """Choose an option in a dropdown (select element) by its visible text or value.
    target: element number of the select
    option: option text or value"""
    bc = _bc(_ctx)

    def job():
        pg = _page(bc)
        el = _target(pg, target)
        _flash(el)
        try:
            el.select_option(label=option, timeout=5000)
        except Exception:
            el.select_option(value=option, timeout=5000)
        _settle(pg, quick=True)
        return _snapshot(bc, full=False)
    return _run(job)


@tool(pack="browser")
def browser_scroll(direction: str = "down", _ctx=None):
    """Scroll the page.
    direction: down, up, top or bottom"""
    bc = _bc(_ctx)
    js = {"top": "scrollTo(0,0)", "bottom": "scrollTo(0,document.body.scrollHeight)",
          "up": "scrollBy(0,-innerHeight*0.8)"}.get(direction.lower().strip(), "scrollBy(0,innerHeight*0.8)")

    def job():
        pg = _page(bc)
        pg.evaluate(f"() => {js}")
        pg.wait_for_timeout(300)
        return _snapshot(bc, full=False)
    return _run(job)


@tool(pack="browser")
def browser_find(text: str, _ctx=None):
    """Search the whole current page for text (including parts not shown in the snapshot) and scroll to the first match.
    text: words to look for"""
    bc = _bc(_ctx)

    def job():
        pg = _page(bc)
        hits = pg.evaluate(FIND_JS, text)
        if not hits:
            return f"'{text}' was not found on {pg.url}"
        try:
            pg.get_by_text(text, exact=False).first.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        return f"{len(hits)} match(es) for '{text}' (previous line / matching line):\n" + "\n".join("- " + h for h in hits)
    return _run(job)


@tool(pack="browser")
def browser_wait(text: str = "", seconds: int = 5, _ctx=None):
    """Wait for text to appear on the page (or just wait a few seconds), then re-read it. Useful while a page is still loading.
    text: text to wait for; empty waits for the page to settle
    seconds: longest wait"""
    bc = _bc(_ctx)
    seconds = max(1, min(int(seconds), 30))

    def job():
        pg = _page(bc)
        note = ""
        if text:
            try:
                pg.get_by_text(text, exact=False).first.wait_for(timeout=seconds * 1000)
            except Exception:
                note = f"'{text}' did not appear within {seconds}s.\n\n"
        else:
            try:
                pg.wait_for_load_state("networkidle", timeout=seconds * 1000)
            except Exception:
                pass
        return note + _snapshot(bc)
    return _run(job, timeout=seconds + 30)


@tool(pack="browser")
def browser_back(_ctx=None):
    """Go back one page."""
    bc = _bc(_ctx)

    def job():
        pg = _page(bc)
        pg.go_back(timeout=15000)
        _settle(pg)
        return _snapshot(bc)
    return _run(job)


@tool(pack="browser")
def browser_tabs(action: str = "list", index: int = 0, _ctx=None):
    """List, switch to, open or close tabs.
    action: list, switch, new or close
    index: tab number for switch or close (from the list; 0 means the current tab)"""
    bc = _bc(_ctx)
    action = action.lower().strip()

    def job():
        _page(bc)
        pages = _pages()
        cur = _S.get("page")
        i = index - 1 if index else (pages.index(cur) if cur in pages else 0)
        if action in ("switch", "close") and not 0 <= i < len(pages):
            return f"error: there is no tab {index}; there are {len(pages)}"
        if action == "new":
            _new_page()
            return "opened a new empty tab; use browser_open to load a page"
        if action == "switch":
            _S["page"] = pages[i]
            pages[i].bring_to_front()
            return _snapshot(bc)
        if action == "close":
            pages[i].close()
            rest = _pages()
            _S["page"] = rest[min(i, len(rest) - 1)] if rest else None
            if not rest:
                return "closed the last tab"
            return f"closed tab {i + 1}.\n\n" + _snapshot(bc)
        rows = []
        for k, p in enumerate(pages, 1):
            try:
                title = p.title()[:60]
            except Exception:
                title = "?"
            rows.append(f"{'*' if p is cur else ' '} {k}. {title}  {p.url[:80]}")
        return "Tabs (* is current):\n" + "\n".join(rows)
    return _run(job)


@tool(pack="browser")
def browser_screenshot(full_page: bool = False, _ctx=None):
    """Take a screenshot. With a vision model it is attached so you can see the page.
    full_page: capture the whole page instead of the viewport"""
    bc = _bc(_ctx)
    d = home() / "shots"
    d.mkdir(exist_ok=True)
    path = d / f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    _run(lambda: _page(bc).screenshot(path=str(path), full_page=full_page))
    return f"saved {path}. " + _ctx.queue_image(str(path))


@tool(pack="browser")
def browser_handoff(reason: str, _ctx=None):
    """Hand the browser to the user for something you can't or shouldn't do yourself: a captcha or bot check, logging in, two-factor codes, confirming a payment. Shows a window, waits until they're done, then returns the page.
    reason: what the user should do, in a few words"""
    bc = _bc(_ctx)  # keeps Tor if the browser is on Tor
    if not sys.stdin.isatty():
        return "error: nobody is at the keyboard to take over; tell the user what needs doing instead"
    if _S.get("cdp") is None or _S.get("headless"):
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return "error: there's no display to show a browser window on; ask the user to open the page themselves"
        url = None
        if _S.get("context"):
            with contextlib.suppress(Exception):
                url = _run(lambda: _S["page"].url, timeout=10)
            _run(_stop, timeout=15)
        bc["headless"] = False
        _ctx.cfg.setdefault("browser", {})["headless"] = False  # stay visible for the rest of the session

        def reopen():
            pg = _page(bc)
            if url and url.startswith("http"):
                pg.goto(url, wait_until="domcontentloaded", timeout=int(bc.get("nav_timeout", 45)) * 2000)
            pg.bring_to_front()
        _run(reopen, timeout=int(bc.get("nav_timeout", 45)) * 2 + 30)
    else:
        _run(lambda: _page(bc).bring_to_front())
    if not _ctx.ui.handoff(reason):
        raise Interrupted("the user stopped instead of finishing in the browser")
    return "The user says they're done in the browser.\n\n" + _run(lambda: _snapshot(bc))


@tool(pack="browser")
def browser_close(_ctx=None):
    """Close the browser."""
    if not _S.get("context"):
        return "the browser isn't open"
    _run(_stop, timeout=15)
    return "browser closed"


# ── for the /browser command ─────────────────────────────────────────────────

def describe():
    """Lines describing the browser for /browser."""
    if not _S.get("context"):
        return None

    def job():
        cur = _S.get("page")
        rows = []
        for k, p in enumerate(_pages(), 1):
            try:
                title = p.title()[:50]
            except Exception:
                title = "?"
            rows.append((p is cur, k, title, p.url))
        return _S.get("mode", "?"), rows
    return _run(job, timeout=10)


def close():
    if _S.get("context"):
        try:
            _run(_stop, timeout=15)
        except Exception:
            _S.clear()
