"""Security plugin: vulnerability intel and defensive checks for systems you own. No API keys.

- cve_lookup       CVSS, CWE, exploited-in-the-wild (CISA KEV) and EPSS for a CVE, or a keyword search
- osv_check        known vulnerabilities in one package version (OSV.dev, every ecosystem)
- audit_deps       scan a project's lockfiles/requirements against OSV
- secret_scan      leaked keys and tokens in a folder (output is masked)
- tls_check        certificate, expiry, protocol and cipher of a host
- headers_audit    HTTP security headers and cookie flags of a URL
- dns_lookup       DNS records over HTTPS; type=EMAIL checks SPF, DMARC, MX, CAA
- listening_ports  what on this machine accepts connections, and on which interfaces
- port_check       TCP connect check of hosts you own or may test (asks first)
- file_hash        md5/sha1/sha256 of a file

Commands: /cve <id>   /pwned  (checks a password against Have I Been Pwned with
k-anonymity; it is read with getpass and never reaches the model)

Set NVD_API_KEY for higher NVD rate limits. Copy or symlink to ~/.lotus/plugins/."""
import concurrent.futures
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from lotus.config import home
from lotus.tools import command, pack, tool

pack("security", "CVE/OSV vulnerability lookups, dependency audit, secret scan, TLS/headers/DNS checks, open ports")

UA = "lotus-agent/0.1 (security plugin)"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".tox", "dist", "build", ".next", "target"}


