"""Research plugin: find papers, read abstracts, follow citations, cite them. No API keys.

Sources: arXiv, OpenAlex (250M+ works, citation counts, open-access links), PubMed,
Wikipedia, and doi.org for citations. Copy or symlink to ~/.lotus/plugins/.

Commands: /cite <doi|arXiv id> [bibtex|apa|mla|...]   /wiki <topic>"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from lotus.tools import command, pack, tool

pack("research", "find academic papers (arXiv, OpenAlex, PubMed), citations, BibTeX, Wikipedia")

UA = "lotus-agent/0.1 (research plugin)"


def _fetch(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": UA, **(headers or {})}
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(8_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def _json(url, **kw):
    return json.loads(_fetch(url, **kw))


def _short(text, n):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _authors(names, k=3):
    names = [n for n in names if n]
    return ", ".join(names[:k]) + (" et al." if len(names) > k else "")


ARXIV_ID = re.compile(r"(?:arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.I)


def _to_doi(ref):
    """Accept a DOI, a doi.org URL, or an arXiv id/URL; return a bare DOI."""
    ref = ref.strip()
    ref = re.sub(r"^https?://(dx\.)?doi\.org/", "", ref)
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?$", ref) or (None if ref.startswith("10.") else ARXIV_ID.fullmatch(ref))
    if m:
        return "10.48550/arXiv." + re.sub(r"v\d+$", "", m.group(1))
    return ref


# ── arXiv ────────────────────────────────────────────────────────────────────

@tool(pack="research")
def arxiv_search(query: str, n: int = 6, sort: str = "relevance"):
    """Search arXiv preprints (CS, physics, math, stats, biology...). Returns id, date, authors, title and a short abstract.
    query: keywords; supports arXiv syntax like 'ti:diffusion AND au:ho'
    n: number of results (max 20)
    sort: relevance or recent"""
    q = query if re.search(r"\b(ti|au|abs|cat|all):", query) else "all:" + query
    params = {"search_query": q, "start": 0, "max_results": max(1, min(n, 20)),
              "sortBy": "submittedDate" if sort.startswith("rec") else "relevance", "sortOrder": "descending"}
    root = ET.fromstring(_fetch("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for i, e in enumerate(root.findall("a:entry", ns), 1):
        aid = e.findtext("a:id", "", ns).rsplit("/abs/", 1)[-1]
        authors = [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)]
        out.append(f"{i}. [{aid}] {e.findtext('a:published', '', ns)[:10]}  {_short(e.findtext('a:title', '', ns), 160)}\n"
                   f"   {_authors(authors)}\n   {_short(e.findtext('a:summary', '', ns), 300)}")
    return "\n".join(out) or "no results"


# ── OpenAlex ─────────────────────────────────────────────────────────────────

def _abstract(inv):
    if not inv:
        return ""
    words = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in words)


def _work_line(i, w, abstract_chars=0):
    authors = [a.get("author", {}).get("display_name") for a in w.get("authorships") or []]
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    oa = (w.get("open_access") or {}).get("oa_url") or ""
    line = (f"{i}. {w.get('publication_year', '?')}  {_short(w.get('display_name'), 160)}\n"
            f"   {_authors(authors)}{' · ' + venue if venue else ''} · cited {w.get('cited_by_count', 0)}"
            f"{' · doi ' + doi if doi else ' · ' + w.get('id', '').rsplit('/', 1)[-1]}{' · pdf ' + oa if oa else ''}")
    if abstract_chars:
        ab = _abstract(w.get("abstract_inverted_index"))
        if ab:
            line += "\n   " + _short(ab, abstract_chars)
    return line


FIELDS = "id,doi,display_name,publication_year,cited_by_count,authorships,primary_location,open_access,abstract_inverted_index"


@tool(pack="research")
def paper_search(query: str, n: int = 6, since: int = 0, sort: str = "relevance"):
    """Search published papers across all fields (OpenAlex) with citation counts, DOIs and open-access PDF links.
    query: keywords
    n: number of results (max 25)
    since: only papers from this year on, e.g. 2020 (0 = any)
    sort: relevance, cited (most cited first) or recent"""
    params = {"search": query, "per-page": max(1, min(n, 25)), "select": FIELDS}
    if since:
        params["filter"] = f"from_publication_date:{int(since)}-01-01"
    if sort.startswith("cite"):
        params["sort"] = "cited_by_count:desc"
    elif sort.startswith("rec"):
        params["sort"] = "publication_date:desc"
    d = _json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    rows = [_work_line(i, w, 220) for i, w in enumerate(d.get("results", []), 1)]
    return (f"{d.get('meta', {}).get('count', 0)} matches\n" + "\n".join(rows)) if rows else "no results"


def _openalex_work(ref):
    ref = ref.strip()
    if re.fullmatch(r"W\d+", ref, re.I):
        key = ref.upper()
    else:
        key = "doi:" + _to_doi(ref)
    return _json(f"https://api.openalex.org/works/{urllib.parse.quote(key, safe=':/')}")


@tool(pack="research")
def paper_details(ref: str):
    """Full record for one paper: abstract, authors, venue, topics, citation count, open-access link.
    ref: DOI, doi.org URL, arXiv id, or OpenAlex id like W2741809807"""
    try:
        w = _openalex_work(ref)
    except urllib.error.HTTPError as e:
        return f"error: not found in OpenAlex ({e.code}); try arxiv_search or check the DOI"
    out = [_work_line(1, w)[3:]]
    topics = [t.get("display_name") for t in (w.get("topics") or [])[:5]]
    if topics:
        out.append("topics: " + ", ".join(topics))
    out.append(f"references: {len(w.get('referenced_works') or [])}  openalex: {w.get('id', '').rsplit('/', 1)[-1]}")
    ab = _abstract(w.get("abstract_inverted_index"))
    out.append("\nabstract: " + (_short(ab, 2500) if ab else "(not available; fetch the paper page)"))
    return "\n".join(out)


@tool(pack="research")
def paper_citations(ref: str, n: int = 8, direction: str = "cited_by"):
    """Follow the citation graph: papers that cite this one (most cited first), or the papers it references.
    ref: DOI, arXiv id, or OpenAlex id
    n: number of results (max 25)
    direction: cited_by or references"""
    try:
        w = _openalex_work(ref)
    except urllib.error.HTTPError as e:
        return f"error: not found in OpenAlex ({e.code})"
    wid = w["id"].rsplit("/", 1)[-1]
    flt = f"cites:{wid}" if direction.startswith("cite") else f"cited_by:{wid}"
    params = {"filter": flt, "sort": "cited_by_count:desc", "per-page": max(1, min(n, 25)), "select": FIELDS}
    d = _json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    rows = [_work_line(i, x) for i, x in enumerate(d.get("results", []), 1)]
    head = f"{_short(w.get('display_name'), 100)}: {d.get('meta', {}).get('count', 0)} {'citing papers' if flt.startswith('cites') else 'references'}"
    return head + "\n" + ("\n".join(rows) or "none found")


# ── PubMed ───────────────────────────────────────────────────────────────────

@tool(pack="research")
def pubmed_search(query: str, n: int = 6):
    """Search PubMed (biomedical and life-science literature).
    query: keywords or PubMed syntax, e.g. 'crispr[ti] AND 2023[dp]'
    n: number of results (max 20)"""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    ids = _json(base + "esearch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "term": query, "retmode": "json", "retmax": max(1, min(n, 20)), "sort": "relevance"}))
    idlist = ids.get("esearchresult", {}).get("idlist", [])
    if not idlist:
        return "no results"
    s = _json(base + "esummary.fcgi?" + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(idlist), "retmode": "json"}))
    out = []
    for i, pid in enumerate(idlist, 1):
        r = s.get("result", {}).get(pid, {})
        doi = next((a["value"] for a in r.get("articleids", []) if a.get("idtype") == "doi"), "")
        out.append(f"{i}. PMID {pid}  {r.get('pubdate', '')[:4]}  {_short(r.get('title'), 180)}\n"
                   f"   {_authors([a.get('name') for a in r.get('authors', [])])} · {r.get('source', '')}"
                   f"{' · doi ' + doi if doi else ''}")
    total = ids.get("esearchresult", {}).get("count", "?")
    return f"{total} matches\n" + "\n".join(out)


# ── citations ────────────────────────────────────────────────────────────────

def _citation(ref, style="bibtex"):
    doi = _to_doi(ref)
    if style.lower() in ("bibtex", "bib"):
        accept = "application/x-bibtex"
    else:
        accept = f"text/x-bibliography; style={style.lower()}"
    return _fetch("https://doi.org/" + urllib.parse.quote(doi, safe="/:"), headers={"Accept": accept}).strip()


@tool(pack="research")
def cite(ref: str, style: str = "bibtex"):
    """Get a citation for a paper from its DOI or arXiv id.
    ref: DOI, doi.org URL, or arXiv id
    style: bibtex, apa, mla, chicago-author-date, ieee, harvard-cite-them-right, vancouver"""
    try:
        return _citation(ref, style)
    except urllib.error.HTTPError as e:
        return f"error: doi.org couldn't resolve {_to_doi(ref)} ({e.code})"


@tool(pack="research", danger=True)
def bib_add(ref: str, file: str = "references.bib", _ctx=None):
    """Append a paper's BibTeX entry to a .bib file in the working directory (skips duplicates).
    ref: DOI or arXiv id
    file: bib file path"""
    p = Path(file).expanduser()
    p = p if p.is_absolute() else Path(_ctx.cwd) / p
    doi = _to_doi(ref)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    if doi.lower() in existing.lower():
        return f"{doi} is already in {p.name}"
    entry = _citation(doi, "bibtex")
    if not entry.startswith("@"):
        return f"error: no BibTeX for {doi}"
    p.write_text(existing.rstrip() + ("\n\n" if existing.strip() else "") + entry + "\n", encoding="utf-8")
    key = entry.split("{", 1)[1].split(",", 1)[0]
    return f"added {key} to {p}"


# ── Wikipedia ────────────────────────────────────────────────────────────────

@tool(pack="research")
def wikipedia(topic: str, lang: str = "en"):
    """Short Wikipedia summary of a topic, with the page link. Good for quick background.
    topic: article title or search words
    lang: language code, e.g. en, de, fr"""
    base = f"https://{lang}.wikipedia.org"
    hits = _json(base + "/w/api.php?" + urllib.parse.urlencode(
        {"action": "opensearch", "search": topic, "limit": 5, "namespace": 0, "format": "json"}))
    titles = hits[1] if len(hits) > 1 else []
    if not titles:
        return f"no Wikipedia article for '{topic}'"
    s = _json(base + "/api/rest_v1/page/summary/" + urllib.parse.quote(titles[0].replace(" ", "_")))
    out = f"{s.get('title')}: {s.get('extract', '')}\n{(s.get('content_urls') or {}).get('desktop', {}).get('page', '')}"
    if len(titles) > 1:
        out += "\nother matches: " + ", ".join(titles[1:])
    return out


# ── commands ─────────────────────────────────────────────────────────────────

@command("cite", "citation for a DOI or arXiv id: /cite <ref> [bibtex|apa|mla|ieee]")
def cite_cmd(agent, arg):
    parts = arg.split()
    if not parts:
        return "usage: /cite <doi|arXiv id> [style]"
    return cite(parts[0], parts[1] if len(parts) > 1 else "bibtex")


@command("wiki", "Wikipedia summary: /wiki <topic>")
def wiki_cmd(agent, arg):
    return wikipedia(arg) if arg else "usage: /wiki <topic>"
