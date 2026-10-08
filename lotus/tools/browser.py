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
- Element numbers stay with their element while the page is open. After an action on the same
  page, only what changed is sent: new and vanished text, new, changed and removed elements, and
  what an in-page observer saw (dialogs, alerts, menus, toasts that came and went). It's the
  model's eyes on the page at a fraction of a screenshot's cost; browser_wait watches over time and
  browser_screenshot(target="changed") crops an image to just the part that changed.
- Safe browsing (see safety.py): known malware and phishing pages are blocked before they
  load, page text is marked as untrusted and checked for prompt injection, passwords and
  card numbers always need approval, and programs are never downloaded.
- Tor: with /tor on (or for any .onion address) Chromium is relaunched behind the Tor SOCKS
  proxy, with DNS resolved inside Tor, WebRTC kept off the network and QUIC disabled."""
import atexit
import concurrent.futures
import contextlib
import difflib
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
  // numbers stay with their element for the life of the document, so "[12]" means the same
  // button before and after a click, and a snapshot can be compared with the previous one
  const refs = window.__lotusRefs || (window.__lotusRefs = {n: 0, doc: Math.random().toString(36).slice(2)});
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[onclick],[contenteditable=""],[contenteditable=true],' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=option],[role=combobox],[role=textbox]';
  const vh = innerHeight, items = [], secret = [], hrefs = {}, used = new Set();
  let count = 0, more = 0, dupes = 0;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    // several links to the same place ("No. 123", "123", "Reply", "Click here") become one entry
    let key = null;
    if (e.tagName === 'A' && e.href && !e.href.startsWith('javascript:')) {
      key = e.href.split('#')[0];
      if (key in hrefs) {  // merge its words into the first entry, if they add anything
        const h = hrefs[key], extra = (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
        if (extra && !h.words.includes(extra.toLowerCase()) && h.words.length < 80) {
          h.words += ' / ' + extra.toLowerCase(); h.extra.push(extra);
        }
        dupes++; continue;
      }
    }
    // numbered even when not listed, so one that scrolls into range later isn't mistaken for a new one
    let n = +e.getAttribute('data-lotus') || 0;
    if (!n || used.has(n)) { n = ++refs.n; e.setAttribute('data-lotus', String(n)); }  // new, or cloned with its number
    used.add(n);
    if (r.bottom < -vh || r.top > vh * 3 || count >= opts.max) { more++; continue; }
    count++;
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
    if (tag === 'a') { try { const u = new URL(e.href); label += '  -> ' + (u.host === location.host ? '' : u.host) + u.pathname.slice(0, 50); } catch (x) {} }
    items.push([n, kind + ' ' + label, bits, r.top > vh || r.bottom < 0]);
    if (key) hrefs[key] = {i: items.length - 1, words: label.toLowerCase(), extra: [], label, kind};
  }
  for (const k in hrefs) {  // rewrite merged entries: "[1] a No. / Reply / Click here  -> /t/1"
    const h = hrefs[k];
    if (!h.extra.length) continue;
    const [text, arrow] = h.label.split('  -> ');
    items[h.i][1] = h.kind + ' ' + [text].concat(h.extra).filter(Boolean).join(' / ').slice(0, 90) + (arrow ? '  -> ' + arrow : '');
  }
  const alive = Array.from(document.querySelectorAll('[data-lotus]'), e => +e.getAttribute('data-lotus'));
  const body = document.body, main = document.querySelector('main, [role=main], article');
  const useMain = main && main.innerText.trim().length > 400;
  let text = ((useMain ? main : body) || {innerText: ''}).innerText.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const sh = Math.max(document.documentElement.scrollHeight, body ? body.scrollHeight : 0);
  return {title: document.title, url: location.href, doc: refs.doc, top: refs.n, items, alive, secret, dupes, text: text.slice(0, opts.keep),
          total: text.length, main: !!useMain, more, scroll: sh > vh ? Math.round(100 * scrollY / Math.max(1, sh - vh)) : 100,
          screens: Math.max(1, Math.round(sh / vh * 10) / 10)};
}
"""

