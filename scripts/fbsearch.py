#!/usr/bin/env python3
"""fbsearch.py - helper CLI for the citation-snowball skill (https://github.com/Andy-LZH/citation-snowball).

Stages (each reads/writes JSON in a work directory):

  backward SEED  resolve the seed paper, digest its full text (tables, related work, citation sentences), fetch its
                 references (recovering ones Semantic Scholar lacks from the bibliography) and propose the core
                 comparison set                       -> seed.json digest.json fulltext.txt refs.json core.json
  forward        gather candidates (citers of the seed, newest citers of each core paper, a citation-sorted topic
                 search, optionally OpenAlex's most-cited citers), verify each candidate's reference list, apply
                 the citation threshold, fetch abstracts and code links (GitHub / Hugging Face)
                                                      -> candidates.json briefs.md
  report         render the markdown report, merging the agent-written diffs.json -> report.md

Helpers: doctor (check APIs, keys, tools) | resolve SEED | code ID... (debug code discovery)

HTTP responses are cached in ~/.cache/citation-snowball (--cache-dir or FBS_CACHE_DIR to change).
Standard library only (Python 3.8+). Needs network access.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

VERSION = "1.2.0"
S2 = "https://api.semanticscholar.org/graph/v1"
OPENALEX = "https://api.openalex.org"
UA = "citation-snowball/%s (+https://github.com/Andy-LZH/citation-snowball; python-urllib)" % VERSION
MISSING = "\x00missing\x00"
HUB_MIN_CITATIONS = 5000  # core papers above max(this, 10x the seed's citations) count half toward relevance
SURVEY_RE = re.compile(r"\b(survey|review|overview|tutorial|primer)\b", re.I)
# Keyless Semantic Scholar shares one rate-limited pool with every other keyless user, so most requests get 429 at
# busy times. Keep retrying one request for up to this many seconds before giving up.
S2_PATIENCE = float(os.environ.get("FBS_S2_PATIENCE", "900"))

PAPER_FIELDS = "paperId,title,year,venue,publicationDate,citationCount,influentialCitationCount,externalIds"
DETAIL_FIELDS = PAPER_FIELDS + ",abstract,tldr,publicationTypes,openAccessPdf,authors,url"
REF_FIELDS = "contexts,intents,isInfluential," + PAPER_FIELDS
CIT_FIELDS = "intents,isInfluential," + PAPER_FIELDS

CORE_ROLES = ("baseline", "benchmarked_model", "compared_dataset", "compared_benchmark", "related")
ROLE_LABEL = {
    "baseline": "baseline",
    "benchmarked_model": "benchmarked model",
    "compared_dataset": "compared dataset",
    "compared_benchmark": "compared benchmark",
    "eval_dataset": "evaluation dataset",
    "related": "related work",
    "background": "background ref",
}
BASIC_STOPWORDS = set("""a an and are as at be by for from has have in into is it its of on or that the this to via
with without towards toward using use based new novel approach approaches method methods model models
learning large language deep neural network networks paper study analysis framework improving improved
efficient effective simple general task tasks data through beyond what when how can not your our we""".split())
# Also generic for picking topic keywords out of a title ("high quality", "rethinking", ...).
STOPWORDS = BASIC_STOPWORDS | set("""high quality better fast faster robust strong scaling scalable unified revisiting
rethinking all any more less one two three first fully very only just make makes made does like""".split())


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class HttpFailure(RuntimeError):
    pass


def s2_key() -> str | None:
    return os.environ.get("S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")


def default_cache_dir() -> str:
    if os.environ.get("FBS_CACHE_DIR"):
        return os.environ["FBS_CACHE_DIR"]
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "citation-snowball")


def writable_dir(path: str | None) -> str | None:
    """`path` if it can be created and written, else a temp-dir fallback (sandboxed agents), else None (no cache)."""
    for cand in (path, os.path.join(tempfile.gettempdir(), "citation-snowball-cache")):
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write-test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return cand
        except OSError:
            continue
    return None


class Http:
    """Tiny HTTP client: per-host throttling, retries with backoff, and an on-disk cache."""

    INTERVAL = {
        "api.semanticscholar.org": 1.1,
        "export.arxiv.org": 3.1,
        "arxiv.org": 1.0,
        "ar5iv.labs.arxiv.org": 1.0,
        "api.openalex.org": 0.2,
        "huggingface.co": 0.15,
        "api.github.com": 0.25,
        "api.crossref.org": 0.3,
        "api.github.com/search": 2.1,
    }

    def __init__(self, cache_dir: str | None, refresh: bool = False):
        self.cache_dir = writable_dir(cache_dir) if cache_dir else None
        self.refresh = refresh
        self.last = {}
        self.counts = {}
        self.s2_busy = 0  # 429 retries against Semantic Scholar (reported in summaries)
        self.failures = {}  # host -> requests that failed even after retries (network down, server errors)

    def _throttle(self, host: str) -> None:
        wait = self.last.get(host, 0.0) + self.INTERVAL.get(host, 0.2) - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last[host] = time.time()

    def _path(self, url: str, body: bytes | None) -> str:
        key = hashlib.sha1(url.encode() + b"\n" + (body or b"")).hexdigest()
        host = urllib.parse.urlsplit(url).netloc.replace(":", "_")
        return os.path.join(self.cache_dir, host, key)

    def fetch(self, url, data=None, headers=None, as_json=True, ttl_days=14.0, tries=7, cache=True):
        body = json.dumps(data).encode() if data is not None else None
        cache = cache and bool(self.cache_dir)
        path = self._path(url, body) if cache else ""
        if cache and not self.refresh and os.path.exists(path):
            if time.time() - os.path.getmtime(path) < ttl_days * 86400:
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
                if raw == MISSING:
                    return None
                try:
                    return json.loads(raw) if as_json else raw
                except ValueError:
                    pass  # corrupt cache entry: fetch again
        host = urllib.parse.urlsplit(url).netloc
        lane = host + ("/search" if host == "api.github.com" and "/search/" in url else "")
        hdrs = {"User-Agent": UA, "Accept": "application/json" if as_json else "*/*"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        s2_keyless = host == "api.semanticscholar.org" and "x-api-key" not in hdrs
        delay, attempt, busy, started = 2.0, 0, 0, time.time()
        while True:
            attempt += 1
            self._throttle(lane)
            self.counts[host] = self.counts.get(host, 0) + 1
            req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body is not None else "GET")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                result = json.loads(raw) if as_json else raw
                if cache:
                    self._store(path, raw)
                return result
            except urllib.error.HTTPError as err:
                if err.code in (404, 410):
                    if cache:
                        self._store(path, MISSING)
                    return None
                if err.code == 429 and s2_keyless and time.time() - started < S2_PATIENCE:
                    busy += 1
                    self.s2_busy += 1
                    if busy in (8, 40) or busy % 150 == 0:
                        log("  Semantic Scholar's shared keyless pool is busy (%d retries, %.0fs on this request); still "
                            "retrying. Setting S2_API_KEY avoids this." % (busy, time.time() - started))
                    time.sleep(random.uniform(1.0, 3.0))
                    continue
                if err.code in (429, 500, 502, 503, 504) and attempt < tries:
                    retry_after = (err.headers or {}).get("Retry-After", "")
                    wait = float(retry_after) if retry_after.isdigit() else delay * random.uniform(0.8, 1.2)
                    log("  HTTP %d from %s; retrying in %.0fs" % (err.code, host, wait))
                    time.sleep(min(wait, 120))
                    delay = min(delay * 2, 64)
                    continue
                detail = ""
                try:
                    detail = err.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                self.failures[host] = self.failures.get(host, 0) + 1
                raise HttpFailure("HTTP %d for %s %s" % (err.code, url, detail)) from None
            except ValueError as err:  # body was not JSON
                raise HttpFailure("bad JSON from %s: %s" % (url, err)) from None
            except OSError as err:  # URLError, timeouts, resets
                if attempt < tries:
                    log("  network error from %s (%s); retrying in %.0fs" % (host, err, delay))
                    time.sleep(delay)
                    delay = min(delay * 2, 64)
                    continue
                self.failures[host] = self.failures.get(host, 0) + 1
                raise HttpFailure("network error for %s: %s" % (url, err)) from None

    def download(self, url: str, dest: str) -> bool:
        host = urllib.parse.urlsplit(url).netloc
        self._throttle(host)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            return True
        except OSError as err:
            log("  download failed for %s: %s" % (url, err))
            return False

    @staticmethod
    def _store(path: str, raw: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw)
        except OSError:
            pass  # caching is best effort


# ----------------------------------------------------------------------------- small utils

def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def alnum(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def short_id(paper_id: str | None) -> str:
    return (paper_id or "")[:8]


def human(n) -> str:
    if n is None:
        return ""
    n = int(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if n >= 10_000:
        return "%dk" % round(n / 1000)
    if n >= 1000:
        return "%.1fk" % (n / 1000)
    return str(n)


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def slugify(title: str, words: int = 6) -> str:
    toks = [t for t in norm(title).split() if t not in BASIC_STOPWORDS] or norm(title).split()
    return "-".join(toks[:words])[:60] or "seed"


def keywords_from(text: str) -> list:
    return [t for t in norm(text).split() if len(t) >= 4 and t not in STOPWORDS]


def months_old(paper: dict, today: dt.date) -> int | None:
    date = paper.get("publicationDate") or ""
    if re.match(r"\d{4}-\d{2}", date):
        y, m = int(date[:4]), int(date[5:7])
    elif paper.get("year"):
        y, m = int(paper["year"]), 7
    else:
        return None
    return (today.year - y) * 12 + (today.month - m)


def passes_threshold(paper: dict, cfg: dict, today: dt.date) -> bool:
    cites = paper.get("citationCount") or 0
    if cites >= cfg["min_citations"]:
        return True
    age = months_old(paper, today)
    return age is not None and age <= cfg["recent_months"] and cites >= cfg["recent_min_citations"]


def paper_link(p: dict) -> str:
    ext = p.get("externalIds") or {}
    if ext.get("ArXiv"):
        return "https://arxiv.org/abs/%s" % ext["ArXiv"]
    if ext.get("DOI"):
        return "https://doi.org/%s" % ext["DOI"]
    if ext.get("ACL"):
        return "https://aclanthology.org/%s" % ext["ACL"]
    if p.get("paperId"):
        return "https://www.semanticscholar.org/paper/%s" % p["paperId"]
    return ""


def scholar_link(title: str | None) -> str:
    """A Google Scholar search the reader can click. The script itself never queries Google Scholar."""
    return "https://scholar.google.com/scholar?q=" + urllib.parse.quote('allintitle:"%s"' % re.sub(r"\s+", " ", title or "").strip())


def first_author(p: dict) -> str:
    authors = p.get("authors") or []
    if not authors:
        return ""
    last = (authors[0].get("name") or "").split()[-1:] or [""]
    return last[0] + (" et al." if len(authors) > 1 else "")


def short_name(title: str) -> str | None:
    """'LoRA: Low-Rank ...' -> 'LoRA'; 'BERT pre-training ...' -> 'BERT'; else None."""
    t = (title or "").strip()
    if ":" in t:
        head = t.split(":", 1)[0].strip()
        if 1 <= len(head.split()) <= 4 and len(alnum(head)) >= 3:
            return head
    words = t.split()
    if words:
        first = words[0].strip(",.")
        hyphenated_title_case = re.fullmatch(r"[A-Z][a-z]+(-[A-Z]?[a-z]+)+", first)  # "Fine-Grained", not a name
        if len(first) >= 3 and sum(ch.isupper() for ch in first) >= 2 and not hyphenated_title_case:
            return first
    return None


def label(title: str | None, width: int = 28) -> str:
    """Short display name: the paper's own short name ("LoRA"), else its first words."""
    name = short_name(title or "")
    if name:
        return name
    out = ""
    for word in (title or "").split():
        if len(out) + len(word) + 1 > width:
            return out + "…"
        out = (out + " " + word).strip()
    return out


# ----------------------------------------------------------------------------- Semantic Scholar

def s2_headers() -> dict:
    key = s2_key()
    return {"x-api-key": key} if key else {}


def s2_url(path: str, **params) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, safe=",:")
    return "%s/%s%s" % (S2, path, ("?" + query) if query else "")


def s2_paper(http: Http, pid: str, fields: str = DETAIL_FIELDS):
    return http.fetch(s2_url("paper/" + urllib.parse.quote(pid, safe=":/"), fields=fields), headers=s2_headers())


def s2_batch(http: Http, ids: list, fields: str = DETAIL_FIELDS, chunk: int = 250) -> dict:
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        try:
            rows = http.fetch(s2_url("paper/batch", fields=fields), data={"ids": part}, headers=s2_headers()) or []
        except HttpFailure as err:
            log("  batch lookup failed: %s" % err)
            continue
        for pid, row in zip(part, rows):
            if row:
                out[pid] = row
    return out


def s2_edges(http: Http, pid: str, kind: str, fields: str, cap: int) -> list:
    """kind = 'references' | 'citations'. Pages through results (1000 per call) up to `cap`.

    Citations come newest first, and offset + limit must stay below 10,000, so for a heavily cited paper this only
    reaches its most recent citers. `forward` makes up for that with a citation-sorted topic search.
    """
    out, offset = [], 0
    cap = min(cap, 9999)
    while len(out) < cap:
        limit = min(1000, cap - len(out), 9999 - offset)
        if limit <= 0:
            break
        try:
            page = http.fetch(s2_url("paper/%s/%s" % (pid, kind), fields=fields, limit=limit, offset=offset),
                              headers=s2_headers(), ttl_days=7)
        except HttpFailure as err:
            log("  %s page at offset %d failed: %s" % (kind, offset, err))
            break
        if not page:
            break
        rows = page.get("data") or []
        out.extend(rows)
        if page.get("next") is None or not rows:
            break
        offset = page["next"]
    return out


def s2_bulk_search(http: Http, query: str, min_citations: int, limit: int, year: str | None = None) -> list:
    """Papers matching a boolean query (+ | - "phrase" prefix*), most cited first."""
    out, token = [], None
    while len(out) < limit:
        params = {"query": query, "fields": PAPER_FIELDS, "sort": "citationCount:desc",
                  "minCitationCount": min_citations}
        if year:
            params["year"] = year
        if token:
            params["token"] = token
        try:
            page = http.fetch("%s/paper/search/bulk?%s" % (S2, urllib.parse.urlencode(params)), headers=s2_headers(),
                              ttl_days=7)
        except HttpFailure as err:
            log("  topic search failed: %s" % err)
            break
        rows = (page or {}).get("data") or []
        out.extend(rows)
        token = (page or {}).get("token")
        if not token or not rows:
            break
    return out[:limit]