def _fetch(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": UA, **(headers or {})}
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(20_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def _json(url, **kw):
    return json.loads(_fetch(url, **kw))


def _short(text, n):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _path(ctx, p):
    p = Path(os.path.expanduser(str(p)))
    return p if p.is_absolute() else Path(ctx.cwd) / p


# ── CVE intel ────────────────────────────────────────────────────────────────

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.I)


def _kev():
    """CISA Known Exploited Vulnerabilities catalog, cached for a day."""
    cache = home() / "cache" / "kev.json"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 86400:
        try:
            raw = _fetch("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json", timeout=40)
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(raw, encoding="utf-8")
        except (OSError, urllib.error.URLError):
            if not cache.exists():
                return {}
    data = json.loads(cache.read_text(encoding="utf-8"))
    return {v["cveID"]: v for v in data.get("vulnerabilities", [])}


def _epss(ids):
    try:
        d = _json("https://api.first.org/data/v1/epss?cve=" + ",".join(ids))
        return {r["cve"]: (float(r["epss"]), float(r["percentile"])) for r in d.get("data", [])}
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def _nvd(params):
    h = {"apiKey": os.environ["NVD_API_KEY"]} if os.environ.get("NVD_API_KEY") else {}
    return _json("https://services.nvd.nist.gov/rest/json/cves/2.0?" + urllib.parse.urlencode(params), headers=h, timeout=40)


def _cvss(metrics):
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        for m in metrics.get(key) or []:
            d = m.get("cvssData", {})
            sev = d.get("baseSeverity") or m.get("baseSeverity", "")
            return f"{d.get('baseScore', '?')} {sev} ({key[10:].replace('V', 'v')})", d.get("vectorString", "")
    return "not scored yet", ""


@tool(pack="security")
def cve_lookup(query: str, n: int = 8):
    """Look up a CVE (CVSS score, weakness, whether it's exploited in the wild per CISA KEV, EPSS exploit probability, fix references), or search recent CVEs by keyword.
    query: a CVE id like CVE-2024-3094, or keywords like 'openssh regreSSHion'
    n: results for keyword searches (max 20)"""
    ids = [x.upper() for x in CVE_RE.findall(query)]
    if ids:
        vulns = []
        for cid in ids[:5]:
            vulns += _nvd({"cveId": cid}).get("vulnerabilities", [])
        detail = True
    else:
        n = max(1, min(n, 20))
        first = _nvd({"keywordSearch": query, "resultsPerPage": 1})
        total = first.get("totalResults", 0)
        if not total:
            return f"no CVEs match '{query}'"
        vulns = _nvd({"keywordSearch": query, "resultsPerPage": n, "startIndex": max(0, total - n)}).get("vulnerabilities", [])
        vulns.reverse()  # NVD returns oldest first; show the newest
        detail = False
    if not vulns:
        return "not found in NVD"
    kev = _kev()
    epss = _epss([v["cve"]["id"] for v in vulns])
    out = [] if detail else [f"{total} CVEs match; newest {len(vulns)}:"]
    for v in vulns:
        c = v["cve"]
        cid = c["id"]
        desc = next((d["value"] for d in c.get("descriptions", []) if d.get("lang") == "en"), "")
        score, vector = _cvss(c.get("metrics", {}))
        flags = []
        if cid in kev:
            k = kev[cid]
            flags.append(f"EXPLOITED IN THE WILD (CISA KEV since {k.get('dateAdded')}"
                         f"{', used in ransomware' if k.get('knownRansomwareCampaignUse') == 'Known' else ''})")
        if cid in epss:
            p, pct = epss[cid]
            flags.append(f"EPSS {p * 100:.1f}% chance of exploitation in 30 days (higher than {pct * 100:.0f}% of CVEs)")
        if not detail:
            out.append(f"- {cid}  {c.get('published', '')[:10]}  {score}  {'[KEV] ' if cid in kev else ''}{_short(desc, 160)}")
            continue
        cwes = sorted({d["value"] for w in c.get("weaknesses", []) for d in w.get("description", []) if d["value"].startswith("CWE")})
        refs = c.get("references", [])
        fixes = [r["url"] for r in refs if set(r.get("tags", [])) & {"Patch", "Vendor Advisory", "Mitigation"}][:4]
        out.append(f"{cid}  published {c.get('published', '')[:10]}  status {c.get('vulnStatus', '')}\n"
                   f"CVSS {score}{'  ' + vector if vector else ''}\n"
                   + (f"weakness: {', '.join(cwes)}\n" if cwes else "")
                   + "".join(f"{f}\n" for f in flags)
                   + f"{_short(desc, 900)}\n"
                   + (f"patches/advisories: {' '.join(fixes)}\n" if fixes else f"references: {' '.join(r['url'] for r in refs[:3])}\n")
                   + (f"CISA action: {kev[cid].get('requiredAction')}\n" if cid in kev else ""))
    return "\n".join(out).strip()


# ── OSV: packages and lockfiles ──────────────────────────────────────────────

ECOSYSTEMS = {"pypi": "PyPI", "pip": "PyPI", "python": "PyPI", "npm": "npm", "node": "npm", "js": "npm",
              "cargo": "crates.io", "rust": "crates.io", "crates.io": "crates.io", "go": "Go", "golang": "Go",
              "maven": "Maven", "java": "Maven", "nuget": "NuGet", "rubygems": "RubyGems", "gem": "RubyGems",
              "ruby": "RubyGems", "packagist": "Packagist", "php": "Packagist", "debian": "Debian", "alpine": "Alpine"}


def _osv_fixed(v, name):
    fixed = []
    for a in v.get("affected", []):
        if a.get("package", {}).get("name", "").lower() != name.lower():
            continue
        for r in a.get("ranges", []):
            fixed += [e["fixed"] for e in r.get("events", []) if "fixed" in e]
    return sorted(set(fixed))


def _osv_severity(v):
    sev = (v.get("database_specific") or {}).get("severity")
    if sev:
        return sev.lower()
    for s in v.get("severity", []):
        m = re.search(r"/(?:AV|CVSS):", s.get("score", ""))
        if m:
            return s.get("type", "").lower()
    return "unrated"


@tool(pack="security")
def osv_check(package: str, version: str = "", ecosystem: str = "PyPI"):
    """Known vulnerabilities affecting one package version (OSV.dev covers PyPI, npm, crates.io, Go, Maven, NuGet, RubyGems, Packagist, Debian...). Shows the versions that fix each one.
    package: package name, e.g. 'requests' or 'lodash'
    version: installed version; empty lists every known advisory
    ecosystem: PyPI, npm, crates.io, Go, Maven, NuGet, RubyGems, Packagist"""
    eco = ECOSYSTEMS.get(ecosystem.lower(), ecosystem)
    q = {"package": {"name": package, "ecosystem": eco}}
    if version:
        q["version"] = version
    vulns = _json("https://api.osv.dev/v1/query", data=q).get("vulns", [])
    if not vulns:
        return f"no known vulnerabilities for {package} {version} ({eco})"
    out = [f"{len(vulns)} advisories for {package} {version} ({eco}):"]
    for v in vulns[:25]:
        aliases = [a for a in v.get("aliases", []) if a.startswith("CVE")]
        fixed = _osv_fixed(v, package)
        out.append(f"- {v['id']}{' / ' + ', '.join(aliases[:2]) if aliases else ''} [{_osv_severity(v)}] "
                   f"{_short(v.get('summary') or v.get('details'), 140)}"
                   f"{'  fixed in ' + ', '.join(fixed[-3:]) if fixed else '  no fix listed'}")
    return "\n".join(out)


def _parse_deps(root):
    """(ecosystem, name, version, source file) from the lockfiles we can read without extra packages."""
    deps, seen_files = [], []
    files = [root] if root.is_file() else []
    if root.is_dir():
        for dirpath, dirnames, fs in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            files += [Path(dirpath) / f for f in fs]
            if len(files) > 20000:
                break
    for f in files:
        name = f.name.lower()
        try:
            if re.fullmatch(r"requirements.*\.txt", name):
                for l in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    m = re.match(r"\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.\-+!]+)", l)
                    if m:
                        deps.append(("PyPI", m.group(1), m.group(2), f))
            elif name in ("poetry.lock", "uv.lock", "pdm.lock", "cargo.lock"):
                eco = "crates.io" if name == "cargo.lock" else "PyPI"
                text = f.read_text(encoding="utf-8", errors="replace")
                for block in text.split("[[package]]")[1:]:
                    n = re.search(r'^name\s*=\s*"([^"]+)"', block, re.M)
                    v = re.search(r'^version\s*=\s*"([^"]+)"', block, re.M)
                    if n and v:
                        deps.append((eco, n.group(1), v.group(1), f))
            elif name == "pipfile.lock":
                d = json.loads(f.read_text(encoding="utf-8"))
                for sec in ("default", "develop"):
                    for n, info in (d.get(sec) or {}).items():
                        v = str(info.get("version", "")).lstrip("=")
                        if v:
                            deps.append(("PyPI", n, v, f))
            elif name == "package-lock.json":
                d = json.loads(f.read_text(encoding="utf-8"))
                for key, info in (d.get("packages") or {}).items():
                    if key and info.get("version") and not info.get("link"):
                        deps.append(("npm", info.get("name") or key.rsplit("node_modules/", 1)[-1], info["version"], f))
                if not d.get("packages"):  # lockfile v1
                    def walk(tree):
                        for n, info in (tree or {}).items():
                            if info.get("version"):
                                deps.append(("npm", n, info["version"], f))
                            walk(info.get("dependencies"))
                    walk(d.get("dependencies"))
            elif name == "go.sum":
                for l in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    p = l.split()
                    if len(p) >= 2 and not p[1].endswith("/go.mod"):
                        deps.append(("Go", p[0], p[1].split("/")[0], f))
            elif name == "gemfile.lock":
                for m in re.finditer(r"^    ([A-Za-z0-9_\-]+) \(([0-9][^)]*)\)", f.read_text(encoding="utf-8", errors="replace"), re.M):
                    deps.append(("RubyGems", m.group(1), m.group(2), f))
            else:
                continue
            seen_files.append(f)
        except (OSError, ValueError):
            continue
    uniq = list(dict.fromkeys((e, n, v.lstrip("v") if e != "Go" else v, str(f)) for e, n, v, f in deps))
    return uniq, seen_files


@tool(pack="security")
def audit_deps(path: str = ".", _ctx=None):
    """Audit a project's pinned dependencies for known vulnerabilities (OSV.dev). Reads requirements*.txt, poetry.lock, uv.lock, Pipfile.lock, package-lock.json, Cargo.lock, go.sum and Gemfile.lock.
    path: project folder or a single lockfile"""
    root = _path(_ctx, path)
    deps, files = _parse_deps(root)
    if not deps:
        return f"no pinned dependencies found under {root} (looked for requirements*.txt with ==, and lockfiles)"
    pkgs = list(dict.fromkeys((e, n, v) for e, n, v, _ in deps))
    hits = {}
    for i in range(0, len(pkgs), 500):
        chunk = pkgs[i:i + 500]
        res = _json("https://api.osv.dev/v1/querybatch",
                    data={"queries": [{"package": {"name": n, "ecosystem": e}, "version": v} for e, n, v in chunk]}, timeout=60)
        for (e, n, v), r in zip(chunk, res.get("results", [])):
            if r.get("vulns"):
                hits[(e, n, v)] = [x["id"] for x in r["vulns"]]
    head = f"checked {len(pkgs)} packages from {len(files)} file(s): " + ", ".join(sorted({str(f.relative_to(root)) if root.is_dir() else f.name for f in files}))
    if not hits:
        return head + "\nno known vulnerabilities"
    ids = sorted({i for v in hits.values() for i in v})
    details = {}

    def get(vid):
        try:
            return vid, _json(f"https://api.osv.dev/v1/vulns/{vid}")
        except (OSError, ValueError, urllib.error.URLError):
            return vid, {}

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for vid, d in ex.map(get, ids[:80]):
            details[vid] = d
    out = [head, f"{len(hits)} vulnerable packages, {len(ids)} advisories:"]
    for (e, n, v), vids in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        fixed = sorted({x for vid in vids for x in _osv_fixed(details.get(vid, {}), n)})
        sevs = sorted({_osv_severity(details[vid]) for vid in vids if details.get(vid)})
        out.append(f"- {n} {v} ({e}): {len(vids)} [{', '.join(sevs) or '?'}]  "
                   f"{'upgrade to ≥ ' + fixed[-1] if fixed else 'no fixed version listed'}")
        for vid in vids[:3]:
            d = details.get(vid, {})
            cves = [a for a in d.get("aliases", []) if a.startswith("CVE")]
            out.append(f"    {vid}{' ' + cves[0] if cves else ''}: {_short(d.get('summary') or d.get('details'), 110)}")
        if len(vids) > 3:
            out.append(f"    …{len(vids) - 3} more (osv_check {n} {v})")
    return "\n".join(out)


# ── secrets ──────────────────────────────────────────────────────────────────

SECRETS = [
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("GitHub token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    ("GitLab token", r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b"),
    ("Slack webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("Stripe key", r"\b[sr]k_live_[0-9a-zA-Z]{20,}\b"),
    ("Anthropic key", r"\bsk-ant-[A-Za-z0-9_\-]{30,}\b"),
    ("OpenAI key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}\b"),
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    ("Twilio key", r"\bSK[0-9a-fA-F]{32}\b"),
    ("SendGrid key", r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("private key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    ("JWT", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ("URL with password", r"\b[a-z][a-z0-9+\-.]*://[^/\s:@]+:[^/\s:@]{3,}@[^\s/]+"),
    ("hard-coded password", r"(?i)\b(?:password|passwd|pwd|secret|api_?key|token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
]
SECRET_RES = [(n, re.compile(p)) for n, p in SECRETS]
PLACEHOLDER = re.compile(r"(?i)example|changeme|your[_\-]?|xxxx|placeholder|dummy|<[^>]+>|\$\{|\{\{|os\.environ|getenv|process\.env")


def _mask(s):
    s = s.strip()
    return s[:6] + "…" + f"({len(s)} chars)" if len(s) > 10 else s[:2] + "…"


@tool(pack="security")
def secret_scan(path: str = ".", _ctx=None):
    """Scan files for leaked credentials: cloud/API keys, tokens, private keys, passwords in code or URLs. Matches are masked. Skips .git, node_modules, virtualenvs and binaries.
    path: folder or file to scan"""
    root = _path(_ctx, path)
    files = [root] if root.is_file() else []
    if root.is_dir():
        for dirpath, dirnames, fs in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            files += [Path(dirpath) / f for f in fs]
    hits, scanned = [], 0
    for f in files[:50000]:
        try:
            if f.stat().st_size > 2_000_000 or f.suffix.lower() in (".png", ".jpg", ".gif", ".pdf", ".zip", ".gz", ".whl", ".so", ".dll", ".exe"):
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:2048]:
            continue
        scanned += 1
        for i, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            if len(line) > 5000:
                continue
            for name, rx in SECRET_RES:
                m = rx.search(line)
                if m and not (name in ("hard-coded password", "URL with password") and PLACEHOLDER.search(line)):
                    rel = f.relative_to(root) if root.is_dir() else f.name
                    hits.append(f"{rel}:{i}  {name}: {_mask(m.group(0))}")
                    break
        if len(hits) >= 200:
            break
    tracked = ""
    if hits and root.is_dir() and shutil.which("git") and (root / ".git").exists():
        r = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True)
        in_git = set(r.stdout.splitlines())
        n = sum(1 for h in hits if h.split(":", 1)[0].replace(os.sep, "/") in in_git)
        tracked = f"\n{n} of these are in files tracked by git: rotate those keys, removing them from the code isn't enough."
    if not hits:
        return f"scanned {scanned} files: no secrets found"
    return f"scanned {scanned} files, {len(hits)} possible secrets:\n" + "\n".join(hits) + tracked


# ── network checks ───────────────────────────────────────────────────────────

def _host_port(target, default=443):
    t = target.strip()
    if "://" in t:
        u = urllib.parse.urlparse(t)
        return u.hostname, u.port or (443 if u.scheme == "https" else 80)
    if t.count(":") == 1:
        h, p = t.split(":")
        return h, int(p)
    return t, default


@tool(pack="security")
def tls_check(host: str, port: int = 443):
    """Inspect a server's TLS: certificate subject, issuer, SANs, days until expiry, protocol version and cipher, and whether it validates.
    host: hostname or URL
    port: port, default 443"""
    h, p = _host_port(host, port)
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((h, p), timeout=10) as s, ctx.wrap_socket(s, server_hostname=h) as t:
            cert, proto, cipher = t.getpeercert(), t.version(), t.cipher()
    except ssl.SSLCertVerificationError as e:
        return f"{h}:{p} certificate does NOT validate: {e.verify_message or e}"
    except (OSError, ssl.SSLError) as e:
        return f"error: couldn't connect to {h}:{p}: {e}"

    def name(rdns):
        return ", ".join(f"{k}={v}" for rdn in rdns for k, v in rdn if k in ("commonName", "organizationName"))

    end = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    days = (end - datetime.now(timezone.utc)).days
    sans = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    notes = []
    if days < 0:
        notes.append("EXPIRED")
    elif days < 21:
        notes.append(f"expires soon ({days} days)")
    if proto in ("TLSv1", "TLSv1.1", "SSLv3"):
        notes.append(f"{proto} is obsolete")
    return (f"{h}:{p} valid certificate\nsubject: {name(cert.get('subject', ()))}\nissuer: {name(cert.get('issuer', ()))}\n"
            f"expires: {end:%Y-%m-%d} ({days} days)\nSANs: {', '.join(sans[:12])}{' …' if len(sans) > 12 else ''}\n"
            f"protocol: {proto}  cipher: {cipher[0]} ({cipher[2]} bit)" + (f"\nwarnings: {'; '.join(notes)}" if notes else ""))


HEADER_CHECKS = [
    ("strict-transport-security", "HSTS: forces HTTPS on return visits", lambda v: "max-age=0" not in v.lower()),
    ("content-security-policy", "CSP: limits where scripts can load from (main XSS defence)", lambda v: "unsafe-inline" not in v or "nonce-" in v or "strict-dynamic" in v),
    ("x-content-type-options", "stops MIME sniffing", lambda v: v.lower().strip() == "nosniff"),
    ("referrer-policy", "limits URL leakage to other sites", lambda v: v.lower() != "unsafe-url"),
    ("permissions-policy", "restricts camera, mic, geolocation…", lambda v: True),
    ("cross-origin-opener-policy", "isolates the window from cross-origin popups", lambda v: True),
]


@tool(pack="security")
def headers_audit(url: str):
    """Audit a website's HTTP security headers (HSTS, CSP, framing, nosniff, referrer, permissions), cookie flags, and version disclosure.
    url: page URL"""
    if "://" not in url:
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 " + UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, final, hdrs = r.status, r.geturl(), r.headers
    except urllib.error.HTTPError as e:
        status, final, hdrs = e.code, url, e.headers
    except (OSError, urllib.error.URLError) as e:
        return f"error: {e}"
    low = {k.lower(): v for k, v in hdrs.items()}
    out = [f"{final}  HTTP {status}"]
    good = 0
    for h, why, ok in HEADER_CHECKS:
        v = low.get(h)
        if v is None:
            out.append(f"  missing  {h}: {why}")
        elif not ok(v):
            out.append(f"  weak     {h}: {_short(v, 100)}")
        else:
            good += 1
            out.append(f"  ok       {h}: {_short(v, 80)}")
    csp = low.get("content-security-policy", "")
    if "frame-ancestors" in csp or low.get("x-frame-options"):
        good += 1
        out.append("  ok       clickjacking protection (frame-ancestors / X-Frame-Options)")
    else:
        out.append("  missing  X-Frame-Options or CSP frame-ancestors: page can be framed (clickjacking)")
    for h in ("server", "x-powered-by", "x-aspnet-version", "x-generator"):
        if low.get(h) and re.search(r"\d", low[h]):
            out.append(f"  leak     {h}: {low[h]} (version disclosure)")
    for ck in hdrs.get_all("Set-Cookie") or []:
        name, attrs = ck.split("=", 1)[0], ck.lower()
        missing = [f for f in ("secure", "httponly", "samesite") if f not in attrs]
        if missing:
            out.append(f"  cookie   {name}: missing {', '.join(missing)}")
    if final.startswith("http://"):
        out.append("  warning  page is served over plain HTTP")
    out.insert(1, f"score {good}/7")
    return "\n".join(out)


DNS_TYPES = {"A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA", "SRV", "PTR", "DS", "DNSKEY", "HTTPS"}


def _doh(name, rtype):
    d = _json("https://cloudflare-dns.com/dns-query?" + urllib.parse.urlencode({"name": name, "type": rtype}),
              headers={"Accept": "application/dns-json"})
    return [a["data"].strip('"') for a in d.get("Answer", []) if a.get("type") != 5 or rtype == "CNAME"], d.get("AD", False)


@tool(pack="security")
def dns_lookup(name: str, type: str = "A"):
    """DNS records over HTTPS (works even when local DNS is filtered). type=EMAIL checks a domain's email security: SPF, DMARC, MX, CAA and DNSSEC.
    name: domain name, or an IP address for a reverse (PTR) lookup
    type: A, AAAA, CNAME, MX, NS, TXT, SOA, CAA, SRV, PTR, HTTPS, or EMAIL"""
    t = type.upper()
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", name):
        name, t = ".".join(reversed(name.split("."))) + ".in-addr.arpa", "PTR"
    if t == "EMAIL":
        txt, ad = _doh(name, "TXT")
        dmarc, _ = _doh("_dmarc." + name, "TXT")
        mx, _ = _doh(name, "MX")
        caa, _ = _doh(name, "CAA")
        spf = [r for r in txt if r.lower().startswith("v=spf1")]
        dm = [r for r in dmarc if r.lower().startswith("v=dmarc1")]
        out = [f"{name} email security"]
        if not spf:
            out.append("  SPF    missing: anyone can send mail as this domain")
        else:
            s = spf[0]
            verdict = "strict (-all)" if s.rstrip().endswith("-all") else "soft (~all)" if "~all" in s else "WEAK (+all/?all)" if re.search(r"[+?]all", s) else "no all-mechanism"
            out.append(f"  SPF    {verdict}: {_short(s, 120)}" + ("  [multiple SPF records is an error]" if len(spf) > 1 else ""))
        if not dm:
            out.append("  DMARC  missing: spoofed mail isn't rejected and you get no reports")
        else:
            pol = re.search(r"\bp=(\w+)", dm[0])
            out.append(f"  DMARC  policy {pol.group(1) if pol else '?'}{' (monitoring only)' if pol and pol.group(1) == 'none' else ''}: {_short(dm[0], 120)}")
        out.append(f"  MX     {', '.join(mx) or 'none (domain does not receive mail)'}")
        out.append(f"  CAA    {', '.join(caa) or 'none (any CA may issue certificates)'}")
        out.append(f"  DNSSEC {'validated' if ad else 'not validated'}")
        return "\n".join(out)
    if t not in DNS_TYPES:
        return f"error: unsupported type {t}; use one of {', '.join(sorted(DNS_TYPES))} or EMAIL"
    recs, ad = _doh(name, t)
    return f"{name} {t}{' (DNSSEC validated)' if ad else ''}:\n" + ("\n".join("  " + r for r in recs) or "  no records")


@tool(pack="security")
def listening_ports():
    """List programs on this computer listening for network connections, flagging ones reachable from other machines (bound to all interfaces)."""
    if sys.platform.startswith("linux") and shutil.which("ss"):
        r = subprocess.run(["ss", "-tulpnH"], capture_output=True, text=True)
        rows = []
        for l in r.stdout.splitlines():
            p = l.split()
            if len(p) < 5:
                continue
            proto, local = p[0], p[4]
            proc = re.search(r'"([^"]+)",pid=(\d+)', l)
            addr, _, port = local.rpartition(":")
            exposed = addr.strip("[]").split("%")[0] in ("0.0.0.0", "*", "::", "")
            rows.append((not exposed, int(port) if port.isdigit() else 0,
                         f"{'EXPOSED ' if exposed else 'local   '}{proto:4} {local:28} {proc.group(1) + ' (pid ' + proc.group(2) + ')' if proc else '(run as root to see the process)'}"))
        rows = sorted(set(rows))
        n = sum(1 for r_ in rows if not r_[0])
        return f"{len(rows)} listening sockets, {n} on all interfaces (reachable from the network unless a firewall blocks them):\n" + "\n".join(r_[2] for r_ in rows)
    if sys.platform == "darwin":
        cmd = ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]
    elif os.name == "nt":
        cmd = ["netstat", "-ano", "-p", "TCP"]
    else:
        cmd = ["netstat", "-tuln"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if "LISTEN" in l.upper() or l.lower().startswith(("command", "udp"))]
    return "\n".join(lines[:150]) or r.stderr or "nothing listening"


COMMON_PORTS = [21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 389, 443, 445, 465, 587, 631, 993, 995, 1433, 1521,
                2049, 2375, 3000, 3306, 3389, 5000, 5432, 5672, 5900, 6379, 8000, 8080, 8443, 8888, 9000, 9090, 9200,
                11211, 11434, 27017]


def _ports(spec):
    if not spec or spec == "common":
        return COMMON_PORTS
    out = []
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return sorted(set(p for p in out if 0 < p < 65536))


@tool(pack="security", danger=True)
def port_check(host: str, ports: str = "common", banner: bool = True):
    """TCP connect check of which ports are open on a host, with service banners. Only for machines you own or are authorized to test.
    host: hostname or IP
    ports: 'common', a list like '22,80,443', or a range like '8000-8100' (max 1024 ports)
    banner: read the first bytes each open service sends"""
    plist = _ports(ports)
    if len(plist) > 1024:
        return "error: at most 1024 ports per check"
    try:
        ip = socket.gethostbyname(host)
    except OSError as e:
        return f"error: can't resolve {host}: {e}"

    def probe(p):
        try:
            with socket.create_connection((ip, p), timeout=1.0) as s:
                text = ""
                if banner:
                    s.settimeout(1.0)
                    try:
                        text = s.recv(160).decode("latin-1", "replace")
                    except OSError:
                        pass
                return p, re.sub(r"\s+", " ", "".join(ch for ch in text if ch.isprintable() or ch in "\r\n")).strip()
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        open_ = [r for r in ex.map(probe, plist) if r]
    if not open_:
        return f"{host} ({ip}): none of {len(plist)} ports open"
    rows = []
    for p, b in open_:
        try:
            svc = socket.getservbyport(p)
        except OSError:
            svc = ""
        rows.append(f"  {p:<6} {svc:12} {_short(b, 90)}")
    return f"{host} ({ip}): {len(open_)} of {len(plist)} ports open\n" + "\n".join(rows)


@tool(pack="security")
def file_hash(path: str, _ctx=None):
    """MD5, SHA-1 and SHA-256 of a file, e.g. to verify a download or look it up in malware databases.
    path: file path"""
    p = _path(_ctx, path)
    if not p.is_file():
        return f"error: {p} is not a file"
    hs = [hashlib.md5(), hashlib.sha1(), hashlib.sha256()]
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            for h in hs:
                h.update(block)
    return f"{p.name} ({p.stat().st_size} bytes)\nmd5     {hs[0].hexdigest()}\nsha1    {hs[1].hexdigest()}\nsha256  {hs[2].hexdigest()}"


# ── commands ─────────────────────────────────────────────────────────────────

@command("cve", "CVE details with KEV and EPSS: /cve <CVE-id or keywords>")
def cve_cmd(agent, arg):
    return cve_lookup(arg) if arg else "usage: /cve CVE-2024-3094"


@command("pwned", "check a password against Have I Been Pwned (never shown to the model)")
def pwned_cmd(agent, arg):
    try:
        pw = getpass.getpass("  password (hidden): ")
    except (EOFError, KeyboardInterrupt):
        return ""
    if not pw:
        return ""
    digest = hashlib.sha1(pw.encode()).hexdigest().upper()
    del pw
    body = _fetch("https://api.pwnedpasswords.com/range/" + digest[:5], headers={"Add-Padding": "true"})
    count = next((int(c) for s, c in (l.split(":") for l in body.splitlines() if ":" in l) if s == digest[5:]), 0)
    if count:
        return f"  this password appears in {count:,} known breaches; don't use it anywhere"
    return "  not found in known breaches (only the first 5 characters of its SHA-1 hash left this machine)"