# Watches the page between looks, so the model hears about what changed (a dialog opened,
# an error appeared, a toast came and went, a menu was shown) without a screenshot or a
# full re-read. Runs in every page from load on; costs nothing while the page is still.
OBSERVE_JS = r"""
(() => {
  if (window.__lotusObs) return;
  const POP = 'dialog,[role=dialog],[role=alertdialog],[aria-modal=true],[role=alert],[role=status],[aria-live=polite],' +
              '[aria-live=assertive],[role=menu],[role=listbox],[role=tooltip],[popover]';
  let log = [], since = performance.now(), last = 0, muts = 0, dropped = 0, rect = null, timer = null, on = false;
  let added = [], attrs = [], live = new Set();
  const vis = new WeakMap(), logged = new WeakMap();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = e => {
    if (!e.isConnected) return false;
    if (e.checkVisibility) return e.checkVisibility({opacityProperty: true, visibilityProperty: true});
    const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1;
  };
  const role = e => {
    const r = e.getAttribute('role'), live = e.getAttribute('aria-live');
    if (e.tagName === 'DIALOG' || r === 'dialog' || r === 'alertdialog' || e.getAttribute('aria-modal') === 'true') return 'dialog';
    if (r === 'alert' || live === 'assertive') return 'alert';
    if (r === 'status' || live === 'polite') return 'status';
    if (r === 'menu' || r === 'listbox' || r === 'tooltip') return r;
    if (e.hasAttribute('popover')) return 'popup';
    return '';
  };
  const kindOf = e => {
    const own = role(e); if (own) return own;
    const up = e.parentElement && e.parentElement.closest(POP); if (up) return 'in ' + role(up);
    const st = getComputedStyle(e);
    return (st.position === 'fixed' || st.position === 'sticky') && +st.zIndex > 0 ? 'popup' : '';
  };
  const grow = e => {
    const r = e.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return;
    const b = {x: r.left + scrollX, y: r.top + scrollY, r: r.right + scrollX, b: r.bottom + scrollY};
    rect = rect ? {x: Math.min(rect.x, b.x), y: Math.min(rect.y, b.y), r: Math.max(rect.r, b.r), b: Math.max(rect.b, b.b)} : b;
  };
  const note = (what, e, kind, text) => {
    if (log.length >= 60) { dropped++; return; }
    const ev = {t: Math.round(performance.now() - since), what, kind, text: text.slice(0, 160), el: e ? new WeakRef(e) : null};
    log.push(ev); if (e) logged.set(e, ev);
  };
  const flush = () => {
    timer = null;
    const roots = added.filter(e => e.isConnected && !added.some(o => o !== e && o.isConnected && o.contains(e)));
    added = [];
    for (const e of roots.slice(0, 25)) {
      const v = shown(e); vis.set(e, v);
      if (!v || !clean(e.textContent)) continue;
      const text = clean(e.innerText); if (!text) continue;
      note('appeared', e, kindOf(e), text); grow(e);
    }
    for (const [e, name, old] of attrs.splice(0, 40)) {
      if (!e.isConnected) continue;
      const v = shown(e);
      let was = vis.get(e);
      if (was === undefined) {
        if (name === 'hidden' || name === 'aria-hidden') was = name === 'hidden' ? old === null : old !== 'true';
        else if (name === 'open') was = old !== null;
        else if (e.matches(POP)) was = false;  // a dialog or menu shown by class/style
        else { vis.set(e, v); continue; }
      }
      vis.set(e, v);
      if (v === was) continue;
      const seen = logged.get(e);  // what was hidden is only news if we saw it shown
      const text = v ? clean(e.innerText) : seen ? seen.text : '';
      if (!text) continue;
      note(v ? 'shown' : 'hidden', e, kindOf(e), text); if (v) grow(e);
    }
    for (const e of live) {  // text updates inside live regions: "3 results", "Saved", form errors
      if (!e.isConnected) continue;
      const text = clean(e.innerText);
      const prev = logged.get(e);
      if (text && (!prev || prev.text !== text.slice(0, 160))) { note('updated', e, role(e) || 'status', text); grow(e); }
    }
    live.clear();
  };
  const obs = new MutationObserver(list => {
    if (!on) return;
    muts += list.length; last = performance.now();
    for (const m of list) {
      if (m.type === 'childList') {
        for (const n of m.addedNodes) {
          if (n.nodeType === 1) added.length < 200 && added.push(n);
          else if (n.nodeType === 3 && m.target.closest) { const l = m.target.closest(POP); if (l) live.add(l); }
        }
        for (const n of m.removedNodes) {  // only things worth knowing: popups, and what we saw appear
          if (n.nodeType !== 1) continue;
          const ev = logged.get(n), kind = ev ? ev.kind : role(n);
          if (ev && !ev.sent) ev.gone = true;  // came and went before anyone looked
          else if (kind && !kind.startsWith('in ') && (ev || vis.get(n) !== false)) {
            const text = ev ? ev.text : clean(n.textContent); if (text) note('closed', null, kind, text);
          }
        }
        if (m.target.nodeType === 1 && m.target.closest) { const l = m.target.closest(POP); if (l) live.add(l); }
      } else if (m.type === 'characterData') {
        const p = m.target.parentElement, l = p && p.closest(POP); if (l) live.add(l);
      } else attrs.length < 200 && attrs.push([m.target, m.attributeName, m.oldValue]);
    }
    if (!timer) timer = setTimeout(flush, 150);
  });
  const start = () => {
    if (on) return; on = true;
    obs.observe(document, {childList: true, subtree: true, characterData: true, attributes: true, attributeOldValue: true,
                           attributeFilter: ['hidden', 'open', 'aria-hidden', 'class', 'style']});
  };
  // what loads with the page isn't news (the first look reads it all), so start once it has loaded
  if (document.readyState === 'complete') start(); else addEventListener('load', start, {once: true});
  setTimeout(start, 4000);
  window.__lotusObs = {
    take() {
      if (timer) { clearTimeout(timer); flush(); }
      log.forEach(ev => { ev.sent = true; });
      const out = log.map(ev => {
        const e = ev.el && ev.el.deref();
        const gone = ev.gone || (ev.what !== 'hidden' && ev.what !== 'closed' && e && !shown(e));
        return {t: ev.t, what: ev.what, kind: ev.kind, text: ev.text, gone: !!gone};
      });
      const res = {events: out, dropped, muts, rect, quiet: Math.round(performance.now() - Math.max(last, since))};
      log = []; dropped = 0; muts = 0; rect = null; since = performance.now();
      return res;
    },
    quiet() { return Math.round(performance.now() - last); },
    box() { if (timer) { clearTimeout(timer); flush(); } return rect; },
  };
})()
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
    for cx in _S.get("private", []):
        with contextlib.suppress(Exception):
            cx.close()
    try:
        if _S.get("persistent") and _S.get("context"):
            _S["context"].close()  # writes the profile to disk
        elif _S.get("browser") and not _S.get("cdp"):
            _S["browser"].close()
        if _S.get("pbrowser"):
            _S["pbrowser"].close()
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


def _engine(pw, bc):
    name = (bc.get("engine") or "chromium").lower()
    if name not in ("chromium", "firefox"):
        raise RuntimeError(f"browser.engine must be chromium or firefox, not {name!r}")
    return name, getattr(pw, name)


def _launch_opts(bc, tor, engine):
    """Options for launching a browser (and for a persistent profile, which takes both kinds)."""
    headless = bc.get("headless")
    if headless is None:
        headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    kw = {"headless": headless}
    if engine == "chromium":
        kw["args"] = []
        if bc.get("channel"):
            kw["channel"] = bc["channel"]  # "chrome" or "msedge" to use an installed browser
        if bc.get("executable"):
            kw["executable_path"] = os.path.expanduser(bc["executable"])  # e.g. Brave
    if tor:
        kw["proxy"] = {"server": tor}
        proxy_host = tor.split("://")[-1].rsplit(":", 1)[0]
        if engine == "chromium":
            kw["args"] += [
                # no DNS outside the proxy; only the proxy itself is reached directly
                f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {proxy_host} , EXCLUDE localhost",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",  # WebRTC can't reveal your IP
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-quic", "--dns-prefetch-disable", "--no-pings",
            ]
        else:
            kw["firefox_user_prefs"] = {"network.proxy.socks_remote_dns": True, "media.peerconnection.enabled": False,
                                        "network.dns.disablePrefetch": True, "network.http.http3.enable": False,
                                        "browser.send_pings": False}
    return kw, headless


def _context_opts(tor):
    o = {"viewport": {"width": 1280, "height": 900}, "accept_downloads": True}
    if tor:
        o.update(locale="en-US", timezone_id="UTC")  # like Tor Browser: don't give away where you are
    return o


def _setup(context, bc, private=False):
    """Safety guard, dialogs, downloads and popups for one window (browser context)."""
    cfg = bc.get("_cfg")
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
    with contextlib.suppress(Exception):
        context.add_init_script(OBSERVE_JS)
    for p in context.pages:
        _wire(p, bc)
    if private:
        _S.setdefault("private", []).append(context)


def profile_dir(bc):
    return home() / "browser-profile" / (bc.get("engine") or "chromium").lower()


def _launch(bc):
    tor, cfg = bc.get("_tor"), bc.get("_cfg")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("browser tools need Playwright: pip install playwright && python -m playwright install chromium")
    pw = sync_playwright().start()
    _S["pw"] = pw
    engine, kind = _engine(pw, bc)
    persistent = (bc.get("profile") or "private") == "persistent" and not tor  # Tor never keeps cookies
    try:
        if bc.get("cdp_url"):
            if tor:
                raise RuntimeError("Tor can't be applied to a browser you attached (browser.cdp_url); start it with "
                                   f"--proxy-server={tor}, or detach with /browser detach")
            b = pw.chromium.connect_over_cdp(bc["cdp_url"])
            context = b.contexts[0] if b.contexts else b.new_context()
            _S.update(cdp=True, mode=f"your browser at {bc['cdp_url']}")
        else:
            kw, headless = _launch_opts(bc, tor, engine)
            if persistent:
                d = profile_dir(bc)
                d.mkdir(parents=True, exist_ok=True)
                context = kind.launch_persistent_context(str(d), **kw, **_context_opts(tor))
                b = context.browser
            else:
                b = kind.launch(**kw)
                context = b.new_context(**_context_opts(tor))
            what = ("headless" if headless else "window") + (f" · {engine}" if engine != "chromium" else "")
            what += " · logins kept" if persistent else " · private"
            _S.update(cdp=False, mode=what + (" · via Tor" if tor else ""))
    except Exception as e:
        with contextlib.suppress(Exception):
            pw.stop()
        _S.clear()
        if "Executable doesn't exist" in str(e):
            raise RuntimeError(f"Playwright's {engine} isn't installed: python -m playwright install {engine}") from None
        if "ProcessSingleton" in str(e) or "SingletonLock" in str(e):
            raise RuntimeError(f"the lotus profile at {profile_dir(bc)} is in use by another browser; close it first") from None
        raise
    _S.update(browser=b, context=context, headless=_S["mode"].startswith("headless"), bc=bc, tor=tor, cfg=cfg,
              persistent=persistent, engine=engine)
    _setup(context, bc)
    pages = context.pages
    _S["page"] = pages[-1] if pages else _new_page()


def _new_page(context=None):
    _S["opening"] = True
    try:
        pg = (context or _S["context"]).new_page()
    finally:
        _S["opening"] = False
    _S["page"] = pg
    return pg


def _private_window(bc):
    """A fresh, isolated window: its own cookies and storage, gone when it closes."""
    b = _S.get("browser")
    if b is None or _S.get("persistent"):  # a persistent profile can't host other windows; use a second browser
        if not _S.get("pbrowser"):
            engine, kind = _engine(_S["pw"], bc)
            _S["pbrowser"] = kind.launch(**_launch_opts(bc, _S.get("tor"), engine)[0])
        b = _S["pbrowser"]
    context = b.new_context(**_context_opts(_S.get("tor")))
    _setup(context, bc, private=True)
    return _new_page(context)


def _pages():
    if not _S.get("context"):
        return []
    out = [p for p in _S["context"].pages if not p.is_closed()]
    for cx in _S.get("private", []):
        with contextlib.suppress(Exception):
            out += [p for p in cx.pages if not p.is_closed()]
    return out


def _is_private(pg):
    return any(pg.context is cx for cx in _S.get("private", []))


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


def _take(pg):
    """What the page's observer saw since the last look. Also installs it in pages that were
    open before lotus wired the window (an attached browser) or that load without it."""
    try:
        return pg.evaluate("() => { " + OBSERVE_JS + "; return window.__lotusObs.take(); }")
    except Exception:
        return None


def _lines(text):
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _el(n, label, bits, off=False):
    bits = list(bits) + (["offscreen"] if off else [])
    return f"[{n}] {label}" + (f"  ({', '.join(bits)})" if bits else "")


def _clip(s, n=160):
    return s if len(s) <= n else s[:n - 1] + "…"


def _event_lines(events, timed):
    """The observer's events in words. Untimed (after an action), only popups and things that
    came and went: the text diff already covers text that is simply new or gone."""
    out, seen = [], set()
    for ev in events:
        kind, what, gone = ev["kind"], ev["what"], ev["gone"]
        if not timed and not kind and not gone:
            continue
        key = (what, kind, ev["text"])
        if key in seen:
            continue
        seen.add(key)
        if gone and what in ("appeared", "shown", "updated"):
            what = ("showed" if what == "updated" else what) + " and went away again"
        if kind.startswith("in "):
            line = f"{what} {kind}"
        else:
            line = f"{kind} {what}" if kind else what
        out.append((f"+{ev['t'] / 1000:.1f}s " if timed else "") + f'{line}: "{_clip(ev["text"])}"')
    return out


def _text_diff(old, new):
    sm = difflib.SequenceMatcher(None, old, new, autojunk=len(old) > 2000)
    add, rem = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            rem += old[i1:i2]
        if op in ("replace", "insert"):
            add += new[j1:j2]
    moved = set(add) & set(rem)  # reordered, not changed
    return [x for x in add if x not in moved], [x for x in rem if x not in moved]


def _capped(prefix, lines, budget):
    out, used = [], 0
    for i, ln in enumerate(lines):
        ln = _clip(ln, 200)
        if used + len(ln) > budget and out:
            out.append(f"  …and {len(lines) - i} more line(s)")
            break
        out.append(f"  {prefix} {ln}")
        used += len(ln)
    return out


def _changes(base, view, d, events, timed):
    """What changed between the last look and now, in a few lines, or None if it's too much
    to be worth listing (then the whole page is sent instead)."""
    out = _event_lines(events, timed)
    shown = " ".join(e["text"].lower() for e in events)
    add, rem = _text_diff(base["lines"], view["lines"])
    add = [x for x in add if x.lower()[:50] not in shown]
    rem = [x for x in rem if x.lower()[:50] not in shown]
    budget = int(base.get("chars", 4000)) // 3
    if add:
        out.append("new text:")
        out += _capped("+", add, budget)
    if rem:
        out.append("text gone:")
        out += _capped("-", rem, budget // 2)
    old, now, alive = base["items"], view["items"], set(d["alive"])
    new = [n for n in now if n not in old and n > base["top"]]
    into = [n for n in now if n not in old and n <= base["top"]]
    changed = [n for n in now if n in old and old[n][:2] != now[n][:2]]
    removed = [n for n in old if n not in now and n not in alive]
    away = [n for n in old if n not in now and n in alive]
    for word, ids, cap in (("new", new, 15), ("now listed", into, 15)):
        if ids:
            out.append(f"{word}: " + ("" if len(ids) == 1 else "\n  ") +
                       "\n  ".join(_el(n, *now[n]) for n in ids[:cap]) + (f"\n  …and {len(ids) - cap} more" if len(ids) > cap else ""))
    for n in changed[:10]:
        was = old[n][1] if old[n][0] == now[n][0] else None
        out.append(f"now: {_el(n, *now[n])}  (was " + (", ".join(was) or "plain" if was is not None else old[n][0]) + ")")
    if removed:
        out.append("gone: " + ", ".join(f"[{n}] {_clip(old[n][0], 40)}" for n in removed[:8]) +
                   (f" …and {len(removed) - 8} more" if len(removed) > 8 else ""))
    if away:
        out.append(f"{len(away)} element(s) no longer listed (hidden, or scrolled out of range); their numbers still work if they come back")
    return out


def _snapshot(bc, full=True, timed=False):
    """The page as the model sees it. full=False after an action: on the same page, only what
    changed since the model's last look is sent (with element numbers kept), which is far
    smaller than the page and says plainly when an action did nothing."""
    pg = _page(bc)
    _save_downloads()
    chars = int(bc.get("max_text", 4000))
    opts = {"max": int(bc.get("max_elements", 120)), "keep": max(chars, 60000)}
    obs = _take(pg)
    d = pg.evaluate(SNAP_JS, opts)
    if _S.get("page") is not pg and _S.get("page") is not None:  # a popup took over while we were reading
        pg = _S["page"]
        _settle(pg, quick=True)
        obs = _take(pg)
        d = pg.evaluate(SNAP_JS, opts)
        full = True
    _S["labels"] = {str(n): label for n, label, *_ in d["items"]}
    _S["secret"] = {str(n) for n in d.get("secret", [])}
    _S["last"] = (d["url"],)
    if obs and obs.get("rect"):
        _S["rect"] = (d["doc"], obs["rect"])
    act = _S.get("act")  # inside browser_act: compare with the page before the first step, and keep every event
    base = act["base"] if act is not None else _S.get("view")
    view = {"page": pg, "doc": d["doc"], "url": d["url"].split("#")[0], "lines": _lines(d["text"]), "chars": chars,
            "items": {n: (label, tuple(bits), off) for n, label, bits, off in d["items"]}, "top": d["top"]}
    _S["view"] = view
    events = _S.pop("events", [])
    seen = (obs or {}).get("events", [])
    if (obs or {}).get("dropped"):
        seen.append({"t": 0, "what": "appeared", "kind": "", "text": f"(and {obs['dropped']} more changes)", "gone": False})
    if act is not None:
        act["notes"] += events
        act["seen"] += seen
        events, seen = list(act["notes"]), list(act["seen"])

    pages = _pages()
    head = f"# {d['title'] or '(untitled)'}\n{d['url']}" + ("  (via Tor)" if _S.get("tor") else "")
    if len(pages) > 1 and pg in pages:
        head += f"   [tab {pages.index(pg) + 1} of {len(pages)}]"
    if _is_private(pg):
        head += "   (private window)"
    out = [head]
    u = urlparse(d["url"])
    if u.scheme == "http" and not (u.hostname or "").endswith(".onion") and u.hostname not in ("localhost", "127.0.0.1"):
        events.append("this page isn't encrypted (http); don't enter anything private here")
    if safety.is_challenge(d["title"], d["text"][:chars]):
        events.append("this is a bot check / captcha page, which sites often show to Tor and automated browsers. "
                      "Don't try to solve it. Try another page on the same site (e.g. a subdomain or a deeper link), "
                      "or call browser_handoff so the user can solve it in a window")
    if events:
        out.append("\n".join("Note: " + e for e in events))
    pos = f"scrolled {d['scroll']}% of ~{d['screens']} screens" if d["screens"] > 1.1 else "the whole page fits on screen"

    same = (base is not None and base["page"] is pg and base["doc"] == d["doc"] and base["url"] == view["url"])
    if not full and same and _S.get("diffs", 0) < 8:
        lines = _changes(base, view, d, seen, timed)
        if lines:
            fresh = "\n".join(lines)
            warn = safety.guard_text(fresh)
            fresh = safety.UNTRUSTED + ("\n" + warn if warn else "") + "\nWhat changed since your last look:\n" + fresh
        else:
            fresh = ("Nothing visible changed since your last look: no new text, popups or element changes. The action "
                     "may have done nothing, or the page is still working (browser_wait watches it).")
        diff = "\n\n".join(out + [fresh, f"Other elements are as listed before ({len(d['items'])} in range; {pos}). "
                                          "browser_snapshot lists them all again."])
        whole = _whole(out, d, chars, pos, _event_lines(seen, timed))
        if not lines or len(diff) < 0.7 * len(whole):
            _S["diffs"] = _S.get("diffs", 0) + 1
            return diff
        _S["diffs"] = 0
        return whole
    _S["diffs"] = 0
    return _whole(out, d, chars, pos, _event_lines(seen, timed))


def _whole(out, d, chars, pos, happened):
    text = d["text"][:chars]
    body = text or "(no text on the page)"
    if d["total"] > len(text):
        body += f"\n[showing the first {len(text)} of {d['total']} chars; browser_find('words') searches the rest]"
    warn = safety.guard_text(text + "\n".join(happened))
    if happened:
        body = "Seen on the page since your last look:\n" + "\n".join(happened) + "\n\n" + body
    out = out + [safety.UNTRUSTED + ("\n" + warn if warn else "") + "\n" + ("(main content)\n" if d["main"] else "") + body]
    els = "\n".join(_el(n, label, bits, off) for n, label, bits, off in d["items"]) or "(none)"
    more = f"\n…and {d['more']} more further down (scroll to see them)" if d["more"] else ""
    out.append(f"Interactive elements ({pos}; use the number with browser_click / browser_type):\n{els}{more}")
    return "\n\n".join(out)


def _target(pg, target):
    t = str(target).strip().strip("[]")
    if t.isdigit():
        loc = pg.locator(f'[data-lotus="{t}"]')
        if loc.count() == 0:  # fail now instead of waiting out a timeout
            raise RuntimeError(f"there is no element [{t}] on this page now; call browser_snapshot for current numbers")
        return loc.first
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
def browser_open(url: str, new_tab: bool = False, private: bool = False, via_tor: bool = False, _ctx=None):
    """Open a URL in the browser and return the page text and numbered elements. To search a site, open its search URL directly (e.g. https://www.amazon.com/s?k=water+bottle, https://duckduckgo.com/?q=...) instead of typing into its search box. For Tor set via_tor=true (.onion addresses always use Tor); never use third-party "Tor gateway" or proxy websites.
    url: address to open
    new_tab: open in a new tab instead of the current one
    private: open in a new private window (its own cookies, nothing kept)
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
        if private:
            _page(bc)  # make sure the browser is running (with Tor if asked)
            pg = _private_window(bc)
        else:
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
    return _type(target, text, _ctx, submit, batch=False)


def _type(target, text, _ctx, submit=False, batch=True):
    """batch: called from browser_act, whose approval didn't single out sensitive fields."""
    bc = _bc(_ctx)
    t = str(target).strip().strip("[]")
    if t in (_S.get("secret") or set()) and (batch or _ctx.permission == "auto" or "browser_type" in _ctx.always):
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
    """Watch the page until text appears, or until it stops changing, then report what happened meanwhile with timings: new text, dialogs, alerts, messages that came and went. Use it while a page loads, a reply streams in, or after an action that seemed to do nothing. Much cheaper than screenshots.
    text: text to wait for; empty waits until the page settles
    seconds: longest wait"""
    bc = _bc(_ctx)
    seconds = max(1, min(int(seconds), 30))

    def job():
        pg = _page(bc)
        with contextlib.suppress(Exception):
            pg.evaluate(OBSERVE_JS)
        t0 = time.monotonic()
        found = False
        while time.monotonic() - t0 < seconds:
            try:
                if text:
                    found = pg.evaluate("q => (document.body ? document.body.innerText : '').toLowerCase().includes(q)",
                                        text.lower())
                    if found:
                        break
                elif time.monotonic() - t0 > 0.8 and pg.evaluate(
                        "() => window.__lotusObs ? window.__lotusObs.quiet() : 1e9") > 800:
                    break  # nothing has changed for a moment: settled
            except Exception:  # navigating; the next look reads the new page
                pass
            pg.wait_for_timeout(250)
        took = time.monotonic() - t0
        if text and not found:
            note = f"'{text}' did not appear within {seconds}s.\n\n"
        elif text:
            note = f"'{text}' appeared after {took:.1f}s.\n\n"
        else:
            note = (f"The page settled after {took:.1f}s.\n\n" if took < seconds
                    else f"The page was still changing after {seconds}s.\n\n")
        return note + _snapshot(bc, full=False, timed=True)
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
    """List, switch to, open or close tabs, or open a private window.
    action: list, switch, new, private or close
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
        if action == "private":
            _private_window(bc)
            return "opened a new private window (its own cookies, nothing kept); use browser_open to load a page"
        if action == "switch":
            _S["page"] = pages[i]
            pages[i].bring_to_front()
            return _snapshot(bc)
        if action == "close":
            cx = pages[i].context if _is_private(pages[i]) else None
            pages[i].close()
            if cx is not None and not cx.pages:  # last tab of a private window: forget the window entirely
                cx.close()
                _S["private"] = [c for c in _S.get("private", []) if c is not cx]
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
            rows.append(f"{'*' if p is cur else ' '} {k}. {title}  {p.url[:80]}" + ("  (private)" if _is_private(p) else ""))
        return "Tabs (* is current):\n" + "\n".join(rows)
    return _run(job)


@tool(pack="browser")
def browser_screenshot(full_page: bool = False, target: str = "", _ctx=None):
    """Take a screenshot. With a vision model it is attached so you can see the page. Prefer the text snapshot; when you do need to look, a target keeps the image small.
    full_page: capture the whole page instead of the viewport
    target: an element number to capture just that element, or "changed" for only the part of the page that changed last"""
    bc = _bc(_ctx)
    d = home() / "shots"
    d.mkdir(exist_ok=True)
    path = d / f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    t = str(target or "").strip().strip("[]").lower()

    def job():
        pg = _page(bc)
        if t == "changed":
            r = None
            with contextlib.suppress(Exception):
                r = pg.evaluate("() => window.__lotusObs ? window.__lotusObs.box() : null")
            if not r:
                doc = None
                with contextlib.suppress(Exception):
                    doc = pg.evaluate("() => window.__lotusRefs && window.__lotusRefs.doc")
                r = _S["rect"][1] if _S.get("rect") and _S["rect"][0] == doc else None
            if not r:
                pg.screenshot(path=str(path))
                return "nothing has been seen changing on this page, so this is the whole screen"
            pad = 12
            x, y = max(0, r["x"] - pad), max(0, r["y"] - pad)
            clip = {"x": x, "y": y, "width": min(r["r"] + pad - x, 2000), "height": min(r["b"] + pad - y, 2000)}
            pg.screenshot(path=str(path), full_page=True, clip=clip)
            return f"the area that changed ({int(clip['width'])}x{int(clip['height'])})"
        if t:
            loc = _target(pg, t)
            loc.screenshot(path=str(path), timeout=int(bc.get("timeout", 10)) * 1000)
            return f"element [{t}] {_label(t)}".rstrip()
        pg.screenshot(path=str(path), full_page=full_page)
        return "the whole page" if full_page else "the screen"
    what = _run(job)
    return f"saved {path} ({what}). " + _ctx.queue_image(str(path))


ACT_HELP = """one action per line:
  open <url>            click <n>              type <n> <text>
  select <n> <option>   press <key>            scroll down|up|top|bottom
  wait <seconds|text>   back"""


@tool(pack="browser")
def browser_act(steps: list, _ctx=None):
    """Do several browser actions in one call, e.g. ["type 3 water bottle", "press Enter", "click 12"]. Stops at the first step that fails and returns the page after the last step. Saves round trips: use it whenever you already know the next few actions.
    steps: actions in order: open <url>, click <n>, type <n> <text>, select <n> <option>, press <key>, scroll <down|up|top|bottom>, wait <seconds or text>, back"""
    lines = []
    for item in (steps if isinstance(steps, list) else [steps]):
        lines += [l.strip() for l in re.split(r"[\n;]+", str(item)) if l.strip()]
    if not lines:
        return "error: no steps. " + ACT_HELP
    if len(lines) > 12:
        return "error: at most 12 steps per call"
    if any(l.split()[0].lower() == "type" for l in lines):  # same approval as browser_type
        typed = "\n".join(l for l in lines if l.split()[0].lower() == "type")
        if not _ctx.approve("browser_type", typed, key="browser_type"):
            return "error: the user declined the typing in these steps. Ask them how to proceed."
    done = []
    _run(lambda: _S.__setitem__("act", {"base": _S.get("view"), "notes": [], "seen": []}))
    try:
        last = _steps(lines, done, _ctx)
    finally:
        _S.pop("act", None)
    left = len(lines) - len(done)
    summary = "\n".join(done) + (f"\n({left} later step(s) not run)" if left else "")
    if last.startswith("error"):
        with contextlib.suppress(Exception):
            last = browser_snapshot(_ctx=_ctx)
    return f"Steps:\n{summary}\n\n{last}"


def _steps(lines, done, _ctx):
    import shlex
    last = ""
    for line in lines:
        verb, _, rest = line.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        try:
            if verb in ("open", "goto", "go"):
                last = browser_open(rest, _ctx=_ctx)
            elif verb == "click":
                last = browser_click(rest, _ctx=_ctx)
            elif verb in ("type", "fill", "select"):
                parts = shlex.split(rest) if rest.startswith(('"', "'")) else rest.split(None, 1)
                if len(parts) < 2:
                    raise ValueError(f"{verb} needs a target and text")
                target, text = parts[0], " ".join(parts[1:]) if rest.startswith(('"', "'")) else parts[1]
                last = browser_select(target, text, _ctx=_ctx) if verb == "select" else _type(target, text, _ctx)
            elif verb == "press":
                last = browser_press(rest, _ctx=_ctx)
            elif verb == "scroll":
                last = browser_scroll(rest or "down", _ctx=_ctx)
            elif verb == "wait":
                last = browser_wait(seconds=int(rest), _ctx=_ctx) if rest.isdigit() else browser_wait(text=rest, _ctx=_ctx)
            elif verb == "back":
                last = browser_back(_ctx=_ctx)
            else:
                raise ValueError(f"unknown action '{verb}'. " + ACT_HELP)
        except Interrupted:
            raise
        except Exception as e:
            last = f"error: {e}"
        if last.startswith("error"):
            done.append(f"x {line}: {last[7:200]}")
            break
        done.append(f"ok {line}")
    return last


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
            rows.append((p is cur, k, ("(private) " if _is_private(p) else "") + title, p.url))
        return _S.get("mode", "?"), rows
    return _run(job, timeout=10)


def close():
    if _S.get("context"):
        try:
            _run(_stop, timeout=15)
        except Exception:
            _S.clear()