def s2_reference_ids(http: Http, ids: list, deep: set | None = None, max_single: int = 40) -> dict:
    """paperId -> set of the paperIds it references (None when no reference list could be obtained).

    Large batch requests silently return empty reference lists for many papers (about half of a 250-paper batch in
    one test), so batches are kept at 100 papers, empties are retried in batches of 10, and for the ids in `deep`
    that are still empty the per-paper references endpoint is tried (up to `max_single` calls).
    """
    out = {}
    todo = [i for i in dict.fromkeys(ids) if i]
    for chunk in (100, 10):
        retry = []
        for start in range(0, len(todo), chunk):
            part = todo[start:start + chunk]
            try:
                rows = http.fetch(s2_url("paper/batch", fields="referenceCount,references.paperId"), data={"ids": part},
                                  headers=s2_headers()) or []
            except HttpFailure as err:
                log("  reference-list batch failed: %s" % err)
                retry += part
                continue
            for pid, row in zip(part, rows):
                refs = {r.get("paperId") for r in (row or {}).get("references") or [] if r.get("paperId")}
                out[pid] = refs or None
                if not refs and (row or {}).get("referenceCount"):
                    retry.append(pid)
        todo = retry
        if not todo:
            break
    for pid in [p for p in todo if deep and p in deep][:max_single]:
        refs = {(e.get("citedPaper") or {}).get("paperId") for e in s2_edges(http, pid, "references", "paperId", cap=1000)}
        refs.discard(None)
        out[pid] = refs or None
    return out


def s2_match(http: Http, title: str) -> dict | None:
    try:
        res = http.fetch(s2_url("paper/search/match", query=title, fields=PAPER_FIELDS), headers=s2_headers())
    except HttpFailure as err:
        log("  title match failed: %s" % err)
        return None
    return ((res or {}).get("data") or [None])[0]


def title_close(a: str | None, b: str | None) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    short, long_ = sorted((na, nb), key=len)
    if len(short.split()) >= 4 and (" %s " % short) in (" %s " % long_):  # "Vector-based ..." in "VeRA: Vector-based ..."
        return True
    return len(ta & tb) / max(len(ta | tb), 1) >= 0.85


def parse_seed(seed: str) -> str | None:
    """Turn an arXiv/DOI/S2/ACL id or URL into a Semantic Scholar paper id. None means 'treat as title'."""
    s = seed.strip()
    m = re.match(r"(?i)^(arxiv|doi|corpusid|acl|pmid|pmcid|mag|url):\s*(.+)$", s)
    if m:
        prefix = {"arxiv": "ARXIV", "doi": "DOI", "corpusid": "CorpusId", "acl": "ACL", "pmid": "PMID",
                  "pmcid": "PMCID", "mag": "MAG", "url": "URL"}[m.group(1).lower()]
        return prefix + ":" + m.group(2).strip()
    new_arxiv = re.search(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)", s)
    old_arxiv = re.search(r"\b([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b", s)
    if re.search(r"arxiv\.org|alphaxiv\.org|huggingface\.co/papers|ar5iv", s, re.I) and (new_arxiv or old_arxiv):
        return "ARXIV:" + (new_arxiv or old_arxiv).group(1)
    doi = re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+)", s)
    if doi:
        d = doi.group(1).rstrip(".,;)]")
        am = re.match(r"(?i)10\.48550/arxiv\.(.+)$", d)
        return ("ARXIV:" + am.group(1)) if am else ("DOI:" + d)
    if "semanticscholar.org" in s:
        sm = re.search(r"([0-9a-f]{40})", s)
        if sm:
            return sm.group(1)
    acl = re.search(r"aclanthology\.org/([A-Za-z0-9.\-]+?)(?:\.pdf)?/?$", s)
    if acl:
        return "ACL:" + acl.group(1)
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", s) or re.fullmatch(r"[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?", s):
        return "ARXIV:" + re.sub(r"v\d+$", "", s)
    if re.fullmatch(r"[0-9a-f]{40}", s):
        return s
    return None


def openreview_title(http: Http, seed: str) -> str | None:
    m = re.search(r"openreview\.net/(?:forum|pdf)\?id=([A-Za-z0-9_\-]+)", seed)
    if not m:
        return None
    for base in ("https://api2.openreview.net", "https://api.openreview.net"):
        try:
            res = http.fetch("%s/notes?id=%s" % (base, m.group(1)))
        except HttpFailure:
            continue
        notes = (res or {}).get("notes") or []
        if notes:
            title = (notes[0].get("content") or {}).get("title")
            return title.get("value") if isinstance(title, dict) else title
    return None


def resolve(http: Http, seed: str) -> dict:
    pid = parse_seed(seed)
    if pid:
        paper = s2_paper(http, pid)
        if paper:
            return paper
        log("Semantic Scholar does not know %s yet; falling back to title search." % pid)
        if pid.startswith("ARXIV:"):
            meta = arxiv_meta(http, [pid[6:]]).get(pid[6:])
            seed = meta["title"] if meta else seed
    title = openreview_title(http, seed) or seed
    res = None
    try:
        res = http.fetch(s2_url("paper/search/match", query=title, fields=DETAIL_FIELDS), headers=s2_headers())
    except HttpFailure as err:
        log("  title match failed: %s" % err)
    if res and res.get("data"):
        return res["data"][0]
    hits = http.fetch(s2_url("paper/search", query=title, limit=8, fields=PAPER_FIELDS), headers=s2_headers()) or {}
    lines = ["Could not resolve the seed unambiguously. Candidates:"]
    for h in hits.get("data") or []:
        lines.append("  %s  %s (%s) cites=%s ids=%s" % (h.get("paperId"), h.get("title"), h.get("year"),
                                                      h.get("citationCount"), h.get("externalIds")))
    lines.append("Re-run with one of the paperIds above (or an arXiv id / DOI).")
    raise SystemExit("\n".join(lines))


# ----------------------------------------------------------------------------- arXiv full text digest

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.kids, self.parent = tag, attrs, [], parent

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def iter(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(k for k in reversed(node.kids) if isinstance(k, Node))

    def text(self) -> str:
        out = []

        def walk(n):
            if n.tag == "math":
                out.append(" %s " % (n.attrs.get("alttext") or ""))
                return
            for k in n.kids:
                if isinstance(k, Node):
                    if k.tag in ("script", "style", "annotation", "annotation-xml"):
                        continue
                    walk(k)
                else:
                    out.append(k)
        walk(self)
        return re.sub(r"\s+", " ", "".join(out)).strip()


class DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.cur)
        self.cur.kids.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.kids.append(Node(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.kids.append(data)


BLOCK_TAGS = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "figcaption",
              "table", "figure", "blockquote", "ul", "ol", "br"}


def text_with_breaks(root: Node) -> str:
    """Plain text with a line per paragraph, heading and table row (cells joined by ' | '), so tables stay readable."""
    out = []

    def walk(n):
        if n.tag == "math":
            out.append(" %s " % (n.attrs.get("alttext") or ""))
            return
        if n.tag in ("script", "style", "annotation", "annotation-xml"):
            return
        block = n.tag in BLOCK_TAGS
        if block:
            out.append("\n")
        for k in n.kids:
            if isinstance(k, Node):
                walk(k)
            else:
                out.append(re.sub(r"\s+", " ", k))  # source newlines are layout, not structure
        if n.tag in ("td", "th"):
            out.append(" | ")
        if block:
            out.append("\n")
    walk(root)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(out))
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return re.sub(r"\| *\n", "\n", text).strip()


def parse_html(text: str) -> Node:
    builder = DomBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


SECTION_KINDS = [
    ("related_work", r"related work|prior work|previous work|literature|background|preliminar"),
    ("experiments", r"experiment|evaluation|result|benchmark|comparison|empirical|performance|analysis"),
    ("dataset", r"dataset|data collection|data construction|corpus|annotation|data statistics|data curation"),
    ("method", r"method|approach|model|architecture|framework|algorithm|proposed"),
    ("intro", r"introduction|motivation"),
    ("conclusion", r"conclusion|discussion|limitation|future work|broader impact"),
]


def section_kind(title: str) -> str:
    t = title.lower()
    for kind, pattern in SECTION_KINDS:
        if re.search(pattern, t):
            return kind
    return "other"


def table_kind(caption: str) -> str:
    """'dataset_comparison' (rows are other datasets/benchmarks), 'results', 'ignore' (hyper-parameters...) or 'other'."""
    c = caption.lower()
    results = (r"result|perform|accura|score|compar|evaluat|baseline|state-of-the-art|\bsota\b|leaderboard|win rate|"
               r"error|\bf1\b|bleu|rouge|top-[15]|perplexity|benchmark|ablation|outperform|metric")
    if re.search(r"hyper-?parameter|implementation detail|configuration|training (setting|detail)|prompt template|"
                 r"notation", c) and not re.search(results, c):
        return "ignore"
    data = r"(datasets?|benchmarks?|corpus|corpora|data ?sets?|suites?|testbeds?)\b"
    if (re.search(r"\b(existing|other|previous|prior|related|popular|public|common|representative) %s" % data, c)
            or re.search(r"\b%s (statistics|comparison|overview|characteristics)" % data, c)
            or re.search(r"\b(statistics|overview|characteristics|summary) of (the |existing |different |popular )?%s" % data, c)
            or re.search(r"\bcompar\w* (of|between|with|to|against) (our |the proposed |the |\w+ )?%s" % data, c)) \
            and not re.search(r"state-of-the-art|\bsota\b|\bmethods\b|\bapproaches\b|\bbaselines\b", c):
        return "dataset_comparison"
    if re.search(results, c):
        return "results"
    return "other"


BIB_HREF = re.compile(r"#(bib\.bib\d+)$")


def cited_bibs(node: Node) -> list:
    out = []
    for n in node.iter():
        if n.tag == "a":
            m = BIB_HREF.search(n.attrs.get("href") or "")
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


SENTENCE_END = re.compile(r"(?<!\bal)(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\bFig)(?<!\bTab)(?<!\bEq)(?<!\bSec)(?<!\bcf)"
                          r"[.!?]\s+(?=[A-Z(\[])")


USE_BEFORE_CITE = re.compile(r"(\b(on|using|via|upon|atop|based on|built on|fine-?tun\w*|pre-?train\w* on|"
                             r"backbones?|initiali[sz]ed (from|with))\s+(the\s+)?[\w.\-]+(\s[\w.\-]+)?\s*[(\[]?\s*$)",
                             re.I)


def cite_contexts(root: Node, per_bib: int = 3) -> tuple:
    """-> (bib id -> up to `per_bib` citing sentences from body paragraphs,
           bib id -> share of its citations that read as usage, e.g. "fine-tuning LLaMA [37]", "on COCO [31]")."""
    out, usage, total = {}, {}, {}
    for para in root.iter():
        if para.tag != "p" or "ltx_p" not in para.classes:
            continue
        pieces, marks = [], []

        def walk(n):
            if n.tag == "math":
                pieces.append(" %s " % (n.attrs.get("alttext") or ""))
                return
            if n.tag == "a":
                m = BIB_HREF.search(n.attrs.get("href") or "")
                if m:
                    marks.append((sum(len(x) for x in pieces), m.group(1)))
            for k in n.kids:
                if isinstance(k, Node):
                    if k.tag not in ("script", "style", "annotation", "annotation-xml"):
                        walk(k)
                else:
                    pieces.append(k)
        walk(para)
        if not marks:
            continue
        text = "".join(pieces)
        bounds = [0] + [m.end() for m in SENTENCE_END.finditer(text)] + [len(text)]
        for pos, bid in marks:
            lo = max(b for b in bounds if b <= pos)
            hi = min((b for b in bounds if b > pos), default=len(text))
            sentence = re.sub(r"\s+", " ", text[lo:hi]).strip()[:400]
            lst = out.setdefault(bid, [])
            if sentence and sentence not in lst and len(lst) < per_bib:
                lst.append(sentence)
            if USE_BEFORE_CITE.search(text[max(lo, pos - 50):pos]):
                usage[bid] = usage.get(bid, 0) + 1
            total[bid] = total.get(bid, 0) + 1
    return out, {bid: usage.get(bid, 0) / n for bid, n in total.items()}


def digest_html(html_text: str) -> dict:
    root = parse_html(html_text)
    bib, sections, tables = {}, [], []
    for n in root.iter():
        cls = n.classes
        if n.tag == "li" and "ltx_bibitem" in cls and n.attrs.get("id"):
            blocks = [k.text() for k in n.iter() if k is not n and k.tag == "span" and "ltx_bibblock" in k.classes]
            links = [k.attrs["href"] for k in n.iter() if k.tag == "a" and (k.attrs.get("href") or "").startswith("http")]
            bib[n.attrs["id"]] = {"text": n.text()[:600], "blocks": [b[:300] for b in blocks[:4]], "links": links[:4]}
        elif n.tag == "section" and ("ltx_section" in cls or "ltx_appendix" in cls):
            heading = next((k for k in n.iter() if k is not n and k.tag in ("h2", "h3") and "ltx_title" in k.classes), None)
            title = heading.text() if heading else (n.attrs.get("id") or "")
            sections.append({"id": n.attrs.get("id"), "title": title[:120], "kind": section_kind(title),
                             "appendix": "ltx_appendix" in cls, "bib": cited_bibs(n), "_node": n})
        elif n.tag == "figure" and "ltx_table" in cls:
            cap_node = next((k for k in n.iter() if k.tag == "figcaption"), None)
            caption = cap_node.text() if cap_node else ""
            rows, header_bibs, row_bibs, labels, groups = [], [], [], [], []
            all_rows = [r for r in n.iter() if r.tag == "tr" or "ltx_tr" in r.classes]
            for i, r in enumerate(all_rows):
                cells = [c for c in r.kids if isinstance(c, Node) and (c.tag in ("td", "th") or "ltx_td" in c.classes)]
                in_head = r.parent is not None and (r.parent.tag == "thead" or "ltx_thead" in r.parent.classes)
                is_header = in_head or (i == 0 and len(all_rows) >= 3) or (cells and all(c.tag == "th" for c in cells))
                if not is_header:
                    # Many papers name compared methods in a row without citing them ("LoRA", "Prefix"). Spanning
                    # cells are group labels such as the backbone ("LLaMA-7B" over six rows), so skip them.
                    for c in cells[:3]:
                        txt = c.text().strip()
                        if (c.attrs.get("rowspan") or "1") != "1":
                            if txt and len(groups) < 40:
                                groups.append(txt[:60])
                            continue
                        if not txt:
                            continue
                        if not re.fullmatch(r"[-–—\d.,%±\s()/x×*†‡]+", txt) and len(labels) < 80:
                            labels.append(txt[:60])
                        break
                bibs = cited_bibs(r)
                if not bibs:
                    continue
                if is_header:
                    header_bibs += [b for b in bibs if b not in header_bibs]
                else:
                    first = cited_bibs(cells[0]) if cells else bibs
                    row_bibs += [b for b in (first or bibs) if b not in row_bibs]
                label = cells[0].text() if cells else r.text()
                rows.append({"label": label[:90], "bib": bibs, "header": bool(is_header)})
            tables.append({"id": n.attrs.get("id"), "caption": caption[:400], "kind": table_kind(caption),
                           "bib": cited_bibs(n), "header_bib": header_bibs, "row_bib": row_bibs, "rows": rows[:60],
                           "labels": labels, "groups": groups})
    # Section text for the agent (related work + experiments), without the DOM nodes.
    for sec in sections:
        node = sec.pop("_node")
        if sec["kind"] in ("related_work",) and not sec["appendix"]:
            sec["text"] = node.text()[:6000]
    abstract = next((n.text() for n in root.iter() if "ltx_abstract" in n.classes), "")
    title = next((n.text() for n in root.iter() if n.tag == "h1" and "ltx_title_document" in n.classes), "")
    contexts, usage = cite_contexts(root)
    return {"title": title, "abstract": abstract[:3000], "sections": sections, "tables": tables, "bib": bib,
            "contexts": contexts, "usage": usage, "fulltext": text_with_breaks(root)}


def map_bib_to_refs(digest: dict, refs: list) -> None:
    """Attach each bibliography entry to the reference whose title appears in it (keeps existing matches)."""
    taken = {bid for bid, entry in digest["bib"].items() if "ref" in entry}
    mapped = {entry["ref"] for entry in digest["bib"].values() if "ref" in entry}
    bib_norm = {bid: " %s " % norm(entry["text"]) for bid, entry in digest["bib"].items() if bid not in taken}
    bib_toks = {bid: set(text.split()) for bid, text in bib_norm.items()}
    titles = []
    for idx, edge in enumerate(refs):
        raw = (edge.get("citedPaper") or {}).get("title") or ""
        if idx in mapped:
            continue
        for variant in {raw, raw.split(":", 1)[1] if ":" in raw else ""}:  # "ViP-LLaVA: Making ..." -> "Making ..."
            title = norm(variant)
            if len(title) >= 10 and (variant == raw or len(title.split()) >= 4):
                titles.append((len(title), idx, title))
    titles.sort(reverse=True)  # longest titles first so short generic titles cannot steal entries
    for _, idx, title in titles:
        if idx in mapped:
            continue
        tokens = set(title.split())
        for bid, text in bib_norm.items():
            if bid in taken:
                continue
            hit = (" %s " % title) in text
            if not hit and len(tokens) >= 4:  # tolerate a typo or two in long titles ("Enhacing")
                hit = len(tokens & bib_toks[bid]) / len(tokens) >= (0.85 if len(tokens) >= 6 else 0.9)
            if hit:
                digest["bib"][bid]["ref"] = idx
                taken.add(bid)
                mapped.add(idx)
                break


ARXIV_IN_TEXT = re.compile(r"(?i)(?:arxiv[:\s/]*(?:abs/)?|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})")
DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)")


