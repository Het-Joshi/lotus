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
- After an action, the page text is only resent if it changed, which keeps small contexts small."""
import atexit
import concurrent.futures
import fnmatch
import os
import queue
import re
import sys
import threading
import time
from urllib.parse import urlparse

from . import Interrupted, pack, tool
from ..config import home

pack("browser", "drive a real web browser: open, read, click, type, tabs, screenshot")

_S = {}  # browser state; only touched on the browser thread (reads from elsewhere are harmless)

RISKY = re.compile(r"\b(buy|purchase|pay|checkout|check out|place order|order now|add to cart|subscribe|unsubscribe|"
                   r"delete|remove|erase|send|post|publish|tweet|reply|submit|confirm|transfer|donate|book now|"
                   r"reserve|sign out|log ?out|deactivate|close account|cancel (?:subscription|account|order|plan))\b", re.I)

SNAP_JS = r"""
(opts) => {
  document.querySelectorAll('[data-lotus]').forEach(e => e.removeAttribute('data-lotus'));
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[onclick],[contenteditable=""],[contenteditable=true],' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=option],[role=combobox],[role=textbox]';
  const vh = innerHeight, out = [], labels = {};
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
    if (r.top > vh || r.bottom < 0) bits.push('offscreen');
    if (tag === 'a') { try { const u = new URL(e.href); label += '  -> ' + (u.host === location.host ? '' : u.host) + u.pathname.slice(0, 50); } catch (x) {} }
    labels[n] = kind + ' ' + label;
    out.push('[' + n + '] ' + kind + ' ' + label + (bits.length ? '  (' + bits.join(', ') + ')' : ''));
  }
  const body = document.body, main = document.querySelector('main, [role=main], article');
  const useMain = main && main.innerText.trim().length > 400;
  let text = ((useMain ? main : body) || {innerText: ''}).innerText.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const sh = Math.max(document.documentElement.scrollHeight, body ? body.scrollHeight : 0);
  return {title: document.title, url: location.href, elements: out, labels, text: text.slice(0, opts.chars), total: text.length,
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

def _bc(ctx):
    return (ctx.cfg.get("browser") or {}) if ctx is not None else {}


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


def _launch(bc):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("browser tools need Playwright: pip install playwright && python -m playwright install chromium")
    pw = sync_playwright().start()
    _S["pw"] = pw
    if bc.get("cdp_url"):
        b = pw.chromium.connect_over_cdp(bc["cdp_url"])
        context = b.contexts[0] if b.contexts else b.new_context()
        _S.update(cdp=True, mode=f"your Chrome at {bc['cdp_url']}")
    else:
        headless = bc.get("headless")
        if headless is None:
            headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        kw = {"headless": headless}
        if bc.get("channel"):
            kw["channel"] = bc["channel"]  # "chrome" or "msedge" to use an installed browser
        b = pw.chromium.launch(**kw)
        context = b.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        _S.update(cdp=False, mode="headless" if headless else "window")
    _S.update(browser=b, context=context, headless=_S["mode"] == "headless", bc=bc)
    if bc.get("allow") or bc.get("block"):
        def guard(route):
            req = route.request
            if req.is_navigation_request() and not _host_ok(req.url, bc):
                _event(f"blocked navigation to {urlparse(req.url).hostname} (not allowed by browser.allow / browser.block)")
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
    key = (d["url"], hash(d["text"]))
    same = _S.get("last") == key
    _S["last"] = key
    pages = _pages()
    head = f"# {d['title'] or '(untitled)'}\n{d['url']}"
    if len(pages) > 1 and pg in pages:
        head += f"   [tab {pages.index(pg) + 1} of {len(pages)}]"
    out = [head]
    events = _S.pop("events", [])
    if events:
        out.append("\n".join("Note: " + e for e in events))
    if same and not full:
        out.append("(page text unchanged since the last snapshot)")
    else:
        body = d["text"] or "(no text on the page)"
        if d["total"] > len(d["text"]):
            body += f"\n[showing the first {len(d['text'])} of {d['total']} chars; browser_find('words') searches the rest]"
        out.append(("(main content)\n" if d["main"] else "") + body)
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
def browser_open(url: str, new_tab: bool = False, _ctx=None):
    """Open a URL and return the page text and numbered elements.
    url: address to open
    new_tab: open in a new tab instead of the current one"""
    bc = _bc(_ctx)
    if "://" not in url and not url.startswith("about:"):
        url = "https://" + url
    if not _host_ok(url, bc):
        return f"error: {urlparse(url).hostname} is not allowed by the browser.allow / browser.block settings"

    def job():
        pg = _new_page() if new_tab and _S.get("context") else _page(bc)
        pg.goto(url, wait_until="domcontentloaded", timeout=int(bc.get("nav_timeout", 45)) * 1000)
        _settle(pg)
        return _snapshot(bc)
    return _run(job, timeout=int(bc.get("nav_timeout", 45)) + 30)


@tool(pack="browser")
def browser_snapshot(_ctx=None):
    """Re-read the current page: text plus numbered interactive elements."""
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