def bib_title(entry: dict) -> str | None:
    """LaTeXML splits a bibliography entry into blocks: authors, title, venue. Returns the title block if plausible."""
    blocks = [b.strip() for b in entry.get("blocks") or [] if b.strip()]
    if len(blocks) >= 2:
        title = blocks[1].strip().rstrip(".").strip().strip('"“”')
        if 3 <= len(title.split()) <= 40:
            return title
    return None


def arxiv_title_search(http: Http, title: str) -> str | None:
    words = norm(title).split()[:12]
    if len(words) < 3:
        return None
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": 'ti:"%s"' % " ".join(words), "max_results": 5})
    try:
        xml_text = http.fetch(url, as_json=False, ttl_days=60)
        root = ET.fromstring(xml_text) if xml_text else None
    except (HttpFailure, ET.ParseError) as err:
        log("  arXiv title search failed: %s" % err)
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in (root.findall("a:entry", ns) if root is not None else []):
        m = re.search(r"abs/(.+?)(v\d+)?$", entry.findtext("a:id", default="", namespaces=ns))
        if m and title_close(entry.findtext("a:title", default="", namespaces=ns), title):
            return m.group(1)
    return None


def recover_refs_from_bib(http: Http, digest: dict, refs: list, max_titles: int, only: set | None = None) -> list:
    """Resolve bibliography entries missing from Semantic Scholar's reference list. Returns the new reference edges
    (to be appended to `refs`) and records each entry's index in digest['bib'][id]['ref']."""
    map_bib_to_refs(digest, refs)
    have = {(e.get("citedPaper") or {}).get("paperId") for e in refs}
    in_tables = {b for t in digest["tables"] if t["kind"] != "ignore" for b in t["bib"]}
    in_related = {b for s in digest["sections"] if s["kind"] == "related_work" for b in s["bib"]}
    labels = {alnum(lab) for t in digest["tables"] if t["kind"] != "ignore" for lab in t.get("labels") or []}
    compared = {b for b, ctx in (digest.get("contexts") or {}).items() if any(COMPARE_RE.search(c) for c in ctx)}

    def named_in_table(bid: str) -> bool:
        name = alnum(short_name(bib_title(digest["bib"][bid]) or "") or "")
        return len(name) >= 3 and any(lab == name or (len(lab) >= 4 and name.startswith(lab)) for lab in labels)

    def unlinked(entry: dict) -> bool:  # no reference at all, or one Semantic Scholar knows only by its title
        idx = entry.get("ref")
        return idx is None or (idx < len(refs) and not (refs[idx].get("citedPaper") or {}).get("paperId"))

    todo = [bid for bid, entry in digest["bib"].items() if unlinked(entry) and (only is None or bid in only)]
    todo.sort(key=lambda b: (b not in in_tables and not named_in_table(b), b not in compared, b not in in_related))
    new, linked = [], []

    index_of = {(e.get("citedPaper") or {}).get("paperId"): i for i, e in enumerate(refs)}

    def push(bid: str, paper: dict | None) -> None:
        if paper and paper.get("paperId") in index_of and digest["bib"][bid].get("ref") is None:
            digest["bib"][bid]["ref"] = index_of[paper["paperId"]]  # known paper, bibliography just titled it differently
            linked.append(bid)
            return
        if paper and paper.get("paperId") and paper["paperId"] not in have:
            have.add(paper["paperId"])
            idx = digest["bib"][bid].get("ref")
            if idx is not None and idx < len(refs):  # repair the title-only reference in place
                refs[idx]["citedPaper"] = dict(refs[idx].get("citedPaper") or {}, **paper)
                refs[idx]["source"] = "bibliography"
                linked.append(bid)
                return
            new.append({"citedPaper": paper, "contexts": [], "intents": [], "isInfluential": False,
                        "source": "bibliography"})
            digest["bib"][bid]["ref"] = len(refs) + len(new) - 1

    ids = {}
    for bid in todo:
        entry = digest["bib"][bid]
        text = entry["text"] + " " + " ".join(entry.get("links") or [])
        m = ARXIV_IN_TEXT.search(text)
        d = DOI_IN_TEXT.search(text)
        if m:
            ids[bid] = "ARXIV:" + m.group(1)
        elif d:
            doi = d.group(1).rstrip(".,;)]")
            am = re.match(r"(?i)10\.48550/arxiv\.(.+)$", doi)
            ids[bid] = ("ARXIV:" + am.group(1)) if am else ("DOI:" + doi)
    found = s2_batch(http, list(ids.values()), fields=PAPER_FIELDS)
    for bid, sid in ids.items():
        push(bid, found.get(sid))
    def title_of(bid: str) -> str | None:
        idx = digest["bib"][bid].get("ref")
        known = (refs[idx].get("citedPaper") or {}).get("title") if idx is not None and idx < len(refs) else None
        return bib_title(digest["bib"][bid]) or known

    titled = [(bid, title_of(bid)) for bid in todo if bid not in ids]
    titled = [(bid, t) for bid, t in titled if t][:max_titles]
    if titled:
        log("  looking up %d bibliography titles (arXiv, then Semantic Scholar) ..." % len(titled))
    via_arxiv = {}
    for bid, title in titled:
        aid = arxiv_title_search(http, title)
        if aid:
            via_arxiv[bid] = "ARXIV:" + aid
    found = s2_batch(http, list(via_arxiv.values()), fields=PAPER_FIELDS)
    for bid, sid in via_arxiv.items():
        push(bid, found.get(sid))
    for bid, title in [(b, t) for b, t in titled if b not in via_arxiv][:max(3, max_titles // 3)]:
        p = s2_match(http, title)
        if p and title_close(p.get("title"), title):
            push(bid, p)
    if linked:
        log("  linked %d bibliography entries to references Semantic Scholar had under another title or no id"
            % len(linked))
    return new


def fetch_fulltext(http: Http, seed: dict, workdir: str) -> tuple:
    """Returns (source_url, html_text_or_None, plain_text_or_None, pdf_path_or_None)."""
    arxiv = (seed.get("externalIds") or {}).get("ArXiv")
    if arxiv:
        for url in ("https://arxiv.org/html/%s" % arxiv, "https://ar5iv.labs.arxiv.org/html/%s" % arxiv):
            try:
                page = http.fetch(url, as_json=False, ttl_days=60)
            except HttpFailure as err:
                log("  %s: %s" % (url, err))
                continue
            if page and ("ltx_page_main" in page or "ltx_document" in page):
                return url, page, None, None
    pdf_url = ((seed.get("openAccessPdf") or {}).get("url")) or (("https://arxiv.org/pdf/%s" % arxiv) if arxiv else None)
    if pdf_url:
        pdf = os.path.join(workdir, "paper.pdf")
        if http.download(pdf_url, pdf):
            if shutil.which("pdftotext"):
                out = os.path.join(workdir, "fulltext.txt")
                subprocess.run(["pdftotext", "-layout", pdf, out], check=False)
                if os.path.exists(out):
                    with open(out, encoding="utf-8", errors="replace") as fh:
                        return pdf_url, None, fh.read(), pdf
            return pdf_url, None, None, pdf
    return pdf_url, None, None, None


# ----------------------------------------------------------------------------- core proposal

# Strong cues that a cited work is compared against; "SOTA"/"competitive" alone often describe a component in use.
COMPARE_RE = re.compile(r"(\b(compar\w*|outperform\w*|baselines?|surpass\w*|versus|superior to|better than|worse than|"
                        r"beats?|in contrast to|unlike)\b|\bvs\.)", re.I)
WEAK_COMPARE_RE = re.compile(r"\b(state[- ]of[- ]the[- ]art|sota|competitive|prior (work|methods?)|"
                             r"existing (methods?|approaches|datasets?|benchmarks?))\b", re.I)
USE_RE = re.compile(r"\b(we (use|used|adopt|adopted|follow|followed|employ|employed|leverage|utili[sz]e|build on|"
                    r"borrow|initiali[sz]e|apply)|following|as (the|our|a) (backbone|detector|encoder|decoder|"
                    r"generator|initiali[sz]ation|teacher|tokenizer)|implemented (with|in)|pre-?trained on|"
                    r"(code|implementation|checkpoint|weights) (of|from))\b", re.I)
DATA_TITLE_RE = re.compile(r"\b(dataset|data set|database|benchmark|corpus|corpora|challenge|suite|leaderboard|"
                           r"shared task|competition|collection|treebank|testbed|arena)\b", re.I)
DATA_CTX_RE = re.compile(r"\b(dataset|data set|benchmark|corpus|corpora|split|test set|leaderboard)\b", re.I)


def guess_type(title: str, abstract: str) -> str:
    t, a = (title or "").lower(), (abstract or "").lower()
    lead = r"\bwe (introduce|present|propose|release|construct|build|collect|curate|create)\s+(a |an |the )?([\w-]+ ){0,4}"
    bench = re.search(r"\bbenchmark|leaderboard|evaluation suite|\barena\b", t) or re.search(lead + r"benchmark", a)
    data = re.search(r"\bdataset|\bcorpus|\bdata set", t) or re.search(lead + r"(dataset|corpus)", a)
    method = re.search(r"\bwe (propose|introduce|present)\s+(a |an )?([\w-]+ ){0,3}(method|approach|framework|model|"
                       r"architecture|technique|algorithm)", a)
    # "We benchmark modern models ..." in a paper that introduces a dataset makes it a dataset + benchmark paper.
    evaluates = re.search(r"\bwe (benchmark|evaluate|assess|compare)\s+([\w-]+ ){0,4}(models|methods|approaches|"
                          r"baselines|systems|algorithms)\b", a)
    kinds = [k for k, hit in (("benchmark", bench), ("dataset", data)) if hit]
    if (kinds and method) or len(kinds) == 2 or (data and evaluates):
        return "mixed"
    return kinds[0] if kinds else "method"


def role_for(paper_type: str, is_data: bool, compared_table: bool, title: str) -> str:
    """Role of a reference that appears in a comparison/results table or a 'compared with' context."""
    benchmark_like = bool(re.search(r"benchmark|suite|leaderboard|arena|evaluation", title, re.I))
    if is_data:
        if paper_type == "method":
            return "eval_dataset"
        if paper_type == "benchmark":
            return "compared_benchmark" if benchmark_like or compared_table else "compared_dataset"
        return "compared_dataset"
    if paper_type in ("benchmark", "dataset", "mixed"):
        return "benchmarked_model"
    return "baseline"


def stem(word: str) -> str:
    """Crude suffix stripping so 'segmentation', 'segments' and 'segmenting' all become 'segment'."""
    for suffix, repl in (("ations", ""), ("ation", ""), ("ings", ""), ("ing", ""), ("ies", "y"), ("es", ""),
                         ("ed", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)] + repl
    return word


def content_terms(text: str | None) -> set:
    return {stem(t) for t in norm(text).split() if len(t) >= 3 and t not in BASIC_STOPWORDS}


def topic_overlap(title: str | None, seed_terms: set) -> float:
    """Share of a reference title's content words that also occur in the seed's title + abstract."""
    toks = content_terms(title)
    return len(toks & seed_terms) / len(toks) if toks else 0.0


def match_label(label: str, names: dict, variants: bool = False) -> list:
    """Reference indices whose short name matches a table label: "LoRA", or "Prefix" for "Prefix-Tuning", and with
    `variants` also "LLaMA-7B" for "LLaMA" (group cells name model sizes)."""
    lab = alnum(re.sub(r"\\text\{([^}]*)\}|\^\{[^}]*\}|\(.*?\)|\[.*?\]|[†‡*]", r"\1", label))
    if len(lab) < 3:
        return []
    out = []
    for name, idxs in names.items():
        if (lab == name or (len(lab) >= 4 and name.startswith(lab) and len(name) - len(lab) <= 8)
                or (variants and len(name) >= 4 and lab.startswith(name))):
            out += idxs
    return out


def propose_core(refs: list, digest: dict | None, paper_type: str, cfg: dict, seed_text: str = "") -> list:
    """Assign each reference a role from where and how the seed cites it, and mark a default core set.

    Evidence, strongest first: a row of a results / dataset-comparison table; a table header (columns are usually
    datasets); a citation sentence with a comparison cue; the related-work section. Table captions and "we use /
    adopt / follow X" sentences mostly cite components (backbones, detectors, eval data), so they are weak evidence.
    """
    seed_terms = content_terms(seed_text)
    ctx_from_html = {}
    if digest:
        for bid, entry in digest["bib"].items():
            if "ref" in entry and (digest.get("contexts") or {}).get(bid):
                ctx_from_html.setdefault(entry["ref"], []).extend(digest["contexts"][bid])
    info = {}
    for idx, edge in enumerate(refs):
        p = edge.get("citedPaper") or {}
        if len(norm(p.get("title")).split()) < 2:  # no title, or a junk resolution such as "Models"
            continue
        contexts = edge.get("contexts") or ctx_from_html.get(idx) or []
        used = [c for c in contexts if USE_RE.search(c)]
        strong = [c for c in contexts if COMPARE_RE.search(c) and not USE_RE.search(c)]
        data_ctx = sum(1 for c in contexts if DATA_CTX_RE.search(c))
        info[idx] = {
            "paperId": p.get("paperId"), "title": p.get("title"), "year": p.get("year"),
            "citationCount": p.get("citationCount") or 0, "externalIds": p.get("externalIds") or {},
            "publicationDate": p.get("publicationDate"), "venue": p.get("venue"),
            "intents": edge.get("intents") or [], "isInfluential": bool(edge.get("isInfluential")),
            "n_contexts": len(contexts), "compare_contexts": strong[:2], "use_contexts": used[:1],
            "weak_compare": any(WEAK_COMPARE_RE.search(c) for c in contexts),
            "data_ctx": bool(contexts) and data_ctx * 2 >= len(contexts),
            "tables": [], "sections": set(), "overlap": topic_overlap(p.get("title"), seed_terms),
        }
    if digest:
        names = {}  # alnum short name -> reference indices ("lora" -> LoRA: Low-Rank Adaptation ...)
        for idx, r in info.items():
            name = alnum(short_name(r["title"]) or "")
            if len(name) >= 3:
                names.setdefault(name, []).append(idx)
        for table in digest.get("tables", []):
            if table["kind"] == "ignore":
                continue
            heads, rows_ = set(table.get("header_bib") or []), set(table.get("row_bib") or [])
            for bid in table["bib"]:
                idx = digest["bib"].get(bid, {}).get("ref")
                if idx in info:
                    pos = "header" if bid in heads else ("row" if bid in rows_ else "caption")
                    info[idx]["tables"].append((table["id"] or "table", table["kind"], pos))
            for lab in table.get("labels") or []:
                for idx in match_label(lab, names):
                    if not any(t == (table["id"] or "table") and p in ("row", "row-label") for t, _, p in info[idx]["tables"]):
                        info[idx]["tables"].append((table["id"] or "table", table["kind"], "row-label"))
            for lab in table.get("groups") or []:  # spanning group cells name the backbone / setting
                for idx in match_label(lab, names, variants=True):
                    info[idx]["group_cell"] = True
        for bid, share in (digest.get("usage") or {}).items():
            idx = digest["bib"].get(bid, {}).get("ref")
            if idx in info and share >= 0.5:
                info[idx]["html_used"] = True
        for sec in digest.get("sections", []):
            for bid in sec["bib"]:
                idx = digest["bib"].get(bid, {}).get("ref")
                if idx in info:
                    info[idx]["sections"].add(sec["kind"])
    rows = []
    for idx, r in info.items():
        tables = r["tables"]
        in_rows = any(pos in ("row", "row-label") for _, k, pos in tables)
        in_headers = any(pos == "header" for _, k, pos in tables)
        in_compare_rows = any(k == "dataset_comparison" and pos in ("row", "row-label") for _, k, pos in tables)
        data_title = bool(DATA_TITLE_RE.search(r["title"]))
        if in_compare_rows or (in_headers and not in_rows):
            is_data = True
        elif in_rows:  # rows of results tables are models unless the title says otherwise (SAM also released SA-1B)
            is_data = data_title
        else:
            is_data = data_title or r["data_ctx"]
        mostly_used = ((bool(r["use_contexts"]) and not r["compare_contexts"]) or r.get("html_used", False)
                       or (r.get("group_cell", False) and not in_rows))
        evidence = []
        if tables:
            evidence.append("in " + ", ".join(sorted({"%s(%s)" % (t, pos) for t, _, pos in tables})))
        if r["compare_contexts"]:
            evidence.append('compared: "%s"' % re.sub(r"\s+", " ", r["compare_contexts"][0])[:140])
        elif r["use_contexts"]:
            evidence.append('used: "%s"' % re.sub(r"\s+", " ", r["use_contexts"][0])[:140])
        if "related_work" in r["sections"]:
            evidence.append("cited in related work")
        if mostly_used and not in_rows:
            evidence.append("looks like a component (backbone / data / tool)")
        role = "background"
        if in_rows or in_compare_rows:
            role = role_for(paper_type, is_data, in_compare_rows, r["title"])
        elif in_headers:
            role = role_for(paper_type, True, False, r["title"])
        elif tables:  # only in captions: evaluation data or a component (backbone, detector, prompt source)
            role = "eval_dataset" if is_data else "background"
        elif r["compare_contexts"] and not mostly_used and ("experiments" in r["sections"] or not digest):
            role = role_for(paper_type, is_data, False, r["title"])
        elif "related_work" in r["sections"] or (r["compare_contexts"] and not mostly_used):
            role = "related"
            if paper_type in ("dataset", "benchmark", "mixed") and DATA_TITLE_RE.search(r["title"]):
                # A dataset/benchmark paper's related work reviews the datasets it positions itself against.
                role = role_for(paper_type, True, False, r["title"])
        elif not digest and ("methodology" in r["intents"] or r["isInfluential"]):
            role = "related"
        score = (3 * in_rows + 1.5 * in_headers + 0.5 * bool(tables) + 2 * bool(r["compare_contexts"])
                 + 0.5 * r["weak_compare"] + 1.5 * ("related_work" in r["sections"]) + 1 * r["isInfluential"]
                 + 0.5 * ("methodology" in r["intents"]) + 0.3 * min(r["n_contexts"], 5) + 4 * r["overlap"]
                 - 1 * mostly_used + 0.2 * math.log10(1 + r["citationCount"]))
        rows.append({
            "id": short_id(r["paperId"]) or "ref%d" % idx, "paperId": r["paperId"], "title": r["title"],
            "year": r["year"], "venue": r["venue"], "citationCount": r["citationCount"],
            "publicationDate": r["publicationDate"], "externalIds": r["externalIds"],
            "role": role, "score": round(score, 2), "overlap": round(r["overlap"], 2), "evidence": evidence,
            "include": False,
        })
    # Default inclusion: the explicit comparison set, plus the top related-work papers.
    by_role = {}
    for row in sorted(rows, key=lambda x: (-x["score"], -x["citationCount"])):
        by_role.setdefault(row["role"], []).append(row)
    wanted = {
        "method": {"baseline": 99, "related": 12, "compared_dataset": 99, "compared_benchmark": 99},
        # A dataset paper's lineage is the datasets it compares against; the models it evaluates drive the forward
        # search only for benchmark / mixed papers (they are still listed as backward papers either way).
        "dataset": {"compared_dataset": 99, "compared_benchmark": 99, "related": 12},
        "benchmark": {"compared_benchmark": 99, "compared_dataset": 99, "benchmarked_model": 15, "related": 10},
        "mixed": {"baseline": 99, "compared_dataset": 99, "compared_benchmark": 99, "benchmarked_model": 15, "related": 10},
    }[paper_type]
    for role, n in wanted.items():
        pool = by_role.get(role, [])
        if role == "related":  # skip off-topic hubs (GPT-3, CLIP, ...) that are merely cited in passing
            pool = [r for r in pool if r["overlap"] >= 0.2 or any(e.startswith("compared:") for e in r["evidence"])]
        for row in pool[:n]:
            if row["paperId"]:
                row["include"] = True
    included = [r for r in rows if r["include"]]
    if len(included) > cfg["max_core"]:
        priority = {"baseline": 0, "compared_dataset": 0, "compared_benchmark": 0, "benchmarked_model": 1, "related": 2}
        included.sort(key=lambda x: (priority.get(x["role"], 3), -x["score"]))
        for row in included[cfg["max_core"]:]:
            row["include"] = False
    order = {"baseline": 0, "benchmarked_model": 1, "compared_dataset": 2, "compared_benchmark": 3, "related": 4,
             "eval_dataset": 5, "background": 6}
    rows.sort(key=lambda x: (not x["include"], order.get(x["role"], 9), -x["score"]))
    return rows


# ----------------------------------------------------------------------------- code discovery

GH_RE = re.compile(r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.\-]{1,100})", re.I)
GH_SKIP = {"orgs", "topics", "features", "about", "sponsors", "marketplace", "settings", "login", "join", "explore",
           "collections", "trending", "site", "apps", "users", "issues", "pulls", "search", "notifications"}


def github_links(text: str | None) -> list:
    out = []
    for owner, repo in GH_RE.findall((text or "").replace("\\_", "_")):
        repo = re.sub(r"\.git$", "", repo).rstrip(".-_")
        if owner.lower() in GH_SKIP or not repo or repo.lower() in ("blob", "tree"):
            continue
        full = "%s/%s" % (owner, repo)
        if full.lower() not in (o.lower() for o in out):
            out.append(full)
    return out


def arxiv_meta(http: Http, ids: list) -> dict:
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        url = "https://export.arxiv.org/api/query?id_list=%s&max_results=%d" % (",".join(chunk), len(chunk))
        try:
            xml_text = http.fetch(url, as_json=False, ttl_days=30)
            root = ET.fromstring(xml_text) if xml_text else None
        except (HttpFailure, ET.ParseError) as err:
            log("  arXiv metadata lookup failed: %s" % err)
            continue
        for entry in (root.findall("a:entry", ns) if root is not None else []):
            m = re.search(r"abs/(.+?)(v\d+)?$", entry.findtext("a:id", default="", namespaces=ns))
            if not m:
                continue
            out[m.group(1)] = {
                "title": re.sub(r"\s+", " ", entry.findtext("a:title", default="", namespaces=ns)).strip(),
                "comment": entry.findtext("x:comment", default="", namespaces=ns) or "",
                "summary": entry.findtext("a:summary", default="", namespaces=ns) or "",
            }
    return out


def github_token() -> str | None:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    if shutil.which("gh"):
        try:
            res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def github_repo_info(http: Http, repos: list, token: str | None) -> dict:
    info, uniq = {}, sorted({r.lower(): r for r in repos}.values())
    if token:
        for start in range(0, len(uniq), 40):
            chunk = uniq[start:start + 40]
            parts = []
            for j, full in enumerate(chunk):
                owner, name = full.split("/", 1)
                parts.append("r%d: repository(owner: %s, name: %s) { nameWithOwner stargazerCount url isArchived "
                             "pushedAt description homepageUrl }" % (j, json.dumps(owner), json.dumps(name)))
            try:
                res = http.fetch("https://api.github.com/graphql", data={"query": "query { %s }" % " ".join(parts)},
                                 headers={"Authorization": "bearer " + token}, ttl_days=3)
            except HttpFailure as err:
                log("  GitHub GraphQL failed: %s" % err)
                res = None
            data = (res or {}).get("data") or {}
            for j, full in enumerate(chunk):
                r = data.get("r%d" % j)
                if r:
                    info[full.lower()] = {"repo": r["nameWithOwner"], "url": r["url"], "stars": r["stargazerCount"],
                                          "archived": r["isArchived"], "pushed": (r.get("pushedAt") or "")[:10],
                                          "description": (r.get("description") or "")[:200],
                                          "homepage": r.get("homepageUrl") or ""}
        return info
    for full in uniq[:55]:  # unauthenticated REST allows 60 calls/hour
        try:
            r = http.fetch("https://api.github.com/repos/%s" % full, ttl_days=3)
        except HttpFailure as err:
            log("  GitHub REST failed for %s: %s" % (full, err))
            break
        if r:
            info[full.lower()] = {"repo": r["full_name"], "url": r["html_url"], "stars": r["stargazers_count"],
                                  "archived": r.get("archived", False), "pushed": (r.get("pushed_at") or "")[:10],
                                  "description": (r.get("description") or "")[:200]}
    if len(uniq) > 55:
        log("  (no GitHub token: star counts fetched for the first 55 repos only; run `gh auth login` for more)")
    return info


def hf_artifacts(http: Http, arxiv_id: str, title: str, gh_owners: set) -> list:
    """Hugging Face models/datasets/spaces tagged with this arXiv id that plausibly belong to the paper."""
    name = alnum(short_name(title) or "")
    found = []
    for kind in ("models", "datasets", "spaces"):
        try:
            rows = http.fetch("https://huggingface.co/api/%s?filter=arxiv:%s&sort=likes&direction=-1&limit=5"
                              % (kind, arxiv_id), ttl_days=14) or []
        except HttpFailure as err:
            log("  HF %s lookup failed for %s: %s" % (kind, arxiv_id, err))
            continue
        for row in rows:
            rid = row.get("id") or row.get("modelId") or ""
            owner = rid.split("/")[0].lower()
            by_owner = owner in gh_owners  # same owner as the paper's verified GitHub repo
            if by_owner or (len(name) >= 3 and name in alnum(rid)):
                prefix = {"models": "", "datasets": "datasets/", "spaces": "spaces/"}[kind]
                found.append({"kind": kind[:-1], "id": rid, "likes": row.get("likes") or 0, "verified": by_owner,
                              "url": "https://huggingface.co/%s%s" % (prefix, rid)})
                break  # best (most liked) plausible artifact of this kind
    found.sort(key=lambda a: -a["likes"])
    return found


def hf_paper(http: Http, arxiv_id: str) -> dict:
    try:
        return http.fetch("https://huggingface.co/api/papers/%s" % arxiv_id, ttl_days=14) or {}
    except HttpFailure:
        return {}


LIST_REPO_RE = re.compile(r"awesome|papers?\b|paper-?list|reading|survey|curated|collection|must-?read|resources|"
                          r"tutorial|course|notes", re.I)


def repo_title_overlap(item: dict, title: str) -> float:
    """Share of the paper title's content words found in a repo's name + description (e.g. segment-anything)."""
    terms = content_terms(title)
    repo_terms = content_terms("%s %s" % (re.sub(r"[-_.]", " ", item.get("name") or item.get("repo", "").split("/")[-1]),
                                          item.get("description") or ""))
    return len(terms & repo_terms) / len(terms) if terms else 0.0


def repo_points_to_paper(item: dict, title: str, arxiv_id: str | None) -> bool:
    """Strong evidence a found repo is the paper's own: its homepage is the paper, or its description is the title."""
    home = (item.get("homepage") or item.get("homepageUrl") or "").lower()
    if arxiv_id and arxiv_id.lower() in home:
        return True
    return repo_title_overlap(item, title) >= 0.8


def github_search(http: Http, title: str, token: str | None, arxiv_id: str | None = None) -> dict | None:
    """Fallback for papers without a linked repo. First a repo whose name carries the paper's short name ("LoRA"),
    then a repo whose README contains the exact title and whose name/description matches it (lists are skipped)."""
    if not token:
        return None
    hit = github_name_search(http, title, token, arxiv_id)
    if hit:
        return hit
    if len(norm(title).split()) < 2:
        return None
    try:
        res = http.fetch("https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": '"%s" in:readme' % re.sub(r'["\s]+', " ", title).strip(), "sort": "stars", "order": "desc",
             "per_page": 10}), headers={"Authorization": "bearer " + token}, ttl_days=7)
    except HttpFailure as err:
        log("  GitHub search failed: %s" % err)
        return None
    name = alnum(short_name(title) or "")
    for item in (res or {}).get("items") or []:
        if LIST_REPO_RE.search("%s %s" % (item.get("name"), item.get("description") or "")):
            continue
        overlap = repo_title_overlap(item, title)
        named = len(name) >= 3 and name in alnum(item.get("name"))
        if overlap >= 0.6 or named:
            strong = named or repo_points_to_paper(item, title, arxiv_id)
            return {"repo": item["full_name"], "url": item["html_url"], "stars": item["stargazers_count"],
                    "archived": item.get("archived", False), "pushed": (item.get("pushed_at") or "")[:10],
                    "description": (item.get("description") or "")[:200],
                    "source": "github-search" if strong else "github-search (verify)"}
    return None


def github_name_search(http: Http, title: str, token: str, arxiv_id: str | None = None) -> dict | None:
    name = short_name(title)
    if not name or len(alnum(name)) < 3:
        return None
    q = "%s in:name,description" % name
    try:
        res = http.fetch("https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": q, "sort": "stars", "order": "desc", "per_page": 5}),
            headers={"Authorization": "bearer " + token}, ttl_days=7)
    except HttpFailure as err:
        log("  GitHub search failed: %s" % err)
        return None
    title_toks = set(keywords_from(title))
    for item in (res or {}).get("items") or []:
        desc_toks = set(keywords_from((item.get("description") or "") + " " + item.get("name", "")))
        overlap = len(title_toks & desc_toks) / max(1, len(title_toks))
        if alnum(name) in alnum(item.get("name")) and overlap >= 0.35:
            strong = repo_points_to_paper(item, title, arxiv_id)
            return {"repo": item["full_name"], "url": item["html_url"], "stars": item["stargazers_count"],
                    "archived": item.get("archived", False), "pushed": (item.get("pushed_at") or "")[:10],
                    "description": (item.get("description") or "")[:200],
                    "source": "github-search" if strong else "github-search (verify)"}
    return None


def find_code(http: Http, papers: list, use_search: bool = True) -> None:
    """Adds p['code'] = {'github': [...], 'hf': [...]} to each paper dict (in place).

    Repo sources, most trusted first: the repo linked on the paper's Hugging Face page, links in the arXiv comment or
    abstract (which can also name a dependency, hence the order), then a GitHub name search flagged as unverified.
    """
    token = github_token()
    arxiv_ids = [(p.get("externalIds") or {}).get("ArXiv") for p in papers]
    meta = arxiv_meta(http, [a for a in arxiv_ids if a])
    candidates, hf_stars, hf_auto = {}, {}, set()
    for p, aid in zip(papers, arxiv_ids):
        ranked = []  # (priority, repo)
        if aid:
            hp = hf_paper(http, aid)
            for repo in github_links(hp.get("githubRepo") if isinstance(hp.get("githubRepo"), str) else ""):
                # Repos the authors linked are trusted; auto-detected ones are checked against the title below.
                ranked.append((0 if hp.get("githubRepoAddedBy") != "auto" else 1, repo))
                if hp.get("githubRepoAddedBy") == "auto":
                    hf_auto.add(repo.lower())
                if isinstance(hp.get("githubStars"), int):
                    hf_stars[repo.lower()] = hp["githubStars"]
            for text in (meta.get(aid, {}).get("comment"), meta.get(aid, {}).get("summary"), hp.get("summary")):
                ranked += [(1, r) for r in github_links(text)]
        ranked += [(2, r) for r in github_links(p.get("abstract"))]
        best = {}
        for prio, repo in ranked:
            if repo.lower() not in best or prio < best[repo.lower()][0]:
                best[repo.lower()] = (prio, repo)
        candidates[p.get("paperId") or p.get("title")] = sorted(best.values())[:3]
    all_repos = [r for repos in candidates.values() for _, r in repos]
    info = github_repo_info(http, all_repos, token)
    searches = 0
    for p, aid in zip(papers, arxiv_ids):
        ranked_gh = []
        for prio, r in candidates[p.get("paperId") or p.get("title")]:
            meta_r = info.get(r.lower())
            if not meta_r and r.lower() in hf_stars:  # GitHub not queried (no token / quota): use HF's star count
                meta_r = {"repo": r, "url": "https://github.com/" + r, "stars": hf_stars[r.lower()], "archived": False,
                          "pushed": "", "description": None}
            if meta_r and r.lower() in hf_auto and meta_r.get("description") is not None:
                name = alnum(short_name(p.get("title") or "") or "")
                if repo_title_overlap(meta_r, p.get("title") or "") < 0.34 and not (len(name) >= 3 and name in alnum(r)):
                    continue  # auto-detected link to an unrelated repo (one that merely cites the paper)
            if meta_r:
                source = "hf-paper-auto" if r.lower() in hf_auto else ("hf-paper", "arxiv", "abstract")[prio]
                ranked_gh.append((prio, -(meta_r["stars"] or 0), dict(meta_r, source=source)))
        gh = [g for _, _, g in sorted(ranked_gh, key=lambda t: t[:2])]
        if not gh and use_search and searches < 40:
            hit = github_search(http, p.get("title") or "", token, aid)
            searches += 1
            if hit:
                gh.append(hit)
        owners = {g["repo"].split("/")[0].lower() for g in gh if "verify" not in (g.get("source") or "")}
        hf = hf_artifacts(http, aid, p.get("title") or "", owners) if aid else []
        p["code"] = {"github": gh[:2], "hf": hf[:2]}


# ----------------------------------------------------------------------------- OpenAlex (optional)

class OpenAlex:
    """Optional helper for one job only: the most-cited citers of a very highly cited core paper.

    Pricing (2026): singleton GETs are free, list/filter calls cost 1 credit, searches 10 credits; keyless callers get
    1,000 credits/day (shared per IP), a free key (OPENALEX_API_KEY) 10,000. OpenAlex undercounts arXiv-heavy ML papers
    and has some bad merges, so every hit is re-resolved in Semantic Scholar and its title must match.
    """

    def __init__(self, http: Http, budget: int):
        self.http, self.budget, self.used = http, budget, 0
        self.key = os.environ.get("OPENALEX_API_KEY")

    def _get(self, path: str, billable: bool = True, **params):
        if billable and self.used >= self.budget:
            return None
        self.used += int(billable)
        headers = {"Authorization": "Bearer " + self.key} if self.key else None  # header, so it never lands in logs
        try:
            return self.http.fetch("%s/%s?%s" % (OPENALEX, path, urllib.parse.urlencode(params, safe=",:|")),
                                   headers=headers, ttl_days=7)
        except HttpFailure as err:
            log("  OpenAlex: %s" % err)
            return None

    def work_id(self, p: dict) -> str | None:
        ext = p.get("externalIds") or {}
        if ext.get("MAG"):
            w = self._get("works/mag:%s" % ext["MAG"], billable=False, select="id,display_name")
            if w and w.get("id") and norm(w.get("display_name")) == norm(p.get("title")):
                return w["id"].rsplit("/", 1)[-1]
        doi = ext.get("DOI")
        if doi and not doi.lower().startswith("10.48550/"):
            w = self._get("works/doi:%s" % doi, billable=False, select="id,display_name")
            if w and w.get("id"):
                return w["id"].rsplit("/", 1)[-1]
        if ext.get("ArXiv"):
            res = self._get("locations", filter="native_id:oai:arXiv.org:%s|10.48550/arxiv.%s" % (ext["ArXiv"], ext["ArXiv"].lower()),
                            select="id,work_id")
            for loc in (res or {}).get("results") or []:
                if loc.get("work_id"):
                    return loc["work_id"].rsplit("/", 1)[-1]
        return None

    def top_citers(self, work: str, min_cites: int = 0) -> list:
        """The 100 most-cited works citing `work` (per_page above 100 is deprecated)."""
        res = self._get("works", filter="cites:%s,cited_by_count:>%d" % (work, max(min_cites - 1, 0)),
                        sort="cited_by_count:desc", select="id,display_name,publication_year,cited_by_count,doi,ids",
                        per_page=100)
        return (res or {}).get("results") or []

    @staticmethod
    def s2_ids(work: dict) -> list:
        """Semantic Scholar ids to try for an OpenAlex work (MAG first: DOIs are sometimes wrong after bad merges)."""
        ids = work.get("ids") or {}
        out = []
        if ids.get("mag"):
            out.append("MAG:%s" % str(ids["mag"]).rsplit("/", 1)[-1])
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        if doi:
            arx = re.match(r"(?i)10\.48550/arxiv\.(.+)$", doi)
            out.append(("ARXIV:" + arx.group(1)) if arx else ("DOI:" + doi))
        return out


# ----------------------------------------------------------------------------- stages

def workdir_for(args, seed_title: str | None = None) -> str:
    if getattr(args, "workdir", None):
        return args.workdir
    if seed_title:
        return os.path.abspath("fbsearch-" + slugify(seed_title))
    raise SystemExit("--workdir is required (use the folder printed by `backward`).")


def cmd_resolve(args) -> None:
    http = Http(args.cache_dir, refresh=args.refresh)
    p = resolve(http, args.seed)
    print(json.dumps({k: p.get(k) for k in ("paperId", "title", "year", "venue", "citationCount", "externalIds", "url")}, indent=1))


def cmd_backward(args) -> None:
    http = Http(args.cache_dir, refresh=args.refresh)
    seed = resolve(http, args.seed)
    wd = workdir_for(args, seed.get("title"))
    os.makedirs(wd, exist_ok=True)
    ptype = args.type or guess_type(seed.get("title"), seed.get("abstract"))
    seed["paper_type"] = ptype
    seed["paper_type_source"] = "user/agent" if args.type else "auto-guess"
    save_json(os.path.join(wd, "seed.json"), seed)
    log("Seed: %s (%s) [%s] cites=%s" % (seed.get("title"), seed.get("year"), seed.get("paperId"), seed.get("citationCount")))

    log("Fetching references ...")
    refs = [e for e in s2_edges(http, seed["paperId"], "references", REF_FIELDS, cap=5000)
            if (e.get("citedPaper") or {}).get("title")]
    log("Fetching full text ...")
    src, html_text, plain, pdf = fetch_fulltext(http, seed, wd)
    digest, recovered = None, 0
    if html_text:
        digest = digest_html(html_text)
        with open(os.path.join(wd, "fulltext.txt"), "w", encoding="utf-8") as fh:
            fh.write(digest.pop("fulltext"))
        digest["source"] = src
        n_bib = len(digest["bib"])
        if n_bib:
            map_bib_to_refs(digest, refs)
            missing = sum(1 for b in digest["bib"].values() if "ref" not in b)
            if missing:
                log("  Semantic Scholar lists %d references; %d of the %d bibliography entries are unmatched, resolving "
                    "them ..." % (len(refs), missing, n_bib))
            new = recover_refs_from_bib(http, digest, refs, max_titles=args.max_bib_lookups)
            refs, recovered = refs + new, len(new)
            if new:
                log("  recovered %d references from the bibliography" % recovered)
        save_json(os.path.join(wd, "digest.json"), digest)
    elif plain:
        log("  No arXiv HTML; wrote fulltext.txt from the PDF (no table/section structure).")
    elif pdf:
        log("  No arXiv HTML. Saved the PDF to %s: read its comparison tables and related work yourself and fix "
            "core.json." % pdf)
    else:
        log("  Could not get the full text automatically (%s). Read the paper yourself and edit core.json." % (src or "no URL"))
    if not refs:
        log("  No references from Semantic Scholar or the bibliography. Trying Crossref ...")
        refs = crossref_refs(http, seed)
    save_json(os.path.join(wd, "refs.json"), refs)

    cfg = {"max_core": args.max_core}
    core = propose_core(refs, digest, ptype, cfg, "%s %s" % (seed.get("title") or "", seed.get("abstract") or ""))
    save_json(os.path.join(wd, "core.json"), core)
    print_backward_summary(wd, seed, digest, core, refs, recovered, pdf if not html_text else None, http)


def crossref_refs(http: Http, seed: dict) -> list:
    doi = (seed.get("externalIds") or {}).get("DOI")
    if not doi:
        return []
    try:
        work = (http.fetch("https://api.crossref.org/works/%s" % urllib.parse.quote(doi)) or {}).get("message") or {}
    except HttpFailure as err:
        log("  Crossref failed: %s" % err)
        return []
    dois = [r["DOI"] for r in work.get("reference") or [] if r.get("DOI")]
    found = s2_batch(http, ["DOI:" + d for d in dois], fields=PAPER_FIELDS)
    log("  Crossref listed %d references with DOIs; %d resolved in Semantic Scholar." % (len(dois), len(found)))
    return [{"citedPaper": p, "contexts": [], "intents": [], "isInfluential": False, "source": "crossref"}
            for p in found.values()]


def print_backward_summary(wd, seed, digest, core, refs, recovered, pdf, http) -> None:
    out = ["WORKDIR %s" % wd,
           "SEED    %s (%s, %s) cites=%s  type=%s (%s)" % (seed.get("title"), seed.get("venue") or "-", seed.get("year"),
                                                         seed.get("citationCount"), seed["paper_type"], seed["paper_type_source"]),
           "REFS    %d references (%d recovered from the bibliography)" % (len(refs), recovered)]
    if digest:
        mapped = sum(1 for b in digest["bib"].values() if "ref" in b)
        out.append("DIGEST  %s | %d sections, %d tables, %d bib entries (%d matched to references)"
                   % (digest["source"], len(digest["sections"]), len(digest["tables"]), len(digest["bib"]), mapped))
        for t in digest["tables"]:
            if t["bib"]:
                out.append("  table %-8s [%s] cites %d refs | %s" % (t["id"], t["kind"], len(t["bib"]), t["caption"][:110]))
    elif pdf:
        out.append("PDF     %s (no HTML version: table positions unknown, roles come from citation contexts only)" % pdf)
    if http.s2_busy:
        out.append("NOTE    %d retries against Semantic Scholar's busy keyless pool; an S2_API_KEY makes runs much faster."
                   % http.s2_busy)
    if http.failures:
        out.append("WARNING requests failed even after retries (%s); re-run `backward` when the network is stable."
                   % ", ".join("%s x%d" % kv for kv in sorted(http.failures.items(), key=lambda kv: -kv[1])))
    out.append("")
    out.append("PROPOSED CORE SET (include=y feeds the forward search; edit core.json to change)")
    out.append("  inc  id        role               cites   year  title  | evidence")
    shown = 0
    for row in core:
        if row["role"] == "background" and not row["include"]:
            continue
        if not row["include"]:
            shown += 1
            if shown > 25:
                continue
        out.append("  %-4s %-9s %-18s %7s  %-5s %s | %s" % (
            "y" if row["include"] else "-", row["id"], row["role"], row["citationCount"], row["year"] or "",
            (row["title"] or "")[:70], "; ".join(row["evidence"])[:150]))
    hidden = sum(1 for r in core if r["role"] == "background" and not r["include"]) + max(0, shown - 25)
    out.append("  (+%d more references not shown, mostly background; see core.json)" % hidden)
    print("\n".join(out))


def cmd_core(args) -> None:
    """Show the core set, or change it: --include/--exclude ids, --role id=role."""
    wd = workdir_for(args)
    path = os.path.join(wd, "core.json")
    core = load_json(path)
    if core is None:
        raise SystemExit("No core.json in %s - run `backward` first." % wd)
    by_id = {}
    for row in core:
        by_id.setdefault(row["id"], row)
        if row.get("paperId"):
            by_id.setdefault(row["paperId"], row)

    def find(ident: str) -> dict:
        row = by_id.get(ident.strip()) or by_id.get(ident.strip()[:8])
        if not row:
            raise SystemExit("Unknown core id %r (ids are the 8-character keys printed by `backward` / `core`)." % ident)
        return row

    changed = []
    http = None
    for spec in args.add or []:  # papers the script missed: resolve them and add to the core set
        ident, _, role = spec.partition("=")
        role = role or "related"
        if role not in ROLE_LABEL:
            raise SystemExit("Unknown role %r; use one of: %s" % (role, ", ".join(ROLE_LABEL)))
        http = http or Http(args.cache_dir, refresh=args.refresh)
        pid = parse_seed(ident.strip())
        paper = s2_paper(http, pid, fields=PAPER_FIELDS) if pid else None
        if not paper:
            raise SystemExit("Semantic Scholar does not know %r; try an arXiv id, DOI or S2 paperId "
                             "(`fbsearch.py resolve \"<title>\"` finds one)." % ident)
        row = by_id.get(paper["paperId"])
        if row is None:
            row = {"id": short_id(paper["paperId"]), "paperId": paper["paperId"], "title": paper.get("title"),
                   "year": paper.get("year"), "venue": paper.get("venue"), "citationCount": paper.get("citationCount") or 0,
                   "publicationDate": paper.get("publicationDate"), "externalIds": paper.get("externalIds") or {},
                   "role": role, "score": 0, "overlap": 0, "evidence": ["added by hand"], "include": True}
            core.insert(0, row)
            by_id[row["id"]] = by_id[row["paperId"]] = row
        row.update(include=True, excluded=False, role=role)
        changed.append("+%s (%s)" % (row["id"], (row["title"] or "")[:40]))
    for ident in filter(None, (args.include or "").split(",")):
        row = find(ident)
        if not row.get("paperId"):
            raise SystemExit("%s has no Semantic Scholar id, so it cannot feed the forward search." % ident)
        row["include"], row["excluded"] = True, False
        changed.append("+" + row["id"])
    for ident in filter(None, (args.exclude or "").split(",")):
        row = find(ident)
        row["include"], row["excluded"] = False, True  # also keeps it out of the report's backward list
        changed.append("-" + row["id"])
    for spec in args.role or []:
        ident, _, role = spec.partition("=")
        if role not in ROLE_LABEL:
            raise SystemExit("Unknown role %r; use one of: %s" % (role, ", ".join(ROLE_LABEL)))
        find(ident)["role"] = role
        changed.append("%s=%s" % (ident, role))
    if changed:
        save_json(path, core)
        print("UPDATED %s" % " ".join(changed))
    inc = [r for r in core if r.get("include")]
    print("CORE    %d papers feed the forward search" % len(inc))
    rows = inc if not args.all else core
    for row in rows:
        print("  %-4s %-9s %-18s %7s  %-5s %s | %s" % (
            "y" if row.get("include") else "-", row["id"], row["role"], row["citationCount"], row.get("year") or "",
            (row["title"] or "")[:70], "; ".join(row.get("evidence") or [])[:120]))


def cmd_forward(args) -> None:
    wd = workdir_for(args)
    seed = load_json(os.path.join(wd, "seed.json"))
    core = load_json(os.path.join(wd, "core.json"), [])
    if not seed:
        raise SystemExit("No seed.json in %s - run `backward` first." % wd)
    http = Http(args.cache_dir, refresh=args.refresh)
    today = dt.date.today()
    keyed = bool(s2_key())
    seed_cap = args.seed_cap or (9999 if keyed else 3000)
    core_cap = args.cap or (3000 if keyed else 1000)
    cfg = {"min_citations": args.min_citations, "recent_months": args.recent_months,
           "recent_min_citations": args.recent_min_citations}
    floor = min(cfg["min_citations"], cfg["recent_min_citations"])
    keywords = [k.strip().lower() for k in (args.keywords or "").split(",") if k.strip()]
    query = args.query or (" | ".join(('"%s"' % k) if " " in k else k for k in keywords) if keywords else None)
    if not keywords:
        keywords = keywords_from(seed.get("title") or "")
    included = [c for c in core if c.get("include") and c.get("paperId")]
    core_ids = {c["paperId"] for c in included}
    # Hubs (ViT, CLIP, GPT-3, or SAM for a SAM-variant seed) are cited by nearly every paper in the area, so citing
    # one says little about relevance: half weight. "Hub" is relative to the seed's own citation count.
    hub_cut = max(HUB_MIN_CITATIONS, 10 * (seed.get("citationCount") or 0))
    weight = {c["paperId"]: (0.5 if (c.get("citationCount") or 0) >= hub_cut else 1.0) for c in included}
    seed_id = seed["paperId"]
    excluded_ids = {c["paperId"] for c in core if c.get("excluded") and c.get("paperId")}
    log("Forward search over the seed + %d core papers (newest %d citers each, %d for the seed) ..."
        % (len(included), core_cap, seed_cap))

    pool = {}  # paperId -> candidate record

    def add(paper: dict, source: str, cited: str | None = None, influential: bool = False):
        pid = paper.get("paperId")
        if not pid or pid == seed_id or pid in excluded_ids:  # excluded with `core --exclude`: keep out entirely
            return None
        rec = pool.get(pid)
        if rec is None:
            rec = pool[pid] = {"paper": dict(paper), "sources": set(), "cited": set(), "influential": 0,
                               "backward_role": None, "evidence": [], "core_included": False, "verified": False}
        else:
            for k, v in paper.items():
                old = rec["paper"].get(k)
                if v is not None and (old is None or (k == "citationCount" and v > (old or 0))):
                    rec["paper"][k] = v
        rec["sources"].add(source)
        if cited and cited != pid:  # Semantic Scholar occasionally lists a paper among its own citers
            rec["cited"].add(cited)
        rec["influential"] += int(bool(influential))
        return rec

    oa = OpenAlex(http, args.openalex_budget) if args.openalex_budget > 0 else None
    oa_wanted = {}  # Semantic Scholar id -> (OpenAlex title, set of cited paperIds)

    def queue_openalex(paper: dict) -> None:
        wid = oa.work_id(paper)
        for w in (oa.top_citers(wid, min_cites=floor) if wid else []):
            for sid in OpenAlex.s2_ids(w):
                oa_wanted.setdefault(sid, (w.get("display_name"), set()))[1].add(paper["paperId"])

    # 1. Citers of the seed (complete unless it has more than seed_cap).
    edges = s2_edges(http, seed_id, "citations", CIT_FIELDS, cap=seed_cap)
    for e in edges:
        add(e.get("citingPaper") or {}, "seed-citers", seed_id, e.get("isInfluential"))
    log("  seed: %d citing papers%s" % (len(edges), " (newest only)" if (seed.get("citationCount") or 0) > len(edges) else ""))
    if oa and (seed.get("citationCount") or 0) > len(edges):
        queue_openalex(seed)
    n_seed_citers = len(edges)

    # 2. Newest citers of each core paper. Older influential citers come from steps 3 and 4.
    for c in included:
        edges = s2_edges(http, c["paperId"], "citations", CIT_FIELDS, cap=core_cap)
        for e in edges:
            add(e.get("citingPaper") or {}, "core-citers", c["paperId"], e.get("isInfluential"))
        partial = (c.get("citationCount") or 0) > len(edges)
        if oa and partial:
            queue_openalex(c)
        log("  %-60s %6d citers%s" % ((c["title"] or "")[:60], len(edges), " (newest only)" if partial else ""))

    # 3. Citation-sorted topic search: influential papers on the seed's topic, however old.
    n_topic = 0
    if query and not args.no_topic_search:
        hits = s2_bulk_search(http, query, min_citations=floor, limit=args.topic_limit)
        for p in hits:
            add(p, "topic-search")
        n_topic = len(hits)
        log("  topic search %s: %d papers with at least %d citations" % (query, n_topic, floor))
    elif not query:
        log("  No --query/--keywords given, so no topic search: older influential citers of heavily cited core papers "
            "can be missed.")

    # 4. OpenAlex's most-cited citers of partially fetched papers, re-resolved in Semantic Scholar.
    if oa_wanted:
        found = s2_batch(http, list(oa_wanted), fields=PAPER_FIELDS)
        matched = 0
        for sid, p in found.items():
            title, cited_ids = oa_wanted[sid]
            if title_close(p.get("title"), title):  # drops OpenAlex mis-merges
                for cid in cited_ids:
                    add(p, "openalex", cid)
                matched += 1
        log("  OpenAlex: %d most-cited citers matched in Semantic Scholar (%d billable OpenAlex calls)" % (matched, oa.used))

    # 5. The backward side: the core set plus other compared / related references.
    for c in core:
        relevant_role = c["role"] in CORE_ROLES and (c["role"] != "related" or (c.get("overlap") or 0) >= 0.2)
        if c.get("paperId") and (c.get("include") or (relevant_role and not c.get("excluded"))):
            rec = add({k: c.get(k) for k in ("paperId", "title", "year", "venue", "citationCount", "externalIds",
                                             "publicationDate")}, "backward")
            if rec:
                rec["backward_role"] = c["role"]
                rec["evidence"] = c.get("evidence") or []
                rec["core_included"] = bool(c.get("include"))

    # 6. Verify which of the seed + core papers each candidate above the threshold really cites.
    targets = core_ids | {seed_id}
    to_verify = [pid for pid, rec in pool.items() if not rec["backward_role"] and passes_threshold(rec["paper"], cfg, today)]
    to_verify.sort(key=lambda pid: -(pool[pid]["paper"].get("citationCount") or 0))
    to_verify = to_verify[:args.verify_cap]
    log("Checking the reference lists of %d candidates above the citation threshold ..." % len(to_verify))
    # Candidates with no observed citation edge (topic search / OpenAlex hits) depend entirely on this check.
    reflists = s2_reference_ids(http, to_verify, deep={pid for pid in to_verify if not pool[pid]["cited"]})
    no_reflist = 0
    for pid in to_verify:
        refs = reflists.get(pid)
        if refs is None:
            no_reflist += 1
            continue
        pool[pid]["cited"] |= refs & targets
        pool[pid]["verified"] = True

    # 7. Relevance tiers, threshold, size caps.
    kw_phrases = [norm(k) for k in keywords if norm(k)]
    selected_b, selected_f, below = [], [], []
    for pid, rec in pool.items():
        p = rec["paper"]
        ok = passes_threshold(p, cfg, today)
        title_n = " %s " % norm(p.get("title"))
        rec["keyword_hits"] = [k for k in kw_phrases if (" %s " % k) in title_n]
        rec["cites_seed"] = seed_id in rec["cited"]
        rec["n_core"] = len(rec["cited"] & core_ids)
        core_score = sum(weight[c] for c in rec["cited"] & core_ids)
        if rec["backward_role"]:
            rec["tier"] = 0 if rec["core_included"] else 3
            if ok:
                selected_b.append(rec)
            elif rec["core_included"]:
                below.append(rec)
            continue
        topical = bool(rec["keyword_hits"]) or "topic-search" in rec["sources"]
        if not (rec["cites_seed"] or core_score >= 2 or (rec["n_core"] >= 1 and (topical or rec["influential"]))):
            continue
        # Co-citation alone can come from a neighbouring field citing the same famous papers; topic match confirms it.
        # Core hits are capped at 4 so a paper that lists every classic baseline cannot outrank an influential one.
        rel = (1.5 * rec["cites_seed"] + min(core_score, 4.0) * (1.0 if topical else 0.6) + 0.5 * topical
               + 0.5 * bool(rec["influential"]))
        rec["survey"] = bool(SURVEY_RE.search(p.get("title") or "")) or "Review" in (p.get("publicationTypes") or [])
        if rec["survey"]:
            rel = min(rel, 2.5)  # surveys cite everything; keep them, but below focused work
        rec["relevance"] = round(rel, 2)
        rec["rank"] = rel + math.log10(1 + (p.get("citationCount") or 0))  # relevance, then influence
        rec["tier"] = 1 if rel >= 3.5 else (2 if rel >= 2 else 3)
        if ok:
            selected_f.append(rec)

    selected_b.sort(key=lambda r: (r["tier"], -(r["n_core"] + r["cites_seed"]), -(r["paper"].get("citationCount") or 0)))
    selected_f.sort(key=lambda r: -r["rank"])
    selected_b = dedupe_titles(selected_b)
    selected_f = dedupe_titles(selected_f, against=selected_b + below)
    role_of = {c["paperId"]: c["role"] for c in included}
    for rec in selected_f:
        rec["lineage"] = lineage_of(rec, role_of, weight)
    picked_f = balanced_pick(selected_f, args.max_forward)
    picked_ids = {id(r) for r in picked_f}
    overflow = selected_b[args.max_backward:] + [r for r in selected_f if id(r) not in picked_ids]
    chosen = selected_b[:args.max_backward] + picked_f
    log("Selected %d backward + %d forward papers (%d overflow, %d directly compared but below threshold)"
        % (min(len(selected_b), args.max_backward), min(len(selected_f), args.max_forward), len(overflow), len(below)))

    details = s2_batch(http, [r["paper"]["paperId"] for r in chosen + below])
    for rec in chosen + below:
        d = details.get(rec["paper"]["paperId"])
        if d:
            rec["paper"] = d
    if not args.no_code:
        log("Looking up code (Hugging Face, arXiv, GitHub) ...")
        find_code(http, [r["paper"] for r in chosen + below], use_search=not args.no_github_search)

    title_of = {c["paperId"]: c["title"] for c in included}
    seed_ref_role = {c["paperId"]: c["role"] for c in core if c.get("paperId")}  # everything the seed cites
    chosen_ids = {id(r) for r in chosen}
    below_ids = {id(r) for r in below}
    out = []
    for rec in chosen + below + overflow:
        p = rec["paper"]
        relation = []
        ref_role = None if rec["backward_role"] else seed_ref_role.get(p.get("paperId"))
        if rec["backward_role"]:
            relation.append(ROLE_LABEL.get(rec["backward_role"], rec["backward_role"]))
        elif ref_role:  # found by the forward search, but the seed cites it too
            relation.append("cited by seed (%s)" % ROLE_LABEL.get(ref_role, ref_role))
        if rec["cites_seed"]:
            relation.append("cites seed")
        core_titles = [label(title_of[h]) for h in rec["cited"] if h in title_of]
        if core_titles:
            relation.append("cites %d core: %s" % (len(core_titles), ", ".join(sorted(core_titles)[:4])
                                                    + ("…" if len(core_titles) > 4 else "")))
        if not relation:
            relation.append("topic search")
        if rec.get("survey"):
            relation.append("survey")
        out.append({
            "key": short_id(p.get("paperId")), "paperId": p.get("paperId"), "title": p.get("title"),
            "year": p.get("year"), "venue": p.get("venue"), "publicationDate": p.get("publicationDate"),
            "citationCount": p.get("citationCount"), "influentialCitationCount": p.get("influentialCitationCount"),
            "externalIds": p.get("externalIds"), "authors": [a.get("name") for a in (p.get("authors") or [])][:6],
            "tldr": ((p.get("tldr") or {}).get("text") if isinstance(p.get("tldr"), dict) else None),
            "abstract": p.get("abstract"), "publicationTypes": p.get("publicationTypes"),
            "direction": "backward" if (rec["backward_role"] or ref_role) else "forward",
            "role": rec["backward_role"], "tier": rec.get("tier"), "relation": "; ".join(relation),
            "cites_seed": rec["cites_seed"], "n_core": rec["n_core"], "relevance": rec.get("relevance"),
            "rank": round(rec["rank"], 2) if rec.get("rank") is not None else None,
            "lineage": rec.get("lineage"),
            "found_via": sorted(rec["sources"]),
            "verified": rec["verified"], "evidence": rec.get("evidence") or [], "code": p.get("code"),
            "link": paper_link(p),
            "bucket": "main" if id(rec) in chosen_ids else ("below_threshold" if id(rec) in below_ids else "overflow"),
        })
    meta = {"generated": today.isoformat(), "thresholds": cfg, "keywords": keywords, "query": query,
            "seed_cap": seed_cap, "cap": core_cap, "seed_citers": n_seed_citers, "core_used": len(included),
            "topic_hits": n_topic, "pool": len(pool), "verified": len(to_verify) - no_reflist,
            "no_reference_list": no_reflist, "openalex_calls": oa.used if oa else 0,
            "s2_busy_retries": http.s2_busy, "failures": http.failures, "http_calls": http.counts}
    save_json(os.path.join(wd, "candidates.json"), {"meta": meta, "papers": out})
    write_briefs(wd, seed, out)
    print_forward_summary(wd, out, meta)


def lineage_of(rec: dict, role_of: dict, weight: dict) -> str:
    """Which part of the comparison set connects a forward paper: 'seed', a core role, or 'topic'."""
    if rec["cites_seed"]:
        return "seed"
    score = {}
    for pid in rec["cited"]:
        if pid in role_of:
            score[role_of[pid]] = score.get(role_of[pid], 0) + weight.get(pid, 1.0)
    return max(sorted(score), key=lambda r: score[r]) if score else "topic"


def balanced_pick(recs: list, n: int) -> list:
    """Fill n slots across lineages in proportion to sqrt(lineage size), with the seed's own citers weighted double.
    One tightly co-cited cluster (e.g. the papers citing a dataset paper's benchmarked models) cannot take every slot,
    and a small lineage cannot pad the list with weak papers. Each lineage is consumed best rank first."""
    groups = {}
    for rec in sorted(recs, key=lambda r: -r["rank"]):
        groups.setdefault(rec["lineage"], []).append(rec)
    weight = {g: math.sqrt(len(items)) * (2.0 if g == "seed" else 1.0) for g, items in groups.items()}
    taken = {g: 0 for g in groups}
    out = []
    while len(out) < n and any(groups.values()):
        g = min((g for g in groups if groups[g]), key=lambda g: (taken[g] / weight[g], -groups[g][0]["rank"]))
        out.append(groups[g].pop(0))
        taken[g] += 1
    return out


def same_title(a: str | None, b: str | None) -> bool:
    """Same paper: identical titles, or "Name: subtitle" vs "subtitle" (arXiv vs venue version). Stricter than
    title_close, which would merge e.g. "Video Mask Transfiner ..." with "Mask Transfiner ..."."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return any(":" in (x or "") and norm(x.split(":", 1)[1]) == norm(y) for x, y in ((a, b), (b, a)))


def dedupe_titles(recs: list, against: list = ()) -> list:
    """Semantic Scholar sometimes has two records for one paper (e.g. "Semantic-SAM: X" and the venue version "X").
    Keep the first, best-ranked record of each title, and drop records duplicating one in `against`."""
    kept = []
    for rec in recs:
        title = rec["paper"].get("title")
        if any(same_title(title, other["paper"].get("title")) for other in list(against) + kept):
            continue
        kept.append(rec)
    return kept


def write_briefs(wd: str, seed: dict, papers: list) -> None:
    lines = ["# Briefs for writing diffs.json", "",
             "Seed: %s (%s)" % (seed.get("title"), seed.get("year")), "",
             "Seed abstract: %s" % (seed.get("abstract") or "(none)"), "",
             "Write diffs.json as {\"<key>\": \"one sentence: how it differs from the seed\"}; "
             "use \"EXCLUDE: <reason>\" to drop an off-topic paper.", ""]
    for p in papers:
        if p["bucket"] == "overflow":
            continue
        text = p.get("tldr") or (p.get("abstract") or "")[:700]
        lines.append("## %s | %s (%s) | %s cites | %s" % (p["key"], p["title"], p.get("year"), p.get("citationCount"), p["relation"]))
        lines.append(text or "(no abstract available - judge from the title or skim the paper)")
        lines.append("")
    with open(os.path.join(wd, "briefs.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def print_forward_summary(wd: str, papers: list, meta: dict) -> None:
    main = [p for p in papers if p["bucket"] == "main"]
    with_code = [p for p in main if code_cell(p.get("code"))[0]]
    print("WORKDIR %s" % wd)
    print("POOL    %d unique papers seen (%d seed citers, %d from the topic search); %d reference lists checked%s"
          % (meta["pool"], meta["seed_citers"], meta["topic_hits"], meta["verified"],
             (" (%d candidates had no retrievable reference list)" % meta["no_reference_list"])
             if meta.get("no_reference_list") else ""))
    print("KEPT    %d papers (%d with code), %d below threshold, %d overflow"
          % (len(main), len(with_code), sum(p["bucket"] == "below_threshold" for p in papers),
             sum(p["bucket"] == "overflow" for p in papers)))
    if meta.get("s2_busy_retries"):
        print("NOTE    %d retries against Semantic Scholar's busy keyless pool; an S2_API_KEY makes runs much faster."
              % meta["s2_busy_retries"])
    if meta.get("failures"):
        print("WARNING requests failed even after retries (%s), so results, especially code links, may be incomplete. "
              "Re-run `forward` when the network is stable; cached responses are reused."
              % ", ".join("%s x%d" % kv for kv in sorted(meta["failures"].items(), key=lambda kv: -kv[1])))
    print("NEXT    read %s, write %s, then run `report`" % (os.path.join(wd, "briefs.md"), os.path.join(wd, "diffs.json")))
    print("")
    print("  key       dir  cites   year  code                        title | relation")
    for p in sorted(main, key=lambda x: -(x.get("citationCount") or 0)):
        code = (p.get("code") or {})
        gh = [g for g in code.get("github") or [] if "verify" not in (g.get("source") or "")]
        hf = [h for h in code.get("hf") or [] if h.get("verified", True)]
        maybe = [g for g in code.get("github") or [] if "verify" in (g.get("source") or "")]
        cell = (("%s ★%s" % (gh[0]["repo"], human(gh[0]["stars"]))) if gh else
                ("HF %s ♥%s" % (hf[0]["id"], hf[0]["likes"])) if hf else
                ("?%s (unverified)" % maybe[0]["repo"]) if maybe else "-")
        print("  %-9s %-4s %6s  %-5s %-27s %s | %s" % (p["key"], p["direction"][0].upper(), p.get("citationCount"),
                                                      p.get("year") or "", cell[:27], (p["title"] or "")[:60], p["relation"][:70]))


# ----------------------------------------------------------------------------- report

def md_escape(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("|", "\\|").strip()


def code_cell(code: dict | None) -> tuple:
    """-> (verified links md, their stars/likes md, popularity for tie-breaking, unverified links md).

    Only verified links put a paper in "with code": repos linked from the paper or its Hugging Face page, strong
    GitHub-search matches, and Hugging Face artifacts owned by the same account as that repo.
    """
    code = code or {}
    links, counts, maybe, pop = [], [], [], 0
    for g in code.get("github") or []:
        if "verify" in (g.get("source") or ""):
            maybe.append("[%s](%s) ★%s" % (g["repo"], g["url"], human(g["stars"])))
            continue
        links.append("[%s](%s)" % (g["repo"], g["url"]))
        counts.append("★%s" % human(g["stars"]))
        pop = max(pop, g["stars"] or 0)
    for h in code.get("hf") or []:
        if not h.get("verified", True):
            maybe.append("🤗 [%s](%s) ♥%s" % (h["id"], h["url"], human(h["likes"])))
            continue
        links.append("🤗 [%s](%s)" % (h["id"], h["url"]))
        counts.append("♥%s" % human(h["likes"]))
        pop = max(pop, h["likes"] or 0)
    return "<br>".join(links), "<br>".join(counts), pop, "<br>".join(maybe)


def cmd_report(args) -> None:
    wd = workdir_for(args)
    seed = load_json(os.path.join(wd, "seed.json"))
    data = load_json(os.path.join(wd, "candidates.json"))
    core = load_json(os.path.join(wd, "core.json"), [])
    if not seed or not data:
        raise SystemExit("Missing seed.json / candidates.json in %s - run `backward` and `forward` first." % wd)
    diffs = load_json(os.path.join(wd, "diffs.json"), {}) or {}
    diffs = {k[:8]: v for k, v in diffs.items()}
    meta, papers = data["meta"], data["papers"]

    def diff_of(p):
        d = diffs.get(p["key"])
        if isinstance(d, dict):
            return d.get("diff") or "", bool(d.get("exclude"))
        d = (d or "").strip()
        return d, d.upper().startswith("EXCLUDE")

    main, excluded = [], []
    for p in papers:
        if p["bucket"] != "main":
            continue
        d, ex = diff_of(p)
        (excluded if ex else main).append((p, d))

    def sort_key(item):
        p = item[0]
        return (-(p.get("citationCount") or 0), -code_cell(p.get("code"))[2])

    with_code = sorted([x for x in main if code_cell(x[0].get("code"))[0]], key=sort_key)
    no_code = sorted([x for x in main if not code_cell(x[0].get("code"))[0]], key=sort_key)
    th = meta["thresholds"]
    seed_link = paper_link(seed)
    lines = ["# Forward–backward search: %s" % md_escape(seed.get("title")), ""]
    lines.append("**Seed:** [%s](%s) — %s, %s %s · %s citations" % (
        md_escape(seed.get("title")), seed_link, first_author(seed) or "?", seed.get("venue") or "", seed.get("year") or "",
        human(seed.get("citationCount"))))
    lines.append("")
    lines.append("**Paper type:** %s · **Searched:** %s · **Threshold:** ≥%d citations (≥%d if published in the last %d months)"
                 % (seed.get("paper_type"), meta["generated"], th["min_citations"], th["recent_min_citations"], th["recent_months"]))
    lines.append("")
    lines.append("**Kept:** %d papers — %d with code, %d without. Citation counts: Semantic Scholar (Google Scholar is usually higher)."
                 % (len(main), len(with_code), len(no_code)))
    lines.append("")
    groups = {}
    for c in core:
        if c.get("include"):
            groups.setdefault(c["role"], []).append(c)
    if groups:
        lines += ["## What the seed compares against", ""]
        heading = {"baseline": "Baselines", "benchmarked_model": "Benchmarked models", "compared_dataset": "Compared datasets",
                   "compared_benchmark": "Compared benchmarks", "related": "Closest related work"}
        for role in ("baseline", "benchmarked_model", "compared_dataset", "compared_benchmark", "related"):
            items = groups.get(role)
            if items:
                names = ", ".join("%s (%s)" % (md_escape(label(i["title"], 60)), i.get("year") or "?") for i in items)
                lines.append("- **%s:** %s" % (heading[role], names))
        lines.append("")

    def table(rows, with_code_col):
        maybe_col = not with_code_col and any(code_cell(p.get("code"))[3] for p, _ in rows)
        head = ("| # | Paper | Year | Cites | Found via | " + ("Code | ★ / ♥ | " if with_code_col else "")
                + ("Possible code (unverified) | " if maybe_col else "") + "How it differs from the seed |")
        sep = "|---:|---|---:|---:|---|" + ("---|---:|" if with_code_col else "") + ("---|" if maybe_col else "") + "---|"
        out = [head, sep]
        for i, (p, d) in enumerate(rows, 1):
            links, counts, _, maybe = code_cell(p.get("code"))
            paper = "[%s](%s) · [GS](%s)" % (md_escape(p["title"]), p.get("link") or "", scholar_link(p["title"]))
            via = ("↩ " if p["direction"] == "backward" else "↪ ") + md_escape(p.get("relation"))
            diff = md_escape(d) or "_(to do)_"
            cells = [str(i), paper, str(p.get("year") or ""), human(p.get("citationCount")), via]
            if with_code_col:
                cells += [links, counts]
            if maybe_col:
                cells.append(maybe)
            out.append("| " + " | ".join(cells + [diff]) + " |")
        return out

    lines += ["## 1. Influential papers with code", "", "Sorted by citations, then GitHub stars / Hugging Face likes. "
              "↩ = backward (cited by the seed), ↪ = forward (cites the seed or its comparison set).", ""]
    lines += table(with_code, True) if with_code else ["_None found._"]
    lines += ["", "## 2. Influential papers without public code found", ""]
    lines += table(no_code, False) if no_code else ["_None._"]
    below = [p for p in papers if p["bucket"] == "below_threshold"]
    overflow = [p for p in papers if p["bucket"] == "overflow"]
    if below:
        lines += ["", "## Appendix A. Directly compared, but below the citation threshold", ""]
        for p in sorted(below, key=lambda x: -(x.get("citationCount") or 0)):
            links, counts, _, _ = code_cell(p.get("code"))
            d, ex = diff_of(p)
            lines.append("- [%s](%s) (%s) · %s cites · %s%s%s" % (
                md_escape(p["title"]), p.get("link"), p.get("year"), p.get("citationCount"), md_escape(p.get("relation")),
                (" · " + links.replace("<br>", ", ")) if links else "", (" — " + md_escape(d)) if d and not ex else ""))
    if overflow:
        lines += ["", "## Appendix B. More candidates over the size cap (not reviewed)", ""]
        for p in sorted(overflow, key=lambda x: -(x.get("citationCount") or 0))[:80]:
            lines.append("- [%s](%s) (%s) · %s cites · %s" % (md_escape(p["title"]), p.get("link"), p.get("year"),
                                                             p.get("citationCount"), md_escape(p.get("relation"))))
    if excluded:
        lines += ["", "<details><summary>Excluded as off-topic (%d)</summary>" % len(excluded), ""]
        for p, d in excluded:
            lines.append("- %s — %s" % (md_escape(p["title"]), md_escape(d.split(":", 1)[-1])))
        lines += ["", "</details>"]
    sources = ["the seed's citers (%s)" % human(meta.get("seed_citers")),
               "the newest %s citers of each core paper" % human(meta.get("cap"))]
    if meta.get("query"):
        sources.append("a citation-sorted Semantic Scholar search for %s" % meta["query"])
    if meta.get("openalex_calls"):
        sources.append("OpenAlex's most-cited citers")
    lines += ["", "---", "_Method: backward = the seed's references, with roles read from its comparison tables, related "
              "work and citation sentences. Forward candidates came from %s; each candidate's reference list was then "
              "checked, and kept papers cite the seed, or at least 2 core papers, or 1 core paper plus a topic match "
              "(keywords: %s). Citation counts are Semantic Scholar's as of %s; the GS links open Google Scholar, "
              "whose counts run higher. Generated by the citation-snowball skill._"
              % ("; ".join(sources), ", ".join(meta.get("keywords") or []) or "-", meta["generated"])]
    out_path = args.out or os.path.join(wd, "report.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    todo = sum(1 for p, d in main if not d)
    print("REPORT  %s  (%d papers: %d with code, %d without; %d still missing a diff)" % (out_path, len(main), len(with_code), len(no_code), todo))


def cmd_code(args) -> None:
    http = Http(args.cache_dir, refresh=args.refresh)
    papers = []
    for ident in args.ids:
        p = s2_paper(http, parse_seed(ident) or ident)
        if p:
            papers.append(p)
    find_code(http, papers, use_search=not args.no_github_search)
    for p in papers:
        print(json.dumps({"title": p.get("title"), "code": p.get("code")}, ensure_ascii=False))


def probe(url: str, headers: dict | None = None) -> tuple:
    """One un-retried GET -> (HTTP status or error text, response headers)."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {})
    except OSError as err:
        return "network error: %s" % getattr(err, "reason", err), {}


def cmd_doctor(args) -> None:
    """Check the APIs, keys and tools the skill uses, and say what would make runs faster."""
    rows, tips = [], []
    rows.append(("python", sys.version.split()[0], "ok" if sys.version_info >= (3, 8) else "needs Python 3.8+"))
    key = s2_key()
    n = 2 if key else 6
    codes = []
    for i in range(n):
        codes.append(probe(S2 + "/paper/ARXIV:1706.03762?fields=title", s2_headers())[0])
        time.sleep(1.1)
    ok = sum(1 for c in codes if c == 200)
    if any(isinstance(c, str) for c in codes):
        status = "unreachable (%s)" % next(c for c in codes if isinstance(c, str))
        tips.append("No network access to api.semanticscholar.org. In a sandboxed agent (e.g. Codex), approve network "
                    "access for this command.")
    elif key:
        status = "API key set; %d/%d probes ok" % (ok, n)
    else:
        status = "no API key; shared keyless pool answered %d/%d probes" % (ok, n)
        tips.append("Semantic Scholar has no key: runs still work but retry a lot when the shared pool is busy "
                    "(%d/%d answered just now). A free key (https://www.semanticscholar.org/product/api#api-key-form) "
                    "in S2_API_KEY makes them fast and reliable." % (ok, n))
    rows.append(("Semantic Scholar", status, "required"))
    code, hdrs = probe("https://export.arxiv.org/api/query?id_list=1706.03762&max_results=1")
    rows.append(("arXiv API", "ok" if code == 200 else str(code), "seed full text, metadata"))
    code, hdrs = probe(OPENALEX + "/works/W2626778328?select=id",
                       {"Authorization": "Bearer " + os.environ["OPENALEX_API_KEY"]} if os.environ.get("OPENALEX_API_KEY") else None)
    left = {k.lower(): v for k, v in hdrs.items()}.get("x-ratelimit-remaining")
    rows.append(("OpenAlex", ("ok" if code == 200 else str(code)) + (", %s credits left today" % left if left else "")
                 + ("" if os.environ.get("OPENALEX_API_KEY") else " (keyless)"), "optional"))
    code, hdrs = probe("https://huggingface.co/api/papers/2106.09685")
    rows.append(("Hugging Face", "ok" if code == 200 else str(code), "code links, likes"))
    token = github_token()
    if token:
        code, hdrs = probe("https://api.github.com/rate_limit", {"Authorization": "bearer " + token})
        rows.append(("GitHub", "token found; %s" % ("ok" if code == 200 else code), "stars"))
    else:
        code, hdrs = probe("https://api.github.com/rate_limit")
        rows.append(("GitHub", "no token (60 requests/hour)", "stars"))
        tips.append("No GitHub token: star counts come from Hugging Face where possible, else at most 55 repos per run. "
                    "`gh auth login` (or GH_TOKEN) lifts that.")
    rows.append(("pdftotext", "found" if shutil.which("pdftotext") else "not found", "optional, for non-arXiv PDFs"))
    cache = writable_dir(args.cache_dir) if args.cache_dir else None
    rows.append(("cache dir", cache or "none (not writable)", "HTTP cache shared across runs"))
    width = max(len(r[0]) for r in rows)
    for name, status, note in rows:
        print("%-*s  %s  [%s]" % (width, name, status, note))
    if tips:
        print("")
        for tip in tips:
            print("TIP  " + tip)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Forward/backward citation search helper (citation-snowball skill).")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cache-dir", default=default_cache_dir(),
                        help="HTTP cache shared across runs (default: %(default)s; FBS_CACHE_DIR overrides)")
    common.add_argument("--refresh", action="store_true", help="ignore cached HTTP responses")

    p = sub.add_parser("doctor", parents=[common], help="check APIs, keys and tools")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("resolve", parents=[common],
                       help="resolve a seed (arXiv id/URL, DOI, S2 id/URL, ACL id, OpenReview URL, or title)")
    p.add_argument("seed")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("backward", parents=[common], help="resolve seed, digest full text, fetch references, propose core set")
    p.add_argument("seed")
    p.add_argument("--workdir", help="default: ./fbsearch-<title-slug>")
    p.add_argument("--type", choices=["method", "dataset", "benchmark", "mixed"], help="override the auto-guessed paper type")
    p.add_argument("--max-core", type=int, default=30, help="max core papers marked include=true (default 30)")
    p.add_argument("--max-bib-lookups", type=int, default=25,
                   help="max bibliography titles to look up when Semantic Scholar lacks references (default 25)")
    p.set_defaults(func=cmd_backward)

    p = sub.add_parser("core", parents=[common], help="show or edit the core set (core.json)")
    p.add_argument("--workdir", required=True, help="the WORKDIR printed by `backward`")
    p.add_argument("--add", action="append", metavar="ID[=ROLE]",
                   help="add a paper the script missed, by arXiv id / DOI / S2 id, e.g. 2310.11454=baseline (repeatable)")
    p.add_argument("--include", help="comma-separated ids to add to the core set")
    p.add_argument("--exclude", help="comma-separated ids to drop from the core set")
    p.add_argument("--role", action="append", help="id=role, e.g. 1628a19c=baseline (repeatable)")
    p.add_argument("--all", action="store_true", help="list every reference, including background ones")
    p.set_defaults(func=cmd_core)

    p = sub.add_parser("forward", parents=[common], help="gather and verify citing papers, thresholds, code links")
    p.add_argument("--workdir", required=True, help="the WORKDIR printed by `backward`")
    p.add_argument("--query", help='Semantic Scholar boolean query for the topic search, e.g. \'"interactive '
                                   'segmentation" | "part segmentation"\' (default: built from --keywords)')
    p.add_argument("--keywords", help="comma-separated topic phrases for relevance (default: words of the seed title)")
    p.add_argument("--min-citations", type=int, default=30, help="citation threshold (default 30)")
    p.add_argument("--recent-months", type=int, default=18,
                   help="papers published within this many months use --recent-min-citations (default 18)")
    p.add_argument("--recent-min-citations", type=int, default=10, help="threshold for recent papers (default 10)")
    p.add_argument("--seed-cap", type=int, help="max citers of the seed (default 3000 without an S2 key, 9999 with)")
    p.add_argument("--cap", type=int, help="max newest citers per core paper (default 1000 without an S2 key, 3000 with)")
    p.add_argument("--topic-limit", type=int, default=1000, help="max papers from the topic search (default 1000)")
    p.add_argument("--no-topic-search", action="store_true", help="skip the citation-sorted topic search")
    p.add_argument("--verify-cap", type=int, default=3000,
                   help="max candidates whose reference lists are checked, most cited first (default 3000)")
    p.add_argument("--max-backward", type=int, default=30, help="max backward papers in the report (default 30)")
    p.add_argument("--max-forward", type=int, default=60,
                   help="max forward papers in the report, shared across lineages (default 60)")
    p.add_argument("--openalex-budget", type=int, default=40,
                   help="max billable OpenAlex calls for most-cited citers of partially fetched papers (0 = off)")
    p.add_argument("--no-code", action="store_true", help="skip the GitHub / Hugging Face lookup")
    p.add_argument("--no-github-search", action="store_true", help="only use repos linked from the paper itself")
    p.set_defaults(func=cmd_forward)

    p = sub.add_parser("report", help="render report.md from candidates.json + diffs.json")
    p.add_argument("--workdir", required=True, help="the WORKDIR printed by `backward`")
    p.add_argument("--out", help="output path (default: <workdir>/report.md)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("code", parents=[common], help="debug: find code for papers (arXiv ids, DOIs or S2 ids)")
    p.add_argument("ids", nargs="+")
    p.add_argument("--no-github-search", action="store_true")
    p.set_defaults(func=cmd_code)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except HttpFailure as err:
        raise SystemExit("ERROR: %s" % err)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
