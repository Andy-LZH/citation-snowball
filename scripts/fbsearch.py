#!/usr/bin/env python3
"""fbsearch.py - helper CLI for the citation-snowball skill (https://github.com/Andy-LZH/citation-snowball).

Stages (each reads/writes JSON in a work directory):

  backward SEED [SEED ...]
                 resolve each seed paper, digest its full text (tables, related work, citation sentences), fetch its
                 references (recovering ones Semantic Scholar lacks from the bibliography) and propose the core
                 comparison set, merged across seeds
                                -> seeds.json core.json seeds/<key>/{seed,refs,digest,core}.json fulltext.txt
  core           show or edit the merged core set
  forward        backward co-citation (what the seeds and their compared works all cite); route B, the papers citing
                 the seeds (widened to on-topic papers citing the compared works when too few pass); then forward
                 co-citation (what the area's recent papers cite). Verifies reference lists and probes GitHub stars /
                 README docs, saves the whole screened pool, then runs `select`      -> pool.json (+ select's outputs)
  select         apply the influence gates and size caps to pool.json (offline apart from cached lookups), fetch
                 abstracts, BibTeX and code for the kept papers -> candidates.json briefs.md
  report         render the reading map, merging the agent-written diffs.json -> report.md references.bib

Helpers: doctor (check APIs, keys, tools, updates) | update (git pull this skill) | resolve SEED | code ID...

`backward` checks GitHub for a newer release at most once a day (FBS_NO_UPDATE_CHECK=1 turns that off) and prints an
UPDATE line when there is one.

HTTP responses are cached in ~/.cache/citation-snowball (--cache-dir or FBS_CACHE_DIR to change).
Standard library only (Python 3.8+). Needs network access.
"""
from __future__ import annotations

import argparse
import bisect
import collections
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

VERSION = "1.4.0"
REPO = "Andy-LZH/citation-snowball"  # where new releases are published (FBS_UPDATE_REPO=owner/name for a fork)
S2 = "https://api.semanticscholar.org/graph/v1"
OPENALEX = "https://api.openalex.org"
UA = "citation-snowball/%s (+https://github.com/%s; python-urllib)" % (VERSION, REPO)
MISSING = "\x00missing\x00"
# Core papers above max(this, 10x the most-cited seed's citations) count half toward relevance, and widening does not
# fetch their citers: nearly every paper in the area cites SAM, CLIP or DINOv2, so their citers are noise.
HUB_MIN_CITATIONS = 5000
SURVEY_RE = re.compile(r"\b(survey|review|overview|tutorial|primer)\b", re.I)

# Influence gates for forward papers: route -> age bracket -> (min citations, min GitHub stars). A paper passes when it
# is in the bracket and has either enough citations, or enough stars on a verified repo whose README scores at least
# --min-docs (twice the stars when the README could not be read). "seed" = a paper citing a seed, "expand" = a paper
# citing a compared work, added to section B only when too few papers cite the seed. "any" applies at every age. "old"
# means older than the mid bracket; it is closed (None) by default because widening is for recent work, and when
# reopened it also needs
# citations of at least 2 core papers. Calibrated on a 2025 benchmark seed (InstructPart): see CHANGELOG 1.3.0.
# Override with --gate ROUTE.BRACKET=CITES[,STARS]; "-" closes a bracket or its stars route.
GATES = {
    "seed.any": (50, 1000),
    "seed.recent": (10, 100),
    "seed.mid": (25, 250),
    "expand.recent": (20, 150),
    "expand.mid": (40, 250),
    "expand.old": (None, None),
}
TABLE_ROLES = ("baseline", "benchmarked_model", "compared_dataset", "compared_benchmark")

# Co-citation, in two directions. Backward: what the seeds and their compared works cite (k_b of n_b of them).
# Forward: what the area's recent papers cite - papers of the last --latest-months that cite a seed, or cite a compared
# work and match the topic - as a share of those published after the cited paper (only they could cite it).
# Calibrated on InstructPart: ADE20K is cited by 6 of its 15 (seed + compared works); SAM 3, Qwen3-VL and Qwen2.5-VL by
# 22-25% of the on-topic papers published after them.
COCITE = {
    "k_share": 0.2,           # foundations, path (a): cited by at least 20% of the seeds + compared works ...
    "k_min": 3,               # ... and at least 3 of them,
    "confirm_share": 0.05,    # ... and confirmed by at least 5% of recent area papers ...
    "confirm_support": 3,     # ... and at least 3 of them
    "consensus_share": 0.25,  # foundations, path (b): older papers cited by at least 25% of recent area papers
    "min_citations": 100,     # foundations are influential themselves
    "latest_share": 0.15,     # latest models: cited by at least 15% of the area papers published after them ...
    "latest_support": 4,      # ... at least 4 of them ...
    "min_eligible": 10,       # ... out of at least 10 that could have cited them
    "coupling_top": 30,       # same-area check: the 30 strongest backward co-citations
}
S2_CORPUS = 214_000_000  # papers in Semantic Scholar: the specificity weight is log(S2_CORPUS / citations)
# Keyless Semantic Scholar shares one rate-limited pool with every other keyless user, so most requests get 429 at
# busy times. Keep retrying one request for up to this many seconds before giving up.
S2_PATIENCE = float(os.environ.get("FBS_S2_PATIENCE", "900"))

PAPER_FIELDS = "paperId,title,year,venue,publicationDate,citationCount,influentialCitationCount,externalIds"
DETAIL_FIELDS = PAPER_FIELDS + ",abstract,tldr,publicationTypes,openAccessPdf,authors,url"
KEPT_FIELDS = DETAIL_FIELDS + ",citationStyles"  # kept papers also get BibTeX
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


def s2_bulk_search(http: Http, query: str, min_citations: int, limit: int, since: str | None = None) -> list:
    """Papers matching a boolean query (+ | - "phrase" prefix*), most cited first; optionally only those published on
    or after `since` (YYYY-MM-DD)."""
    out, token = [], None
    while len(out) < limit:
        params = {"query": query, "fields": PAPER_FIELDS, "sort": "citationCount:desc",
                  "minCitationCount": min_citations}
        if since:
            params["publicationDateOrYear"] = since + ":"
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


def github_repo_info(http: Http, repos: list, token: str | None, rest_budget: int = 55) -> dict:
    info, uniq = {}, sorted({r.lower(): r for r in repos}.values())
    if token:
        for start in range(0, len(uniq), 40):
            chunk = uniq[start:start + 40]
            parts = []
            for j, full in enumerate(chunk):
                owner, name = full.split("/", 1)
                parts.append("r%d: repository(owner: %s, name: %s) { nameWithOwner stargazerCount url isArchived "
                             "pushedAt createdAt description homepageUrl }" % (j, json.dumps(owner), json.dumps(name)))
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
                                          "created": (r.get("createdAt") or "")[:10],
                                          "description": (r.get("description") or "")[:200],
                                          "homepage": r.get("homepageUrl") or ""}
        return info
    for full in uniq[:rest_budget]:  # unauthenticated REST allows 60 calls/hour
        try:
            r = http.fetch("https://api.github.com/repos/%s" % full, ttl_days=3)
        except HttpFailure as err:
            log("  GitHub REST failed for %s: %s" % (full, err))
            break
        if r:
            info[full.lower()] = {"repo": r["full_name"], "url": r["html_url"], "stars": r["stargazers_count"],
                                  "archived": r.get("archived", False), "pushed": (r.get("pushed_at") or "")[:10],
                                  "created": (r.get("created_at") or "")[:10],
                                  "description": (r.get("description") or "")[:200]}
    if len(uniq) > rest_budget:
        log("  (no GitHub token: star counts fetched for the first %d repos only; run `gh auth login` for more)"
            % rest_budget)
    return info


# How completely a README documents the implementation. A check counts when a heading matches, or the text matches
# at least twice. Calibrated on 72 repos from a part-segmentation search: most maintained ML repos score 4-5, a stub
# README 0-1, and a project whose docs live elsewhere (e.g. a simulator) 2.
DOCS_CHECKS = (
    ("setup", r"\b(install(ation|ing)?|setup|set up|requirements?|environment|dependenc(y|ies)|getting started|"
              r"pip install|conda (create|env|install)|docker)\b"),
    ("usage", r"\b(usage|inference|demo|quick ?start|examples?|how to (use|run)|predict(ion)?|run(ning)? the)\b"),
    ("training", r"\b(train(ing)?|fine-?tun(e|ing))\b"),
    ("evaluation", r"\b(evaluat(e|ion)|reproduc(e|ing|tion)|benchmark(ing)?|test(ing)? (set|on))\b"),
    ("weights", r"\b(checkpoints?|pre-?trained|weights|model zoo|download)\b|huggingface\.co/"),
)


def docs_score(readme: str | None) -> tuple:
    """-> (0-5, the checks that passed): setup, usage, training, evaluation and released weights. (None, []) when
    there is no README to judge."""
    if not readme:
        return None, []
    headings = "\n".join(line for line in readme.splitlines()
                         if line.lstrip().startswith("#") or re.match(r"\s*<h\d", line, re.I))
    hits = [name for name, pattern in DOCS_CHECKS
            if re.search(pattern, headings, re.I) or len(re.findall(pattern, readme, re.I)) >= 2]
    return len(hits), hits


def github_readme(http: Http, full: str, token: str | None) -> str | None:
    """The repo's README as raw text. Uses the API with a token; without one, raw.githubusercontent.com (no quota)."""
    try:
        if token:
            return http.fetch("https://api.github.com/repos/%s/readme" % full, as_json=False, ttl_days=7,
                              headers={"Authorization": "bearer " + token, "Accept": "application/vnd.github.raw+json"})
        for name in ("README.md", "readme.md", "README.rst"):
            text = http.fetch("https://raw.githubusercontent.com/%s/HEAD/%s" % (full, name), as_json=False, ttl_days=7)
            if text:
                return text
    except HttpFailure as err:
        log("  README lookup failed for %s: %s" % (full, err))
    return None


def add_docs(http: Http, papers: list, token: str | None, min_stars: int = 0) -> None:
    """Score the README of each paper's first verified repo with at least `min_stars` stars (in place)."""
    for p in papers:
        gh = [g for g in ((p.get("code") or {}).get("github") or []) if "verify" not in (g.get("source") or "")]
        if gh and (gh[0].get("stars") or 0) >= min_stars and "docs" not in gh[0]:
            gh[0]["docs"], gh[0]["docs_hits"] = docs_score(github_readme(http, gh[0]["repo"], token))


def hf_artifacts(http: Http, arxiv_id: str, title: str, gh_repos: list) -> list:
    """Hugging Face models / datasets / spaces tagged with this arXiv id that belong to the paper.

    Any model card can cite a paper (Microsoft's Magma-8B cites Set-of-Mark), so an artifact counts only when its id
    carries the paper's short name or its repo's name, or it cites no other arXiv paper. It is verified when its owner
    also owns the paper's verified GitHub repo, and listed as unverified otherwise.
    """
    owners = {r.split("/")[0].lower() for r in gh_repos}
    names = {n for n in [alnum(short_name(title) or "")] + [alnum(r.split("/", 1)[1]) for r in gh_repos] if len(n) >= 3}
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
            cites = {t for t in row.get("tags") or [] if t.startswith("arxiv:")}
            if not (any(n in alnum(rid) for n in names) or cites == {"arxiv:" + arxiv_id}):
                continue  # an artifact that merely cites the paper
            by_owner = rid.split("/")[0].lower() in owners  # same owner as the paper's verified GitHub repo
            prefix = {"models": "", "datasets": "datasets/", "spaces": "spaces/"}[kind]
            found.append({"kind": kind[:-1], "id": rid, "likes": row.get("likes") or 0, "verified": by_owner,
                          "url": "https://huggingface.co/%s%s" % (prefix, rid)})
            break  # best (most liked) artifact of this kind
    found.sort(key=lambda a: -a["likes"])
    return found


def hf_paper(http: Http, arxiv_id: str) -> dict:
    try:
        return http.fetch("https://huggingface.co/api/papers/%s" % arxiv_id, ttl_days=14) or {}
    except HttpFailure:
        return {}


# Paper lists, surveys and course notes, not implementations. Only the plural "papers": a paper's own repo is often
# described as "Official implementation of the paper ...".
LIST_REPO_RE = re.compile(r"awesome|\bpapers\b|paper[- ]?list|reading[- ]?list|survey|curated|collection|must-?read|"
                          r"resources|tutorial|course|notes", re.I)


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
    phrase = " %s " % norm(short_name(title) or "")
    for item in (res or {}).get("items") or []:
        if LIST_REPO_RE.search("%s %s" % (item.get("name"), item.get("description") or "")):
            continue
        overlap = repo_title_overlap(item, title)
        # The README has the exact title; the short name in the repo's name or description makes it the paper's repo
        # (MiniGPT-v2 lives in Vision-CAIR/MiniGPT-4, "Open-sourced codes for MiniGPT-4 and MiniGPT-v2").
        named = len(name) >= 3 and (name in alnum(item.get("name")) or phrase in " %s " % norm(item.get("description")))
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
            # The most-starred repo named exactly like the paper (SAM 2 -> facebookresearch/sam2) is its own; a longer
            # name ("lora-finetune") needs the repo to point at the paper.
            strong = alnum(item.get("name")) == alnum(name) or repo_points_to_paper(item, title, arxiv_id)
            return {"repo": item["full_name"], "url": item["html_url"], "stars": item["stargazers_count"],
                    "archived": item.get("archived", False), "pushed": (item.get("pushed_at") or "")[:10],
                    "description": (item.get("description") or "")[:200],
                    "source": "github-search" if strong else "github-search (verify)"}
    return None


def find_code(http: Http, papers: list, use_search: bool = True, with_hf: bool = True, with_docs: bool = True,
              rest_budget: int = 55) -> None:
    """Adds p['code'] = {'github': [...], 'hf': [...]} to each paper dict (in place).

    Repo sources, most trusted first: the repo linked on the paper's Hugging Face page, links in the arXiv comment or
    abstract (which can also name a dependency, hence the order), then a GitHub name search flagged as unverified.
    `with_hf` adds Hugging Face models / datasets / spaces; `with_docs` scores the first verified repo's README.
    The cheap probe used before selection turns search, Hugging Face artifacts and READMEs off.
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
    info = github_repo_info(http, all_repos, token, rest_budget=rest_budget)
    searches = 0
    for p, aid in zip(papers, arxiv_ids):
        ranked_gh = []
        for prio, r in candidates[p.get("paperId") or p.get("title")]:
            meta_r = info.get(r.lower())
            if not meta_r and r.lower() in hf_stars:  # GitHub not queried (no token / quota): use HF's star count
                meta_r = {"repo": r, "url": "https://github.com/" + r, "stars": hf_stars[r.lower()], "archived": False,
                          "pushed": "", "created": "", "description": None}
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
        verified = [g["repo"] for g in gh if "verify" not in (g.get("source") or "")]
        hf = hf_artifacts(http, aid, p.get("title") or "", verified) if aid and with_hf else []
        p["code"] = {"github": gh[:2], "hf": hf[:2]}
    if with_docs:
        add_docs(http, papers, token)


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

    def top_citers(self, work: str, min_cites: int = 0, since: str | None = None) -> list:
        """The 100 most-cited works citing `work`, optionally only those published on or after `since` (YYYY-MM-DD).
        per_page above 100 is deprecated."""
        flt = "cites:%s,cited_by_count:>%d" % (work, max(min_cites - 1, 0))
        if since:
            flt += ",from_publication_date:%s" % since
        res = self._get("works", filter=flt, sort="cited_by_count:desc",
                        select="id,display_name,publication_year,cited_by_count,doi,ids", per_page=100)
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


# ----------------------------------------------------------------------------- updates

def version_tuple(version: str | None) -> tuple:
    """'v1.10.0' -> (1, 10, 0), so 1.10 sorts after 1.9; anything unparsable -> ()."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", (version or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def fetch_latest_release() -> dict:
    """GitHub's latest published release of this skill (no token needed; 3-second timeout)."""
    repo = os.environ.get("FBS_UPDATE_REPO") or REPO
    req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % repo,
                                 headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_release(cache_dir: str | None, max_age_hours: float = 24, fetch=None) -> dict | None:
    """{"version", "url"} of the latest release, asked at most once per `max_age_hours`: the answer, or the failure, is
    cached in <cache>/update-check.json. Any error (offline, rate limit, no release yet) just means "unknown"."""
    path = os.path.join(cache_dir, "update-check.json") if cache_dir else None
    try:
        cached = load_json(path) if path else None
    except ValueError:
        cached = None
    if cached and time.time() - (cached.get("checked") or 0) < max_age_hours * 3600:
        return cached.get("latest")
    try:
        data = (fetch or fetch_latest_release)()
        latest = {"version": str(data["tag_name"]).lstrip("v"), "url": data.get("html_url")}
    except Exception:  # an update check must never break a run
        latest = None
    if path:
        try:
            save_json(path, {"checked": time.time(), "latest": latest})
        except OSError:
            pass
    return latest


PLUGIN_HINT = ("it was installed as a Claude Code plugin, so update it with `/plugin` (Installed tab, Update now), or "
               "turn on auto-update for its marketplace under /plugin -> Marketplaces")


def installed_as_plugin(path: str) -> bool:
    """True for a copy Claude Code installed from the plugin marketplace: it lives in ~/.claude/plugins/ (or under
    $CLAUDE_CONFIG_DIR/plugins/), which Claude Code manages, so git must not touch it."""
    parts = os.path.realpath(path).split(os.sep)
    return any(a == ".claude" and b == "plugins" for a, b in zip(parts, parts[1:])) or bool(
        os.environ.get("CLAUDE_CONFIG_DIR") and os.path.realpath(path).startswith(
            os.path.join(os.path.realpath(os.environ["CLAUDE_CONFIG_DIR"]), "plugins") + os.sep))


def update_notice(cache_dir: str | None, fetch=None, script: str | None = None) -> str | None:
    """One line when a newer release is out, else None. Checked at most once a day; FBS_NO_UPDATE_CHECK=1 turns it
    off."""
    if os.environ.get("FBS_NO_UPDATE_CHECK"):
        return None
    latest = latest_release(cache_dir, fetch=fetch)
    if not latest or version_tuple(latest["version"]) <= version_tuple(VERSION):
        return None
    script = os.path.realpath(script or __file__)
    how = PLUGIN_HINT if installed_as_plugin(script) else "run: python3 %s update" % script
    return ("citation-snowball %s is out (this copy is %s); what's new: %s. Ask the user before updating, and not "
            "in the middle of a run; %s" % (latest["version"], VERSION, latest.get("url") or
                                            "https://github.com/%s/releases" % REPO, how))


def update_blocker(skill_dir: str) -> str | None:
    """Why `update` must not touch this copy, or None. It must be a git clone, git must be installed, there must be no
    local changes, and it must be on the branch releases land on: a clone left on a merged feature branch would
    otherwise fail with a bare git error. A Claude Code plugin copy is Claude Code's to update (checked first: its
    cache can itself be a git clone)."""
    if installed_as_plugin(skill_dir):
        return "This copy was not updated: " + PLUGIN_HINT + "."
    if not os.path.isdir(os.path.join(skill_dir, ".git")):
        return ("This copy is not a git clone, so it cannot update itself. Download the latest release from "
                "https://github.com/%s/releases and replace %s with it." % (REPO, skill_dir))
    if not shutil.which("git"):
        return "git is not installed; install it, or download the latest release from https://github.com/%s/releases." % REPO

    def git(*argv):
        return subprocess.run(["git", "-C", skill_dir] + list(argv), capture_output=True, text=True).stdout.strip()

    if git("status", "--porcelain", "--untracked-files=no"):
        return ("This copy has local changes, so it was not updated. Commit or stash them first "
                "(git -C %s status)." % skill_dir)
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    default = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1] or "main"
    if branch != default:
        return ("This copy is on %s, but releases land on %s. Switch first: git -C %s checkout %s"
                % ("a detached HEAD" if branch == "HEAD" else "branch " + branch, default, skill_dir, default))
    return None


def cmd_update(args) -> None:
    """Update this copy of the skill to the latest version: `git pull --ff-only` in its folder when it is a git clone
    without local changes. Works the same for Claude Code, Codex and Copilot, which all read the same folder."""
    skill_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    latest = latest_release(writable_dir(args.cache_dir) if args.cache_dir else None, max_age_hours=0)
    print("THIS    citation-snowball %s in %s" % (VERSION, skill_dir))
    print("LATEST  %s" % ("%s (%s)" % (latest["version"], latest.get("url")) if latest else "unknown (GitHub did not answer)"))
    blocker = update_blocker(skill_dir)
    if blocker:
        raise SystemExit(blocker)
    pulled = subprocess.run(["git", "-C", skill_dir, "pull", "--ff-only"], capture_output=True, text=True)
    print((pulled.stdout + pulled.stderr).strip())
    if pulled.returncode != 0:
        raise SystemExit("git pull failed, so nothing was changed. Run `git -C %s pull` yourself to see why." % skill_dir)
    with open(os.path.realpath(__file__), encoding="utf-8") as fh:
        now = re.search(r'^VERSION = "([^"]+)"', fh.read(), re.M)
    print("NOW     citation-snowball %s" % (now.group(1) if now else "?"))


# ----------------------------------------------------------------------------- stages

def workdir_for(args, seed_title: str | None = None, n_seeds: int = 1) -> str:
    if getattr(args, "workdir", None):
        return args.workdir
    if seed_title:
        more = "-and-%d-more" % (n_seeds - 1) if n_seeds > 1 else ""
        return os.path.abspath("fbsearch-" + slugify(seed_title) + more)
    raise SystemExit("--workdir is required (use the folder printed by `backward`).")


def load_seeds(wd: str) -> list:
    seeds = load_json(os.path.join(wd, "seeds.json"))
    if seeds:
        return seeds
    if os.path.exists(os.path.join(wd, "seed.json")):
        raise SystemExit("%s was made by citation-snowball 1.2 or earlier; re-run `backward` (1.3 keeps one folder per "
                         "seed)." % wd)
    raise SystemExit("No seeds.json in %s - run `backward` first." % wd)


def hub_cutoff(seeds: list) -> int:
    """Core papers cited at least this often are hubs (see HUB_MIN_CITATIONS)."""
    return max(HUB_MIN_CITATIONS, 10 * max([s.get("citationCount") or 0 for s in seeds] or [0]))


def cmd_resolve(args) -> None:
    http = Http(args.cache_dir, refresh=args.refresh)
    p = resolve(http, args.seed)
    print(json.dumps({k: p.get(k) for k in ("paperId", "title", "year", "venue", "citationCount", "externalIds", "url")}, indent=1))


def cmd_backward(args) -> None:
    http = Http(args.cache_dir, refresh=args.refresh)
    resolved = []
    for raw in args.seed:
        paper = resolve(http, raw)
        if paper["paperId"] not in {p["paperId"] for p in resolved}:
            resolved.append(paper)
    wd = workdir_for(args, resolved[0].get("title"), len(resolved))
    os.makedirs(wd, exist_ok=True)
    # Seeds already in the workdir stay; running `backward` on one of them again (e.g. with --type) replaces it.
    seeds = load_json(os.path.join(wd, "seeds.json"), []) or []
    summaries = []
    for paper in resolved:
        entry, lines = backward_one(http, args, wd, paper)
        idx = next((i for i, s in enumerate(seeds) if s["paperId"] == entry["paperId"]), None)
        if idx is None:
            seeds.append(entry)
        else:
            seeds[idx] = entry
        summaries.append(lines)
    for s in seeds:  # a seed that cites another seed: that one is a seed, not part of the comparison set
        refs = load_json(os.path.join(wd, s["dir"], "refs.json"), []) or []
        cited = {(e.get("citedPaper") or {}).get("paperId") for e in refs}
        s["cites_seeds"] = sorted(o["key"] for o in seeds if o["paperId"] in cited and o["paperId"] != s["paperId"])
    save_json(os.path.join(wd, "seeds.json"), seeds)
    core = merge_cores(wd, seeds, args.max_core)
    save_json(os.path.join(wd, "core.json"), core)
    print_backward_summary(wd, seeds, summaries, core, http, update_notice(http.cache_dir))


def backward_one(http: Http, args, wd: str, seed: dict) -> tuple:
    """The backward stage for one seed, written to <wd>/seeds/<key>/. -> (seeds.json entry, summary lines)."""
    key = short_id(seed["paperId"])
    sd = os.path.join(wd, "seeds", key)
    os.makedirs(sd, exist_ok=True)
    ptype = args.type or guess_type(seed.get("title"), seed.get("abstract"))
    seed["paper_type"] = ptype
    seed["paper_type_source"] = "user/agent" if args.type else "auto-guess"
    save_json(os.path.join(sd, "seed.json"), seed)
    log("Seed: %s (%s) [%s] cites=%s" % (seed.get("title"), seed.get("year"), seed.get("paperId"), seed.get("citationCount")))

    log("Fetching references ...")
    refs = [e for e in s2_edges(http, seed["paperId"], "references", REF_FIELDS, cap=5000)
            if (e.get("citedPaper") or {}).get("title")]
    log("Fetching full text ...")
    src, html_text, plain, pdf = fetch_fulltext(http, seed, sd)
    digest, recovered = None, 0
    if html_text:
        digest = digest_html(html_text)
        with open(os.path.join(sd, "fulltext.txt"), "w", encoding="utf-8") as fh:
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
        save_json(os.path.join(sd, "digest.json"), digest)
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
    save_json(os.path.join(sd, "refs.json"), refs)
    core = propose_core(refs, digest, ptype, {"max_core": args.max_core},
                        "%s %s" % (seed.get("title") or "", seed.get("abstract") or ""))
    save_json(os.path.join(sd, "core.json"), core)

    entry = {"key": key, "paperId": seed["paperId"], "title": seed.get("title"), "year": seed.get("year"),
             "venue": seed.get("venue"), "publicationDate": seed.get("publicationDate"),
             "citationCount": seed.get("citationCount"), "externalIds": seed.get("externalIds") or {},
             "authors": (seed.get("authors") or [])[:6], "paper_type": ptype,
             "paper_type_source": seed["paper_type_source"], "dir": os.path.join("seeds", key)}
    lines = ["SEED    %s (%s, %s) cites=%s  key=%s  type=%s (%s)" % (
                 seed.get("title"), seed.get("venue") or "-", seed.get("year"), seed.get("citationCount"), key, ptype,
                 seed["paper_type_source"]),
             "  refs  %d references (%d recovered from the bibliography)" % (len(refs), recovered)]
    if digest:
        mapped = sum(1 for b in digest["bib"].values() if "ref" in b)
        lines.append("  text  %s | %d sections, %d tables, %d bib entries (%d matched to references)"
                     % (digest["source"], len(digest["sections"]), len(digest["tables"]), len(digest["bib"]), mapped))
        for t in digest["tables"]:
            if t["bib"]:
                lines.append("  table %-8s [%s] cites %d refs | %s" % (t["id"], t["kind"], len(t["bib"]), t["caption"][:100]))
    elif pdf:
        lines.append("  PDF   %s (no HTML version: table positions unknown, roles come from citation contexts only)" % pdf)
    if os.path.exists(os.path.join(sd, "fulltext.txt")):
        lines.append("  read  %s" % os.path.join(sd, "fulltext.txt"))
    return entry, lines


ROLE_ORDER = {"baseline": 0, "benchmarked_model": 1, "compared_dataset": 2, "compared_benchmark": 3, "related": 4,
              "eval_dataset": 5, "background": 6}


def merge_cores(wd: str, seeds: list, max_core: int) -> list:
    """Merge the per-seed core proposals into one core set: a reference compared by any seed is included, with its
    strongest role, and `seeds` lists the seeds that cite it. Papers that are themselves seeds are left out."""
    seed_ids = {s["paperId"] for s in seeds}
    multi = len(seeds) > 1
    merged, order = {}, []
    for s in seeds:
        tag = label(s.get("title"), 24)
        for row in load_json(os.path.join(wd, s["dir"], "core.json"), []) or []:
            pid = row.get("paperId")
            if pid in seed_ids:
                continue
            evidence = [("[%s] %s" % (tag, e)) if multi else e for e in row.get("evidence") or []]
            key = pid or "%s@%s" % (row["id"], s["key"])
            cur = merged.get(key)
            if cur is None:
                cur = merged[key] = dict(row, evidence=evidence, seeds=[s["key"]])
                if not pid and multi:
                    cur["id"] = "%s@%s" % (row["id"], s["key"][:4])  # "ref12" is only unique within one seed
                order.append(key)
                continue
            cur["seeds"].append(s["key"])
            cur["evidence"] += evidence
            cur["include"] = bool(cur.get("include") or row.get("include"))
            if ROLE_ORDER.get(row["role"], 9) < ROLE_ORDER.get(cur["role"], 9):
                cur["role"] = row["role"]
            cur["score"] = max(cur.get("score") or 0, row.get("score") or 0)
            cur["overlap"] = max(cur.get("overlap") or 0, row.get("overlap") or 0)
    rows = [merged[k] for k in order]
    included = [r for r in rows if r.get("include")]
    if multi and len(included) > max_core:  # papers compared by several seeds first
        included.sort(key=lambda r: (r["role"] not in TABLE_ROLES, -len(r["seeds"]), -(r.get("score") or 0)))
        for row in included[max_core:]:
            row["include"] = False
    rows.sort(key=lambda r: (not r.get("include"), ROLE_ORDER.get(r["role"], 9), -len(r["seeds"]), -(r.get("score") or 0)))
    return rows


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


def core_line(row: dict, multi: bool) -> str:
    return "  %-4s %-9s %-18s %7s  %-5s %s%s | %s" % (
        "y" if row.get("include") else "-", row["id"], row["role"], row["citationCount"], row.get("year") or "",
        ("[%d seeds] " % len(row.get("seeds") or []) if multi and len(row.get("seeds") or []) > 1 else ""),
        (row["title"] or "")[:70], "; ".join(row.get("evidence") or [])[:150])


def print_backward_summary(wd: str, seeds: list, summaries: list, core: list, http: Http,
                           notice: str | None = None) -> None:
    multi = len(seeds) > 1
    out = ["WORKDIR %s" % wd] + (["UPDATE  %s" % notice] if notice else [])
    for lines in summaries:
        out += lines
    if multi:
        out.append("SEEDS   %d in this workdir: %s" % (len(seeds), "; ".join("%s (%s)" % (label(s["title"], 40), s["key"])
                                                                          for s in seeds)))
    if http.s2_busy:
        out.append("NOTE    %d retries against Semantic Scholar's busy keyless pool; an S2_API_KEY makes runs much faster."
                   % http.s2_busy)
    if http.failures:
        out.append("WARNING requests failed even after retries (%s); re-run `backward` when the network is stable."
                   % ", ".join("%s x%d" % kv for kv in sorted(http.failures.items(), key=lambda kv: -kv[1])))
    out.append("")
    out.append("PROPOSED CORE SET (include=y feeds the search; table roles are listed in full in the report's section A)")
    out.append("  inc  id        role               cites   year  title  | evidence")
    shown = 0
    for row in core:
        if row["role"] == "background" and not row.get("include"):
            continue
        if not row.get("include"):
            shown += 1
            if shown > 25:
                continue
        out.append(core_line(row, multi))
    hidden = sum(1 for r in core if r["role"] == "background" and not r.get("include")) + max(0, shown - 25)
    out.append("  (+%d more references not shown, mostly background; see core.json)" % hidden)
    print("\n".join(out))


def cmd_core(args) -> None:
    """Show the core set, or change it: --include/--exclude ids, --role id=role, --add a missing paper."""
    wd = workdir_for(args)
    path = os.path.join(wd, "core.json")
    core = load_json(path)
    if core is None:
        raise SystemExit("No core.json in %s - run `backward` first." % wd)
    multi = len(load_json(os.path.join(wd, "seeds.json"), []) or []) > 1
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
                   "role": role, "score": 0, "overlap": 0, "evidence": ["added by hand"], "include": True, "seeds": []}
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
        row["include"], row["excluded"] = False, True  # also keeps it out of every section of the report
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
    print("CORE    %d papers feed the search; %d of them are compared in tables (listed in full in section A)"
          % (len(inc), sum(1 for r in inc if r["role"] in TABLE_ROLES)))
    for row in (inc if not args.all else core):
        print(core_line(row, multi))


# ----------------------------------------------------------------------------- selection (pure: no network)

SELECT_DEFAULTS = {"min_forward": 8, "max_follow_ups": 15, "max_foundations": 6, "max_latest": 8, "max_also": 20,
                   "expand": "auto", "min_citations": 0, "min_docs": 4, "recent_months": 12, "mid_months": 24,
                   "latest_months": 30, "min_coupling": 1, "min_area": 30}


def selection_config(args, base: dict | None = None) -> dict:
    """Gates and caps from the command line, on top of `base` (the settings `forward` saved) or the defaults."""
    base = base or {}
    cfg = {k: base.get(k, v) for k, v in SELECT_DEFAULTS.items()}
    for k in SELECT_DEFAULTS:
        if getattr(args, k, None) is not None:
            cfg[k] = getattr(args, k)
    gates = {k: tuple(v) for k, v in (base.get("gates") or GATES).items()}
    if getattr(args, "max_forward", None) is not None:  # 1.2 flag
        log("  --max-forward is deprecated; it now sets --max-follow-ups.")
        cfg["max_follow_ups"] = args.max_forward
    if getattr(args, "max_backward", None) is not None:
        log("  --max-backward is ignored: section A1 lists every paper compared in the seed's tables "
            "(drop one with `core --exclude`).")
    if getattr(args, "recent_min_citations", None) is not None:
        log("  --recent-min-citations is deprecated; it now sets --gate seed.recent=%d." % args.recent_min_citations)
        gates["seed.recent"] = (args.recent_min_citations, gates["seed.recent"][1])
    for spec in getattr(args, "gate", None) or []:
        name, _, value = spec.partition("=")
        name = name.strip()
        if name not in GATES:
            raise SystemExit("Unknown gate %r; use one of: %s" % (name, ", ".join(GATES)))
        parts = [x.strip() for x in value.split(",")]
        try:
            cites = None if parts[0] in ("-", "none") else int(parts[0])
            stars = gates[name][1] if len(parts) < 2 else (None if parts[1] in ("", "-", "none") else int(parts[1]))
        except ValueError:
            raise SystemExit("--gate %s: use ROUTE.BRACKET=CITES or CITES,STARS (e.g. seed.recent=10,100; "
                             "'-' closes the bracket or its stars route, e.g. expand.mid=40,-)" % spec)
        gates[name] = (cites, stars)
    cfg["gates"] = gates
    return cfg


def months_since(date: str | None, today: dt.date) -> int | None:
    return months_old({"publicationDate": date}, today) if date else None


def bracket_of(age: int | None, cfg: dict) -> str:
    if age is None or age > cfg["mid_months"]:
        return "old"
    return "recent" if age <= cfg["recent_months"] else "mid"


def best_repo(p: dict) -> dict | None:
    """The first verified GitHub repo found for a paper (by the probe or the full code lookup)."""
    for g in (p.get("code") or {}).get("github") or []:
        if "verify" not in (g.get("source") or ""):
            return g
    return None


def gate(p: dict, route: str, cfg: dict, today: dt.date, core_score: float = 0.0) -> str | None:
    """A short reason when the paper passes its route's influence gate (see GATES), else None.

    route "seed" (B): the "any" bracket, then the paper's age bracket. route "expand" (C): the age bracket only, and
    papers older than the mid bracket also need citations of at least 2 core papers. --min-citations is a hard floor
    that also turns the stars routes off.
    """
    cites = p.get("citationCount") or 0
    floor = cfg.get("min_citations") or 0
    if cites < floor:
        return None
    age = months_old(p, today)
    bracket = bracket_of(age, cfg)
    names = ["seed.any", "seed." + bracket] if route == "seed" else ["expand." + bracket]
    repo = best_repo(p)
    for name in names:
        if name not in cfg["gates"]:
            continue
        min_cites, min_stars = cfg["gates"][name]
        if min_cites is None or (name == "expand.old" and core_score < 2):
            continue  # closed bracket
        if cites >= min_cites:
            return "%s citations" % human(cites) + (" in %d months" % max(age, 1) if age is not None and age <= cfg["mid_months"] else "")
        if not min_stars or not repo or floor:
            continue
        stars, docs = repo.get("stars") or 0, repo.get("docs")
        if docs is None and stars >= 2 * min_stars:
            return "★%s (README not checked)" % human(stars)
        if docs is not None and stars >= min_stars and docs >= cfg["min_docs"]:
            return "★%s, docs %d/5" % (human(stars), docs)
    return None


def sota_score(p: dict, today: dt.date, cfg: dict) -> float:
    """Ranking within section B: citations per month plus half the GitHub stars per month (log scale), +0.5 for
    papers from the last `recent_months`, -0.5 for surveys. Velocity rewards new work that is already taking off."""
    age = months_old(p, today)
    months = max(age if age is not None else 60, 1)
    repo = best_repo(p) or {}
    repo_months = max(months_since(repo.get("created"), today) or months, 1)
    score = (math.log10(1 + (p.get("citationCount") or 0) / months)
             + 0.5 * math.log10(1 + (repo.get("stars") or 0) / repo_months))
    if age is not None and age <= cfg["recent_months"]:
        score += 0.5
    if SURVEY_RE.search(p.get("title") or ""):
        score -= 0.5
    return round(score, 3)


def velocity(p: dict, today: dt.date) -> float:
    age = months_old(p, today)
    return (p.get("citationCount") or 0) / max(age if age is not None else 60, 1)


def balanced_pick(recs: list, n: int, key: str = "lineage") -> list:
    """Fill n slots across groups (recs[key]) in proportion to sqrt(group size), each group best score first. One
    tightly co-cited cluster (e.g. the citers of a single famous baseline) cannot take every slot, and a small group
    cannot pad the list with weak papers."""
    groups = {}
    for rec in sorted(recs, key=lambda r: -r["score"]):
        groups.setdefault(rec.get(key) or "-", []).append(rec)
    weight = {g: math.sqrt(len(items)) for g, items in groups.items()}
    taken = {g: 0 for g in groups}
    out = []
    while len(out) < n and any(groups.values()):
        g = min((g for g in groups if groups[g]), key=lambda g: (taken[g] / weight[g], -groups[g][0]["score"]))
        out.append(groups[g].pop(0))
        taken[g] += 1
    return sorted(out, key=lambda r: -r["score"])


def same_title(a: str | None, b: str | None) -> bool:
    """Same paper: identical titles, or "Name: subtitle" vs "subtitle" (arXiv vs venue version). Stricter than
    title_close, which would merge e.g. "Video Mask Transfiner ..." with "Mask Transfiner ..."."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return any(":" in (x or "") and norm(x.split(":", 1)[1]) == norm(y) for x, y in ((a, b), (b, a)))


def one_survey(recs: list) -> tuple:
    """-> (recs with only the best-ranked survey kept, the other surveys)."""
    kept, extra, seen = [], [], False
    for rec in recs:
        if SURVEY_RE.search(rec.get("title") or ""):
            if seen:
                extra.append(rec)
                continue
            seen = True
        kept.append(rec)
    return kept, extra


def topic_hits(p: dict, keywords: list) -> list:
    """Keyword phrases (normalized) found in the title, or - multi-word phrases only, since single words match
    abstracts too easily - in the abstract."""
    title = " %s " % norm(p.get("title"))
    abstract = " %s " % norm(p.get("abstract"))
    return [k for k in keywords if (" %s " % k) in title or (" " in k and (" %s " % k) in abstract)]


def dedupe_titles(recs: list, against: list = ()) -> list:
    """Semantic Scholar sometimes has two records for one paper (e.g. "Semantic-SAM: X" and the venue version "X").
    Keep the first, best-ranked record of each title, and drop records duplicating one in `against`."""
    kept = []
    for rec in recs:
        if any(same_title(rec.get("title"), other.get("title")) for other in list(against) + kept):
            continue
        kept.append(rec)
    return kept


def cocite_stats(co: dict, today: dt.date) -> dict:
    """paperId -> co-citation numbers for every paper in a pool's `cocitation` block:

    k_b      how many of the seeds and their compared works cite it (out of n_b with a reference list)
    support  how many recent area papers cite it, out of `eligible`: those published after it, the only ones that
             could; share = support / eligible, with eligible floored at COCITE['min_eligible'] so 2 of 3 is not 67%
    idf      specificity, log(corpus size / its citations): COCO and CLIP are cited everywhere, PartImageNet is not
    """
    n_b = co.get("backward_have") or 0
    dates = sorted(co.get("area_dates") or [])
    backward, forward = co.get("backward") or {}, co.get("forward") or {}
    out = {}
    for pid, m in (co.get("meta") or {}).items():
        date = m.get("publicationDate") or ("%s-07-01" % m["year"] if m.get("year") else "")
        eligible = len(dates) - bisect.bisect_right(dates, date) if date else len(dates)
        support = forward.get(pid, 0)
        out[pid] = {"k_b": backward.get(pid, 0), "n_b": n_b, "support": support, "eligible": eligible,
                    "share": support / max(eligible, COCITE["min_eligible"]),
                    "idf": math.log(S2_CORPUS / max(m.get("citationCount") or 0, 1)), "age": months_old(m, today)}
    return out


def coupling_set(co: dict, stats: dict, exclude: set) -> set:
    """The strongest backward co-citations: cited by at least 20% of the seeds and compared works, ranked by that share
    times specificity. A follow-up that cites none of them is probably from another area (CRISP, a real2sim method that
    cites InstructPart once, cites none of its 30)."""
    n_b = co.get("backward_have") or 0
    if not n_b:
        return set()
    k_min = max(2, math.ceil(COCITE["k_share"] * n_b))
    ranked = sorted((pid for pid, s in stats.items() if s["k_b"] >= k_min and pid not in exclude),
                    key=lambda pid: -(stats[pid]["k_b"] / n_b) * stats[pid]["idf"])
    return set(ranked[:COCITE["coupling_top"]])


# "We present LLaVA: ..." / "we introduce PixelLM, an LMM ...": the abstract introduces a named model.
NAMED_INTRO_RE = re.compile(r"\b[Ww]e (?:present|introduce|propose)\s+(?!(?:a|an|the|this|our|two|three|new)\b)[A-Z]")
# "We also present a new large-scale dataset" (RefCOCOg), "the game has produced a dataset" (ReferItGame).
DATA_INTRO_RE = re.compile(r"\bwe (?:also )?(?:introduce|present|propose|release|construct|build|collect|curate|create)\s+"
                           r"(?:a |an |the )?(?:new |novel )?(?:[\w-]+ ){0,3}(?:dataset|data set|benchmark|corpus)\b|"
                           r"\b(?:collected|gathered|produced|crowd-?sourced)\s+(?:a |an )(?:new |novel )?(?:[\w-]+ ){0,3}"
                           r"(?:dataset|benchmark|corpus)\b",
                           re.I)


def is_dataset(p: dict) -> bool:
    """A dataset or benchmark paper, from the first signal that speaks:
    1. the title ("ADE20K Dataset", "PartNet: A Large-Scale Benchmark");
    2. the abstract introducing a named model ("We present LLaVA: ...") -> a model, unless that sentence says dataset;
    3. the abstract introducing a dataset ("We also present a new large-scale dataset" in RefCOCOg's);
    4. at least a third of the seeds and compared works that cite it in a citation sentence use it as data
       (`cited_as_data` = [votes, of]).
    Heuristics miss some (PACO introduces itself by name); the agent fixes those with "kind" in diffs.json."""
    if DATA_TITLE_RE.search(p.get("title") or ""):
        return True
    abstract = p.get("abstract") or ""
    named = NAMED_INTRO_RE.search(abstract)
    if named:  # only the words right after it: abstracts sometimes run sentences together ("understanding.Our ...")
        return bool(re.search(r"\b(datasets?|benchmarks?|corpus)\b", abstract[named.start():named.start() + 160], re.I))
    if DATA_INTRO_RE.search(abstract):
        return True
    votes, of = (p.get("cited_as_data") or [0, 0])[:2]
    return bool(of) and votes * 3 >= of


def merge_twins(rows: list) -> list:
    """One row per dataset. Semantic Scholar keeps journal and conference versions apart ("Scene Parsing through ADE20K
    Dataset" and "Semantic Understanding of Scenes Through the ADE20K Dataset"); dataset rows sharing a name token with
    digits are merged into the better-ranked one, which lists the other under `twins`."""
    out, names = [], []
    for row in rows:
        toks = {t for t in norm(row.get("title")).split() if len(t) >= 5 and re.fullmatch(r"[a-z]+\d+[a-z0-9]*", t)}
        twin = next((o for o, n in zip(out, names) if row.get("kind") == "data" == o.get("kind") and toks & n), None)
        if twin is not None:
            twin.setdefault("twins", []).append(row.get("title"))
            continue
        out.append(row)
        names.append(toks)
    return out


def foundations(co: dict, stats: dict, exclude: set, cfg: dict, kinds: dict | None = None) -> list:
    """Section A2, what the field builds on. Path (a): cited by at least 20% (and 3) of the seeds and their compared
    works, and confirmed by recent area papers citing it too. Path (b): an older paper cited by at least 25% of recent
    area papers (field consensus, e.g. PixelLM and GLaMM for InstructPart). Both need COCITE['min_citations'].
    Ranked by (backward share + forward share) x specificity; datasets and benchmarks, then models and methods, at most
    --max-foundations of each. Without enough recent area papers to confirm anything, path (a) needs no confirmation."""
    n_b = co.get("backward_have") or 0
    can_confirm = (co.get("area_size") or 0) >= COCITE["min_eligible"]
    k_min = max(COCITE["k_min"], math.ceil(COCITE["k_share"] * n_b)) if n_b else None
    rows = []
    for pid, s in stats.items():
        m = co["meta"][pid]
        if pid in exclude or (m.get("citationCount") or 0) < COCITE["min_citations"]:
            continue
        confirmed = s["share"] >= COCITE["confirm_share"] and s["support"] >= COCITE["confirm_support"]
        path_a = k_min is not None and s["k_b"] >= k_min and (confirmed or not can_confirm)
        older = s["age"] is None or s["age"] > cfg["latest_months"]
        path_b = (can_confirm and older and s["share"] >= COCITE["consensus_share"]
                  and s["support"] >= COCITE["confirm_support"])
        if not (path_a or path_b):
            continue
        kind = (kinds or {}).get(short_id(pid)) or ("data" if is_dataset(m) else "model")
        rows.append(dict(m, section="foundation", kind=kind, k_b=s["k_b"], n_b=n_b,
                         support_f=s["support"], eligible=s["eligible"], share_f=round(s["share"], 3),
                         score=round((s["k_b"] / max(n_b, 1) + s["share"]) * s["idf"], 3)))
    rows = merge_twins(dedupe_titles(sorted(rows, key=lambda r: -r["score"])))
    cap = cfg["max_foundations"]
    return [r for r in rows if r["kind"] == "data"][:cap] + [r for r in rows if r["kind"] == "model"][:cap]


def latest_models(co: dict, stats: dict, exclude: set, cfg: dict) -> list:
    """Section C, the latest models to keep an eye on: papers from the last --latest-months cited by at least 15% (and
    4) of the area papers published after them (at least 10 could have). Reaches new models no citation search can:
    Semantic Scholar has no reference list for SAM 3 or Qwen2.5-VL, but 22-25% of InstructPart's recent area cites
    them. Ranked by that share."""
    rows = []
    for pid, s in stats.items():
        if pid in exclude or s["age"] is None or s["age"] > cfg["latest_months"]:
            continue
        if (s["support"] < COCITE["latest_support"] or s["eligible"] < COCITE["min_eligible"]
                or s["share"] < COCITE["latest_share"]):
            continue
        rows.append(dict(co["meta"][pid], section="latest", support_f=s["support"], eligible=s["eligible"],
                         share_f=round(s["share"], 3), score=round(s["share"], 3)))
    return dedupe_titles(sorted(rows, key=lambda r: -r["score"]))[:cfg["max_latest"]]


def select_papers(pool: dict, seeds: list, core: list, cfg: dict, today: dt.date, skip_keys=frozenset(),
                  kinds: dict | None = None) -> dict:
    """Split the screened pool into the report's sections. Pure: no network.

    compared     (A1) included core papers with a table role: every one, no citation bar
    related           included core papers from related work, unless A2 has them: one line in section A
    foundations  (A2) what the seeds and their compared works build on (see foundations())
    follow_ups   (B)  influential papers citing a seed; when fewer than min_forward (or expand=always), also on-topic
                      papers citing the compared works, balanced across them. Every one passes its gate and the
                      same-area check: it cites one of the strongest backward co-citations (coupling_set()). A paper
                      whose reference list is unknown passes on its citation edge (plus the topic match when widening)
    latest       (C)  the latest models the area builds on (see latest_models())
    also              gate passers beyond the caps, and surveys after the first
    `skip_keys` are 8-character keys the agent excluded in diffs.json: they leave every section, so the next
    candidates move up. `kinds` maps keys to "data" / "model" where the agent corrected an A2 classification.
    """
    seed_ids = {s["paperId"] for s in seeds}
    seed_key = {s["paperId"]: s["key"] for s in seeds}
    ref_ids = {c["paperId"] for c in core if c.get("paperId")}
    excluded_ids = {c["paperId"] for c in core if c.get("excluded") and c.get("paperId")}
    included = [c for c in core if c.get("include") and c.get("paperId") and c["paperId"] not in seed_ids
                and short_id(c["paperId"]) not in skip_keys]
    core_ids = {c["paperId"] for c in included}
    hub_cut = hub_cutoff(seeds)
    weight = {c["paperId"]: (0.5 if (c.get("citationCount") or 0) >= hub_cut else 1.0) for c in included}
    cites_of = {c["paperId"]: c.get("citationCount") or 0 for c in included}
    name_of = {c["paperId"]: label(c.get("title")) for c in included}
    compared = [dict(c, section="compared") for c in included if c["role"] in TABLE_ROLES]
    related = [dict(c, section="related") for c in included if c["role"] == "related"]
    compared.sort(key=lambda c: (ROLE_ORDER.get(c["role"], 9), -(c.get("citationCount") or 0)))
    related.sort(key=lambda c: -(c.get("score") or 0))
    compared_ids = {c["paperId"] for c in compared}

    co = pool.get("cocitation") or {}
    stats = cocite_stats(co, today)
    couple = coupling_set(co, stats, exclude=seed_ids)
    keywords = [norm(k) for k in pool["meta"].get("keywords") or [] if norm(k)]
    expanded = bool(pool["meta"].get("expanded"))
    seed_pass, expand_pass = [], []
    for rec in pool["papers"]:
        pid = rec["paperId"]
        if pid in seed_ids or pid in core_ids or pid in excluded_ids or short_id(pid) in skip_keys:
            continue
        cited = set(rec.get("cited") or [])
        if pid in ref_ids and not cited & seed_ids:
            # A seed's own reference is backward work, not new work - unless it cites another seed (with LoRA and DoRA
            # as seeds, QLoRA is a DoRA reference and a LoRA citer).
            continue
        topical = bool(topic_hits(rec, keywords)) or "topic-search" in (rec.get("sources") or [])
        coupling = len(cited & couple)
        same_area = not couple or coupling >= cfg["min_coupling"] or not rec.get("verified")
        core_hits = sorted(cited & core_ids, key=lambda c: cites_of[c])  # most specific (least cited) first
        core_score = sum(weight[c] for c in core_hits)
        row = dict(rec, cites_seeds=sorted(seed_key[s] for s in cited & seed_ids), n_core=len(core_hits),
                   core_score=core_score, topical=topical, coupling=coupling,
                   builds_on=[name_of[c] for c in core_hits if weight[c] == 1.0][:4] or [name_of[c] for c in core_hits][:4])
        if row["cites_seeds"]:
            reason = gate(rec, "seed", cfg, today) if same_area else None
            if reason:
                row.update(route="seed", section="follow_up", gate=reason, score=sota_score(rec, today, cfg),
                           lineage="+".join(row["cites_seeds"]))
                seed_pass.append(row)
            continue
        if not expanded:
            continue
        # Widening must stay on the seed's topic: co-citing two general baselines (Shikra, MiniGPT-v2) is what every
        # new multimodal LLM does, so citing a compared work only counts together with a topic match.
        if not (core_hits and topical and same_area):
            continue
        reason = gate(rec, "expand", cfg, today, core_score)
        if reason:
            row.update(route="expand", section="follow_up", gate=reason, score=sota_score(rec, today, cfg),
                       lineage=next((c for c in core_hits if weight[c] == 1.0), core_hits[0]))
            expand_pass.append(row)

    backward = compared + related
    seed_pass = dedupe_titles(sorted(seed_pass, key=lambda r: -r["score"]), against=backward)
    expand_pass = dedupe_titles(sorted(expand_pass, key=lambda r: -r["score"]), against=backward + seed_pass)
    # One survey per section is useful for a newcomer; more crowd out primary work (they go to "also").
    seed_pass, seed_surveys = one_survey(seed_pass)
    expand_pass, expand_surveys = one_survey(expand_pass)
    seed_kept = balanced_pick(seed_pass, cfg["max_follow_ups"])
    show_expand = expanded and (cfg["expand"] == "always"
                                or (cfg["expand"] == "auto" and len(seed_pass) < cfg["min_forward"]))
    if show_expand and seed_kept and any(SURVEY_RE.search(r.get("title") or "") for r in seed_kept):
        expand_pass, more = one_survey([{"title": "survey"}] + expand_pass)  # the section already has its survey
        expand_pass, expand_surveys = expand_pass[1:], expand_surveys + more
    expand_kept = balanced_pick(expand_pass, max(cfg["max_follow_ups"] - len(seed_kept), 0)) if show_expand else []
    follow_ups = seed_kept + expand_kept
    kept_ids = {r["paperId"] for r in follow_ups}
    also = [r for r in seed_pass + seed_surveys + ((expand_pass + expand_surveys) if show_expand else [])
            if r["paperId"] not in kept_ids]
    for r in also:
        r.pop("abstract", None)  # the report lists these by title only
        r["section"] = "also"
    also.sort(key=lambda r: -r["score"])

    # `core --exclude` keeps a paper out of the comparison set and the citer search (hubs such as SAM), not out of A2
    # and C: a widely co-cited hub is a foundation. EXCLUDE in diffs.json removes it from the report.
    taken = seed_ids | compared_ids | {pid for pid in (co.get("meta") or {}) if short_id(pid) in skip_keys}
    found = dedupe_titles(foundations(co, stats, taken, cfg, kinds), against=compared)
    found_ids = {r["paperId"] for r in found}
    related = [r for r in related if r["paperId"] not in found_ids]
    latest = latest_models(co, stats, taken | found_ids | kept_ids, cfg)
    latest = dedupe_titles(latest, against=backward + found + follow_ups)
    for r in follow_ups:  # a follow-up that recent work already builds on is worth saying so
        s = stats.get(r["paperId"])
        if s and s["share"] >= COCITE["latest_share"] and s["support"] >= COCITE["latest_support"]:
            r.update(share_f=round(s["share"], 3), support_f=s["support"], eligible=s["eligible"])

    if cfg["expand"] == "never":
        why = "turned off (--expand never)"
    elif not show_expand and not expanded and (cfg["expand"] == "always" or len(seed_pass) < cfg["min_forward"]):
        why = "needed, but `forward` did not search it; re-run `forward` (cached) with these settings"
    elif show_expand:
        why = ("forced (--expand always)" if cfg["expand"] == "always" else
               "only %d influential paper%s cite%s the seed%s, fewer than %d"
               % (len(seed_pass), "" if len(seed_pass) == 1 else "s", "s" if len(seed_pass) == 1 else "",
                  "s" if len(seeds) > 1 else "", cfg["min_forward"]))
    else:
        why = "not needed: %d influential papers cite the seed%s" % (len(seed_pass), "s" if len(seeds) > 1 else "")
    return {"compared": compared, "related": related, "foundations": found, "follow_ups": follow_ups,
            "latest": latest, "also": also[:max(cfg["max_also"] * 5, 100)], "n_seed_pass": len(seed_pass),
            "n_expand_pass": len(expand_pass), "expanded": show_expand, "expand_why": why,
            "coupling_set": sorted(couple)}


# ----------------------------------------------------------------------------- forward / select

def probe_code(http: Http, recs: list, cfg: dict, label_: str) -> None:
    """Cheap code probe before selection, for papers below the citation bars: the repo linked on the paper's Hugging
    Face page or in its arXiv comment / abstract, with stars; the README is read only when the stars reach the lowest
    stars bar. Writes rec['code'] like find_code (no GitHub search, no Hugging Face artifacts)."""
    todo = [r for r in recs if "code" not in r and (r.get("externalIds") or {}).get("ArXiv")]
    if not todo:
        return
    log("  probing GitHub stars for %d %s ..." % (len(todo), label_))
    find_code(http, todo, use_search=False, with_hf=False, with_docs=False, rest_budget=0)
    stars = [s for _, s in cfg["gates"].values() if s]
    add_docs(http, todo, github_token(), min_stars=min(stars) if stars else 0)


def backward_cocitation(http: Http, ids: list) -> tuple:
    """Reference lists of the seeds and compared works, with citation sentences -> (Counter: how many of them cite each
    paper, Counter: how many cite it in a sentence with citation sentences, Counter: how many of those sentences read
    as dataset use ("we evaluate on RefCOCO [12]"), how many reference lists Semantic Scholar had). The sentences tell
    datasets from models: RefCOCOg's title, "Generation and Comprehension of Unambiguous Object Descriptions", does
    not."""
    counts, seen, votes, have = collections.Counter(), collections.Counter(), collections.Counter(), 0
    for pid in ids:
        edges = [e for e in s2_edges(http, pid, "references", "contexts,paperId", cap=2000)
                 if (e.get("citedPaper") or {}).get("paperId")]
        if not edges:
            continue
        have += 1
        for e in edges:
            cid = e["citedPaper"]["paperId"]
            counts[cid] += 1
            contexts = e.get("contexts") or []
            if contexts:
                seen[cid] += 1
                votes[cid] += int(sum(1 for c in contexts if DATA_CTX_RE.search(c)) * 2 >= len(contexts))
    return counts, seen, votes, have


def slim(p: dict) -> dict:
    """What `select` needs to know about a co-cited paper (the abstract only to tell datasets from methods)."""
    out = {k: p.get(k) for k in ("paperId", "title", "year", "venue", "publicationDate", "citationCount", "externalIds")}
    out["abstract"] = (p.get("abstract") or "")[:1500]
    return out


def cmd_forward(args) -> None:
    wd = workdir_for(args)
    seeds = load_seeds(wd)
    core = load_json(os.path.join(wd, "core.json"), []) or []
    http = Http(args.cache_dir, refresh=args.refresh)
    today = dt.date.today()
    cfg = selection_config(args)
    keyed = bool(s2_key())
    seed_cap = args.seed_cap or (9999 if keyed else 3000)
    core_cap = args.cap or (3000 if keyed else 1000)
    keywords = [k.strip().lower() for k in (args.keywords or "").split(",") if k.strip()]
    query = args.query or (" | ".join(('"%s"' % k) if " " in k else k for k in keywords) if keywords else None)
    if not keywords:
        keywords = sorted({k for s in seeds for k in keywords_from(s.get("title") or "")})
    kw_norm = [norm(k) for k in keywords if norm(k)]
    seed_ids = [s["paperId"] for s in seeds]
    seed_set = set(seed_ids)
    included = [c for c in core if c.get("include") and c.get("paperId") and c["paperId"] not in seed_set]
    # Every reference of every seed is a target, so later `core` edits can be re-scored by `select` without network.
    targets = seed_set | {c["paperId"] for c in core if c.get("paperId")}
    excluded_ids = {c["paperId"] for c in core if c.get("excluded") and c.get("paperId")}
    hub_cut = hub_cutoff(seeds)
    weight = {c["paperId"]: (0.5 if (c.get("citationCount") or 0) >= hub_cut else 1.0) for c in included}
    core_ids = set(weight)
    pool = {}  # paperId -> candidate record

    def add(paper: dict, source: str, cited: str | None = None, influential: bool = False):
        pid = paper.get("paperId")
        if not pid or pid in seed_set or pid in excluded_ids:
            return None
        rec = pool.get(pid)
        if rec is None:
            rec = pool[pid] = {k: paper.get(k) for k in ("paperId", "title", "year", "venue", "publicationDate",
                                                          "citationCount", "externalIds")}
            rec.update(sources=set(), cited=set(), influential=0, verified=False)
        else:
            for k in ("title", "year", "venue", "publicationDate", "externalIds"):
                if rec.get(k) is None and paper.get(k) is not None:
                    rec[k] = paper[k]
            if (paper.get("citationCount") or 0) > (rec.get("citationCount") or 0):
                rec["citationCount"] = paper["citationCount"]
        rec["sources"].add(source)
        if cited and cited != pid:  # Semantic Scholar occasionally lists a paper among its own citers
            rec["cited"].add(cited)
        rec["influential"] += int(bool(influential))
        return rec

    oa = OpenAlex(http, args.openalex_budget) if args.openalex_budget > 0 else None
    oa_wanted = {}  # Semantic Scholar id -> (OpenAlex title, set of cited paperIds)

    def queue_openalex(paper: dict, min_cites: int, since: str | None = None) -> None:
        wid = oa.work_id(paper)
        for w in (oa.top_citers(wid, min_cites=min_cites, since=since) if wid else []):
            for sid in OpenAlex.s2_ids(w):
                oa_wanted.setdefault(sid, (w.get("display_name"), set()))[1].add(paper["paperId"])

    def resolve_openalex(source: str) -> int:
        found = s2_batch(http, list(oa_wanted), fields=PAPER_FIELDS)
        matched = 0
        for sid, p in found.items():
            title, cited_ids = oa_wanted[sid]
            if title_close(p.get("title"), title):  # drops OpenAlex mis-merges
                for cid in cited_ids:
                    add(p, source, cid)
                matched += 1
        oa_wanted.clear()
        return matched

    def title_topical(r: dict) -> bool:
        return bool(topic_hits(r, kw_norm)) or "topic-search" in r["sources"]

    # Backward co-citation: what the seeds and their compared works all cite (section A2's first path). The strongest
    # candidates join the verification targets, so `select` can tell offline which of them each follow-up cites.
    backward_set = seed_ids + [c["paperId"] for c in included]
    log("Co-citation: reading the reference lists of the seed%s and %d compared works ..."
        % ("s" if len(seeds) > 1 else "", len(included)))
    bw_counts, bw_seen, bw_votes, bw_have = backward_cocitation(http, backward_set)
    bw_ids = [pid for pid, k in bw_counts.most_common(400) if k >= 2 and pid not in seed_set]
    co_meta = {pid: slim(p) for pid, p in s2_batch(http, bw_ids, fields=PAPER_FIELDS + ",abstract", chunk=500).items()}
    for pid, m in co_meta.items():  # how the seeds and compared works cite it: as a dataset, or not
        m["cited_as_data"] = [bw_votes.get(pid, 0), bw_seen.get(pid, 0)]
    ranked = sorted(co_meta, key=lambda pid: -bw_counts[pid] * math.log(S2_CORPUS / max(co_meta[pid]["citationCount"] or 0, 1)))
    targets |= set(ranked[:200])
    log("  %d of %d reference lists found; %d papers are cited by at least 2 of them"
        % (bw_have, len(backward_set), len(bw_ids)))

    def cocitation_block(forward: dict, area_dates: list) -> dict:
        return {"backward_set": backward_set, "backward_have": bw_have,
                "backward": {pid: bw_counts[pid] for pid in co_meta if bw_counts.get(pid, 0) >= 2},
                "area_size": len(area_dates), "area_dates": sorted(area_dates), "forward": forward, "meta": co_meta}

    # Route B: everything that cites a seed (complete unless a seed has more than seed_cap citers).
    log("Route B: papers citing the %s ..." % ("seed" if len(seeds) == 1 else "%d seeds" % len(seeds)))
    n_seed_citers = 0
    for s in seeds:
        edges = s2_edges(http, s["paperId"], "citations", CIT_FIELDS, cap=seed_cap)
        for e in edges:
            add(e.get("citingPaper") or {}, "seed-citers", s["paperId"], e.get("isInfluential"))
        n_seed_citers += len(edges)
        partial = (s.get("citationCount") or 0) > len(edges)
        log("  %-60s %6d citers%s" % ((s.get("title") or "")[:60], len(edges), " (newest only)" if partial else ""))
        if oa and partial:
            queue_openalex(s, min_cites=cfg["gates"]["seed.recent"][0] or 0)
    if oa_wanted:
        log("  OpenAlex: %d most-cited citers matched in Semantic Scholar" % resolve_openalex("openalex"))
    b_recs = list(pool.values())
    if not args.no_code:
        contenders = [r for r in b_recs if not gate(r, "seed", cfg, today)]
        contenders.sort(key=lambda r: -velocity(r, today))
        probe_code(http, contenders[:args.probe_cap], cfg, "seed citers below the citation bars")
    # The same-area check needs each passer's reference list, so check those now.
    b_pass = sorted((r for r in b_recs if gate(r, "seed", cfg, today)), key=lambda r: -(r.get("citationCount") or 0))
    n_verified, no_reflist = verify_refs(http, pool, [r["paperId"] for r in b_pass[:args.verify_cap]], targets)
    # Count B exactly as `select` will (same checks, dedupe, survey cap, agent exclusions), so both agree on widening.
    view = {"meta": {"keywords": [], "expanded": False}, "cocitation": cocitation_block({}, []),
            "papers": [dict(r, cited=sorted(r["cited"]), sources=sorted(r["sources"])) for r in b_recs]}
    skip = {k for k, d in load_diffs(wd).items() if d["exclude"]}
    n_b = select_papers(view, seeds, core, cfg, today, skip_keys=skip)["n_seed_pass"]
    n_recent = sum(1 for r in b_recs if (months_old(r, today) if months_old(r, today) is not None else 999)
                   <= cfg["latest_months"])
    expand = args.expand == "always" or (args.expand == "auto" and (n_b < cfg["min_forward"]
                                                                    or n_recent < cfg["min_area"]))
    if not included and expand:
        log("  No core papers are included, so there is nothing to widen to (see `core`).")
        expand = False
    why = ("--expand always" if args.expand == "always" else
           "fewer than %d pass" % cfg["min_forward"] if n_b < cfg["min_forward"] else
           "only %d recent papers cite the seed, too few to see what the area builds on" % n_recent)
    log("  %d papers citing the seed%s pass the influence gate and the same-area check%s"
        % (n_b, "s" if len(seeds) > 1 else "", "; widening to the compared works' citers (%s)" % why if expand else ""))

    n_topic = 0
    since = (today - dt.timedelta(days=int(max(cfg["mid_months"], cfg["latest_months"]) * 30.44))).isoformat()
    if expand:
        # Newest citers of each non-hub compared work: newest-first order is exactly what a SOTA search wants.
        log("Widening: recent papers citing the compared works (%d core papers) ..." % len(included))
        for c in included:
            if weight[c["paperId"]] < 1:
                log("  %-60s skipped: hub (%s citations), its citers are mostly unrelated"
                    % ((c["title"] or "")[:60], human(c.get("citationCount"))))
                continue
            edges = s2_edges(http, c["paperId"], "citations", CIT_FIELDS, cap=core_cap)
            for e in edges:
                add(e.get("citingPaper") or {}, "core-citers", c["paperId"], e.get("isInfluential"))
            partial = (c.get("citationCount") or 0) > len(edges)
            if oa and partial:
                queue_openalex(c, min_cites=cfg["gates"]["expand.recent"][0] or 0, since=since)
            log("  %-60s %6d citers%s" % ((c["title"] or "")[:60], len(edges), " (newest only)" if partial else ""))
        # Citation-sorted topic search over the same window.
        if query and not args.no_topic_search:
            hits = s2_bulk_search(http, query, min_citations=5, limit=args.topic_limit, since=since)
            for p in hits:
                add(p, "topic-search")
            n_topic = len(hits)
            log("  topic search %s since %s: %d papers with at least 5 citations" % (query, since, n_topic))
        elif not query:
            log("  No --query/--keywords given, so no topic search.")
        # OpenAlex's most-cited recent citers of partially fetched compared works.
        if oa_wanted:
            log("  OpenAlex: %d most-cited recent citers matched in Semantic Scholar" % resolve_openalex("openalex"))

    def c_candidate(r: dict) -> bool:  # could be a widening row: cites a compared work (or is unverified), not a seed
        return not (r["cited"] & seed_set) and bool(r["cited"] & core_ids or not r["verified"])

    if expand:
        # Verify reference lists: which seeds, compared works and foundations each plausible candidate cites.
        to_verify = [pid for pid, r in pool.items() if not r["verified"] and c_candidate(r)
                     and gate(r, "expand", cfg, today, core_score=99)]
        to_verify.sort(key=lambda pid: -(pool[pid].get("citationCount") or 0))
        a, b = verify_refs(http, pool, to_verify[:args.verify_cap], targets)
        n_verified, no_reflist = n_verified + a, no_reflist + b
        # Abstracts let `select` judge the topic from more than the title: fetched for papers that pass the gate on
        # citations and for the fastest-rising ones below it (stars candidates), kept only where a keyword phrase hits.
        passing = [r for r in pool.values() if c_candidate(r) and not title_topical(r) and gate(r, "expand", cfg, today, 99)]
        rising = [r for r in pool.values() if c_candidate(r) and not title_topical(r) and not gate(r, "expand", cfg, today, 99)
                  and bracket_of(months_old(r, today), cfg) != "old"]
        rising.sort(key=lambda r: -velocity(r, today))
        need = [r["paperId"] for r in passing + rising[:3 * args.probe_cap]]
        if need:
            log("  reading %d abstracts to check the topic beyond titles ..." % len(need))
        for pid, d in s2_batch(http, need, fields="paperId,abstract", chunk=500).items():
            text = (d.get("abstract") or "")[:2000]
            if topic_hits({"abstract": text}, kw_norm):
                pool[pid]["abstract"] = text
    if expand and not args.no_code:  # stars probe for on-topic widening candidates below the citation bars
        contenders = [r for r in pool.values() if c_candidate(r) and "code" not in r and title_topical(r)
                      and bracket_of(months_old(r, today), cfg) != "old" and not gate(r, "expand", cfg, today, 99)]
        contenders.sort(key=lambda r: -velocity(r, today))
        probe_code(http, contenders[:args.probe_cap], cfg, "recent widening candidates below the citation bars")
        starred = [r["paperId"] for r in contenders[:args.probe_cap]
                   if not r["verified"] and gate(r, "expand", cfg, today, 99)]
        a, b = verify_refs(http, pool, starred, targets)
        n_verified, no_reflist = n_verified + a, no_reflist + b
    if not args.no_code:  # papers that pass on citations need their stars too: the SOTA score ranks with them
        passers = [r for r in pool.values() if "code" not in r and (
            gate(r, "seed", cfg, today) if r["cited"] & seed_set else
            expand and c_candidate(r) and title_topical(r) and gate(r, "expand", cfg, today, 99))]
        passers.sort(key=lambda r: -velocity(r, today))
        probe_code(http, passers[:args.probe_cap], cfg, "papers above the citation bars")

    # Forward co-citation: what the area's recent papers cite (A2's second path and section C). It reaches new models
    # that no citation search can: Semantic Scholar has no reference list for SAM 3 or Qwen2.5-VL, so they never show
    # up as citers of anything, but recent papers in the area cite them.
    def in_area(r: dict) -> bool:
        age = months_old(r, today)
        if age is None or age > cfg["latest_months"]:
            return False
        return bool(r["cited"] & seed_set) or (bool(r["cited"] & core_ids) and title_topical(r))

    area = sorted((r for r in pool.values() if in_area(r)), key=lambda r: r.get("publicationDate") or "", reverse=True)
    area = area[:args.area_cap or (1000 if keyed else 400)]
    log("Co-citation: reading the reference lists of %d recent papers in the area ..." % len(area))
    fw_reflists = s2_reference_ids(http, [r["paperId"] for r in area])
    fw_counts, area_dates = collections.Counter(), []
    for r in area:
        refs = fw_reflists.get(r["paperId"])
        date = r.get("publicationDate") or ("%s-07-01" % r["year"] if r.get("year") else "")
        if refs and date:
            fw_counts.update(refs)
            area_dates.append(date)
    fw_top = [pid for pid, n in fw_counts.most_common(300) if n >= 2 and pid not in seed_set]
    more = [pid for pid in fw_top if pid not in co_meta]
    co_meta.update({pid: slim(p) for pid, p in s2_batch(http, more, fields=PAPER_FIELDS + ",abstract", chunk=500).items()})
    cocitation = cocitation_block({pid: fw_counts[pid] for pid in fw_top if pid in co_meta}, area_dates)
    log("  %d of them have reference lists; %d papers are cited by at least 2" % (len(area_dates), len(fw_top)))

    meta = {"generated": today.isoformat(), "version": VERSION, "seed_keys": [s["key"] for s in seeds],
            "keywords": keywords, "query": query,
            "since": since if expand else None, "seed_cap": seed_cap, "cap": core_cap, "seed_citers": n_seed_citers,
            "core_used": len(included), "hubs_skipped": sum(1 for w in weight.values() if w < 1) if expand else 0,
            "topic_hits": n_topic, "pool": len(pool), "verified": n_verified, "no_reference_list": no_reflist,
            "probed": sum(1 for r in pool.values() if "code" in r), "github_token": bool(github_token()),
            "openalex_calls": oa.used if oa else 0, "expanded": expand, "expand_mode": args.expand,
            "settings": {k: v for k, v in cfg.items()}, "s2_busy_retries": http.s2_busy, "failures": http.failures,
            "http_calls": http.counts}
    meta["settings"]["gates"] = {k: list(v) for k, v in cfg["gates"].items()}
    papers = []
    for r in pool.values():  # keep what a later `select` could use: anything connected to a seed or its references
        if not r["cited"] and "topic-search" not in r["sources"]:
            continue
        papers.append(dict(r, sources=sorted(r["sources"]), cited=sorted(r["cited"])))
    out = {"meta": meta, "papers": papers, "cocitation": cocitation}
    save_json(os.path.join(wd, "pool.json"), out)
    log("Saved %d screened papers to pool.json (%d reference lists checked, %d without one)."
        % (len(papers), n_verified, no_reflist))
    run_select(wd, seeds, core, out, cfg, http, args)


def verify_refs(http: Http, pool: dict, ids: list, targets: set) -> tuple:
    """Fetch the reference lists of `ids` and record which targets each cites. -> (verified, no reference list)."""
    ids = [pid for pid in ids if not pool[pid]["verified"]]
    if not ids:
        return 0, 0
    log("Checking the reference lists of %d candidates ..." % len(ids))
    # Candidates with no observed citation edge (topic search / OpenAlex hits) depend entirely on this check.
    reflists = s2_reference_ids(http, ids, deep={pid for pid in ids if not pool[pid]["cited"]})
    missing = 0
    for pid in ids:
        refs = reflists.get(pid)
        if refs is None:
            missing += 1
            continue
        pool[pid]["cited"] |= refs & targets
        pool[pid]["verified"] = True
    return len(ids) - missing, missing


def cmd_select(args) -> None:
    wd = workdir_for(args)
    seeds = load_seeds(wd)
    core = load_json(os.path.join(wd, "core.json"), []) or []
    pool = load_json(os.path.join(wd, "pool.json"))
    if not pool:
        raise SystemExit("No pool.json in %s - run `forward` first." % wd)
    cfg = selection_config(args, base=pool["meta"].get("settings"))
    pool_core = {pid for r in pool["papers"] for pid in r.get("cited") or []}
    new = [c for c in core if c.get("include") and c.get("paperId") and "added by hand" in (c.get("evidence") or [])
           and c["paperId"] not in pool_core]
    if new:
        log("  NOTE: %d core paper(s) added with `core --add` after `forward` (%s); re-run `forward` to search their "
            "citers." % (len(new), ", ".join(c["id"] for c in new)))
    run_select(wd, seeds, core, pool, cfg, Http(args.cache_dir), args)


def run_select(wd: str, seeds: list, core: list, pool: dict, cfg: dict, http: Http, args) -> None:
    """Select, then fetch abstracts, BibTeX and code for the kept papers, and write candidates.json + briefs.md."""
    today = dt.date.today()
    diffs = load_diffs(wd)
    skip = {k for k, d in diffs.items() if d["exclude"]}
    kinds = {k: d["kind"] for k, d in diffs.items() if d.get("kind")}
    sel = select_papers(pool, seeds, core, cfg, today, skip_keys=skip, kinds=kinds)
    kept = sel["compared"] + sel["foundations"] + sel["follow_ups"] + sel["latest"]
    details = s2_batch(http, [r["paperId"] for r in kept + sel["related"]], fields=KEPT_FIELDS)
    for r in kept + sel["related"]:
        d = details.get(r["paperId"]) or {}
        for k, v in d.items():
            if k != "code" and v is not None:
                r[k] = v
    if not args.no_code:
        log("Looking up code for %d kept papers (Hugging Face, arXiv, GitHub) ..." % len(kept))
        find_code(http, kept, use_search=not args.no_github_search)
    for r in sel["follow_ups"]:  # the full lookup can change a probed repo's stars or README score
        r["gate"] = gate(r, r["route"], cfg, today, r.get("core_score") or 0) or r["gate"]
        r["score"] = sota_score(r, today, cfg)
    out = [paper_record(r, today) for r in kept + sel["related"] + sel["also"]]
    co = pool.get("cocitation") or {}
    meta = dict(pool["meta"], selected=today.isoformat(), selection={k: v for k, v in cfg.items()},
                expand_shown=sel["expanded"], expand_why=sel["expand_why"], n_seed_pass=sel["n_seed_pass"],
                n_expand_pass=sel["n_expand_pass"], excluded_by_review=sorted(skip),
                cocitation={"backward_set": len(co.get("backward_set") or []), "backward_have": co.get("backward_have") or 0,
                            "area_size": co.get("area_size") or 0, "coupling_set": len(sel["coupling_set"])})
    meta["selection"]["gates"] = {k: list(v) for k, v in cfg["gates"].items()}
    save_json(os.path.join(wd, "candidates.json"), {"meta": meta, "papers": out})
    write_briefs(wd, seeds, out)
    print_select_summary(wd, out, meta, diffs)


def paper_record(r: dict, today: dt.date) -> dict:
    tldr = r.get("tldr")
    bib = ((r.get("citationStyles") or {}).get("bibtex") if isinstance(r.get("citationStyles"), dict) else None)
    return {
        "key": short_id(r.get("paperId")), "paperId": r.get("paperId"), "title": r.get("title"), "year": r.get("year"),
        "venue": r.get("venue"), "publicationDate": r.get("publicationDate"), "citationCount": r.get("citationCount"),
        "influentialCitationCount": r.get("influentialCitationCount"), "externalIds": r.get("externalIds"),
        "authors": [a.get("name") if isinstance(a, dict) else a for a in (r.get("authors") or [])][:6],
        "tldr": tldr.get("text") if isinstance(tldr, dict) else tldr, "abstract": r.get("abstract"),
        "publicationTypes": r.get("publicationTypes"), "link": paper_link(r), "bibtex": bib,
        "section": r["section"], "route": r.get("route"), "role": r.get("role"), "kind": r.get("kind"),
        "seeds": r.get("seeds") or r.get("cites_seeds") or [],
        "gate": r.get("gate"), "score": r.get("score"), "age_months": months_old(r, today),
        "builds_on": r.get("builds_on") or [], "lineage": r.get("lineage"), "n_core": r.get("n_core"),
        "coupling": r.get("coupling"), "k_b": r.get("k_b"), "n_b": r.get("n_b"), "support_f": r.get("support_f"),
        "eligible": r.get("eligible"), "share_f": r.get("share_f"), "twins": r.get("twins") or [],
        "found_via": r.get("sources") or (["co-citation"] if r["section"] in ("foundation", "latest") else ["backward"]),
        "verified": r.get("verified"), "evidence": r.get("evidence") or [], "code": r.get("code"),
    }


SECTION_LABEL = {"compared": "A1. compared in the seed", "foundation": "A2. foundation", "follow_up": "B. follow-up",
                 "latest": "C. latest model", "related": "related work", "also": "also qualified"}
SECTION_LETTER = {"compared": "A1", "foundation": "A2", "follow_up": "B", "latest": "C"}


def pct(share) -> str:
    return "%d%%" % round(100 * (share or 0))


def cocite_note(p: dict) -> str:
    """Why a paper is a foundation: "cited by 6 of 15 (seed + compared works) · 27% of recent area papers"."""
    parts = []
    if (p.get("k_b") or 0) >= 2:
        parts.append("cited by %d of %d (seed + compared works)" % (p["k_b"], p.get("n_b") or 0))
    if p.get("share_f"):
        parts.append("%s of recent area papers" % pct(p["share_f"]))
    return " · ".join(parts)


def latest_note(p: dict) -> str:
    """Why a paper is a latest model: "cited by 22% of the area's papers since it appeared (20 of 89)"."""
    return "cited by %s of the area's papers since it appeared (%d of %d)" % (
        pct(p.get("share_f")), p.get("support_f") or 0, p.get("eligible") or 0)


def why_kept(p: dict) -> str:
    sec = p["section"]
    if sec == "compared":
        return ROLE_LABEL.get(p.get("role"), p.get("role") or "")
    if sec == "foundation":
        return cocite_note(p)
    if sec == "latest":
        return latest_note(p)
    base = "cites the seed" if p.get("route") == "seed" else "builds on %s" % (", ".join(p.get("builds_on") or []) or "-")
    extra = "; also built on by %s of recent area papers" % pct(p["share_f"]) if p.get("share_f") else ""
    return "%s; %s%s" % (base, p.get("gate"), extra)


def write_briefs(wd: str, seeds: list, papers: list) -> None:
    lines = ["# Briefs for writing diffs.json", ""]
    for s in seeds:
        sp = load_json(os.path.join(wd, s["dir"], "seed.json"), {}) or {}
        lines += ["Seed: %s (%s)" % (s.get("title"), s.get("year")), "",
                  "Seed abstract: %s" % (sp.get("abstract") or "(none)"), ""]
    lines += ["Write diffs.json as {\"<key>\": \"one sentence: why read it - what it contributes and how it differs from "
              "the seed\"}. Use \"EXCLUDE: <reason>\" to drop an off-topic paper (then re-run `select` to backfill), and "
              "{\"diff\": \"...\", \"pick\": 1} to put a paper in \"Read these first\".", ""]
    for p in papers:
        if p["section"] not in SECTION_LETTER:
            continue
        repo = best_repo(p)
        code = ("%s ★%s%s" % (repo["repo"], human(repo.get("stars")),
                              ", docs %s/5" % repo["docs"] if repo.get("docs") is not None else "")) if repo else "no code found"
        why = why_kept(p)
        if p["section"] == "compared":
            why = "%s (%s)" % (why, "; ".join(p.get("evidence") or [])[:160])
        lines.append("## %s | %s (%s) | %s cites | %s | %s | %s" % (
            p["key"], p["title"], p.get("year"), p.get("citationCount"), SECTION_LABEL[p["section"]], why, code))
        lines.append(p.get("tldr") or (p.get("abstract") or "")[:700] or "(no abstract available - judge from the title or skim the paper)")
        lines.append("")
    with open(os.path.join(wd, "briefs.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def code_brief(p: dict) -> str:
    repo = best_repo(p)
    if repo:
        return "%s ★%s%s" % (repo["repo"], human(repo.get("stars")), " d%s" % repo["docs"] if repo.get("docs") is not None else "")
    hf = [h for h in (p.get("code") or {}).get("hf") or [] if h.get("verified", True)]
    return ("HF %s ♥%s" % (hf[0]["id"], hf[0]["likes"])) if hf else "-"


def print_select_summary(wd: str, papers: list, meta: dict, diffs: dict) -> None:
    by = {}
    for p in papers:
        by.setdefault(p["section"], []).append(p)
    kept = [p for p in papers if p["section"] in SECTION_LETTER]
    co = meta.get("cocitation") or {}
    widened = sum(1 for p in by.get("follow_up", []) if p.get("route") == "expand")
    print("WORKDIR %s" % wd)
    print("POOL    %d papers screened (%d citing the seeds, %d from the topic search); %d reference lists checked%s"
          % (meta["pool"], meta["seed_citers"], meta["topic_hits"], meta["verified"],
             (" (%d had none)" % meta["no_reference_list"]) if meta.get("no_reference_list") else ""))
    print("COCITE  backward: %d of %d reference lists (seed%s + compared works); forward: %d recent area papers"
          % (co.get("backward_have", 0), co.get("backward_set", 0), "s" if len(meta.get("seed_keys") or []) > 1 else "",
             co.get("area_size", 0)))
    print("KEPT    %d papers: A1 %d compared · A2 %d foundations · B %d follow-ups%s · C %d latest models; %d also qualified"
          % (len(kept), len(by.get("compared", [])), len(by.get("foundation", [])), len(by.get("follow_up", [])),
             " (%d by widening)" % widened if widened else "", len(by.get("latest", [])), len(by.get("also", []))))
    print("WIDEN   %s: %s" % ("yes" if meta["expand_shown"] else "no", meta["expand_why"]))
    if meta.get("s2_busy_retries"):
        print("NOTE    %d retries against Semantic Scholar's busy keyless pool; an S2_API_KEY makes runs much faster."
              % meta["s2_busy_retries"])
    if not meta.get("github_token"):
        print("NOTE    no GitHub token: stars come from Hugging Face's counts and READMEs from raw.githubusercontent.com.")
    if meta.get("failures"):
        print("WARNING requests failed even after retries (%s), so results, especially code links, may be incomplete. "
              "Re-run `forward` when the network is stable; cached responses are reused."
              % ", ".join("%s x%d" % kv for kv in sorted(meta["failures"].items(), key=lambda kv: -kv[1])))
    missing = [p["key"] for p in kept if p["key"] not in diffs]
    print("NEXT    read %s and write %s%s, then run `report`" % (
        os.path.join(wd, "briefs.md"), os.path.join(wd, "diffs.json"),
        " (%d keys still need a sentence: %s)" % (len(missing), ",".join(missing)) if missing and diffs else ""))
    print("")
    print("  key       sec  cites   year  code                          title | why kept")
    for sec in ("compared", "foundation", "follow_up", "latest"):
        for p in by.get(sec, []):
            print("  %-9s %-4s %6s  %-5s %-29s %s | %s" % (p["key"], SECTION_LETTER[sec], p.get("citationCount"),
                                                          p.get("year") or "", code_brief(p)[:29], (p["title"] or "")[:60],
                                                          why_kept(p)[:80]))


def load_diffs(wd: str) -> dict:
    """diffs.json -> {key: {"text", "exclude", "reason", "pick", "kind"}}. Values are a sentence, "EXCLUDE: reason",
    or {"diff": "...", "pick": true | <rank>, "exclude": true | "reason", "kind": "data" | "model"}."""
    raw = load_json(os.path.join(wd, "diffs.json"), {}) or {}
    out = {}
    for order, (k, v) in enumerate(raw.items()):
        if isinstance(v, dict):
            text = (v.get("diff") or v.get("why") or "").strip()
            ex = v.get("exclude")
            pick = v.get("pick")
            pick = None if pick in (None, False) else (order + 1000 if pick is True else float(pick))
            kind = {"data": "data", "dataset": "data", "benchmark": "data", "model": "model", "method": "model"}.get(
                str(v.get("kind") or "").lower())
            out[k[:8]] = {"text": text, "exclude": bool(ex), "reason": ex if isinstance(ex, str) else text, "pick": pick,
                          "kind": kind}
        else:
            text = (v or "").strip()
            ex = text.upper().startswith("EXCLUDE")
            out[k[:8]] = {"text": "" if ex else text, "exclude": ex, "reason": text.split(":", 1)[-1].strip(), "pick": None,
                          "kind": None}
    return out


# ----------------------------------------------------------------------------- report

def md_escape(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("|", "\\|").strip()


def code_cell(code: dict | None) -> tuple:
    """-> (verified links md, their stars/likes md, popularity for tie-breaking, unverified links md).

    Only verified links count as code: repos linked from the paper or its Hugging Face page, strong GitHub-search
    matches, and Hugging Face artifacts owned by the same account as that repo.
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


def code_md(p: dict) -> str:
    """One compact cell: the best verified repo with stars and README docs score, a Hugging Face artifact, or an
    unverified candidate marked as such."""
    code = p.get("code") or {}
    repo = best_repo(p)
    parts = []
    if repo:
        docs = " · docs %d/5" % repo["docs"] if repo.get("docs") is not None else ""
        parts.append("[%s](%s) ★%s%s" % (repo["repo"], repo["url"], human(repo.get("stars")), docs))
    hf = [h for h in code.get("hf") or [] if h.get("verified", True)]
    if hf and (not repo or len(parts) < 2):
        parts.append("🤗 [%s](%s) ♥%s" % (hf[0]["id"], hf[0]["url"], human(hf[0]["likes"])))
    if not parts:
        maybe = [g for g in code.get("github") or [] if "verify" in (g.get("source") or "")]
        if maybe:
            return "maybe [%s](%s) ★%s (unverified)" % (maybe[0]["repo"], maybe[0]["url"], human(maybe[0]["stars"]))
        return "—"
    return "<br>".join(parts)


def paper_md(p: dict) -> str:
    """Title link with the paper's short name in bold ("**LoRA**: Low-Rank ...") so a column scans quickly."""
    title = md_escape(p.get("title"))
    name = short_name(p.get("title") or "")
    if name and title.startswith(md_escape(name)):
        title = "**%s**%s" % (md_escape(name), title[len(md_escape(name)):])
    return "[%s](%s)" % (title, p.get("link") or "")


def cites_md(p: dict) -> str:
    """Semantic Scholar's count, linked to a Google Scholar search the reader can click (never queried by the script)."""
    return "[%s](%s)" % (human(p.get("citationCount") or 0), scholar_link(p.get("title")))


VENUE_SHORT = {  # normalized Semantic Scholar venue (without a leading "IEEE" / "IEEE/CVF") -> short name
    "annual meeting of the association for computational linguistics": "ACL",
    "conference on empirical methods in natural language processing": "EMNLP",
    "north american chapter of the association for computational linguistics": "NAACL",
    "computer vision and pattern recognition": "CVPR",
    "international conference on computer vision": "ICCV",
    "european conference on computer vision": "ECCV",
    "neural information processing systems": "NeurIPS",
    "international conference on learning representations": "ICLR",
    "international conference on machine learning": "ICML",
    "aaai conference on artificial intelligence": "AAAI",
    "international joint conference on artificial intelligence": "IJCAI",
    "winter conference on applications of computer vision": "WACV",
    "international conference on robotics and automation": "ICRA",
    "conference on robot learning": "CoRL",
    "arxiv org": "arXiv",
}


def short_venue(venue: str | None) -> str:
    """Semantic Scholar's long venue names, shortened for the seed line ("Neural Information ..." -> NeurIPS)."""
    if not venue:
        return ""
    return VENUE_SHORT.get(re.sub(r"^ieee (cvf )?", "", norm(venue)), venue)


def anchor(heading: str) -> str:
    return "#" + re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading.lower()).strip())


def auto_picks(sections: dict, n: int) -> list:
    """Read-first list without agent picks: the 2 strongest foundation datasets / benchmarks, the 2 strongest compared
    papers (citations + stars), the 2 best follow-ups and the 2 most-used latest models, then the next best overall."""
    def strength(p):
        return math.log10(1 + (p.get("citationCount") or 0)) + 0.5 * math.log10(1 + ((best_repo(p) or {}).get("stars") or 0))
    groups = [[p for p in sections["foundation"] if p.get("kind") == "data"],  # ranked by co-citation score
              sorted(sections["compared"], key=lambda p: -strength(p)),
              sorted(sections["follow_up"], key=lambda p: -(p.get("score") or 0)),
              sections["latest"]]  # ranked by share
    picks = [p for group in groups for p in group[:2]]
    rest = [p for group in groups for p in group[2:]] + [p for p in sections["foundation"] if p.get("kind") != "data"]
    for p in rest:
        if len(picks) >= n:
            break
        picks.append(p)
    return picks[:n]


def pick_reason(p: dict, seeds_by_key: dict) -> str:
    multi = len(seeds_by_key) > 1
    sec = p["section"]
    if sec == "compared":
        who = " by %s" % ", ".join(seeds_by_key[k] for k in p.get("seeds") or [] if k in seeds_by_key) if multi else ""
        return "↩ Compared in the seed%s (%s)." % (who, ROLE_LABEL.get(p.get("role"), p.get("role")))
    if sec == "foundation":
        return "↩ Foundation: %s." % cocite_note(p)
    if sec == "latest":
        return "↪ Latest model: %s." % latest_note(p)
    if p.get("route") == "seed":
        who = " %s" % ", ".join(seeds_by_key[k] for k in p.get("seeds") or [] if k in seeds_by_key) if multi else " the seed"
        return "↪ Cites%s · %s." % (who, p.get("gate"))
    return "↪ Builds on %s · %s." % (", ".join((p.get("builds_on") or [])[:3]) or "the compared works", p.get("gate"))


def bibtex_for(p: dict) -> str:
    """Semantic Scholar's BibTeX for the paper (with a url field added), or a minimal entry built from its metadata."""
    bib = (p.get("bibtex") or "").strip()
    link = p.get("link") or ""
    if not bib:
        authors = p.get("authors") or []
        last = re.sub(r"[^A-Za-z]", "", (authors[0].split()[-1] if authors else "anon")) or "anon"
        word = next((w for w in re.findall(r"[A-Za-z]+", p.get("title") or "") if w.lower() not in BASIC_STOPWORDS), "paper")
        arxiv = (p.get("externalIds") or {}).get("ArXiv")
        venue = p.get("venue") or ("arXiv preprint arXiv:%s" % arxiv if arxiv else "")
        bib = "@article{%s%s%s,\n title = {%s},\n author = {%s},\n journal = {%s},\n year = {%s}\n}" % (
            last.lower(), p.get("year") or "", word.lower(), p.get("title"), " and ".join(authors) or "Unknown",
            venue, p.get("year") or "")
    if link and not re.search(r"^\s*url\s*=", bib, re.M | re.I):
        bib = re.sub(r"\n?\}\s*$", ",\n url = {%s}\n}" % link, bib)
    return bib


def write_bib(path: str, papers: list) -> int:
    seen, out = set(), []
    for p in papers:
        bib = bibtex_for(p)
        m = re.match(r"\s*@\w+\{([^,]+),", bib)
        if m:
            key, n = m.group(1), 2
            while key in seen:
                key = "%s%s" % (m.group(1), "abcdefghij"[n - 2] if n < 12 else n)
                n += 1
            seen.add(key)
            bib = bib.replace(m.group(1), key, 1)
        out.append(bib)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(out) + "\n")
    return len(out)


def cmd_report(args) -> None:
    wd = workdir_for(args)
    seeds = load_seeds(wd)
    data = load_json(os.path.join(wd, "candidates.json"))
    if not data:
        raise SystemExit("Missing candidates.json in %s - run `forward` (or `select`) first." % wd)
    lines, kept, n_bib_papers, todo = render_report(data, seeds, load_diffs(wd), args.read_first)
    out_path = args.out or os.path.join(wd, "report.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    bib_note = ""
    if not args.no_bib:
        bib_path = os.path.splitext(out_path)[0] + ".bib"
        bib_note = "; BibTeX for %d papers in %s" % (write_bib(bib_path, n_bib_papers), bib_path)
    with_code = sum(1 for p in kept if best_repo(p) or code_cell(p.get("code"))[0])
    print("REPORT  %s  (%d papers, %d with code; %d still missing a sentence%s)"
          % (out_path, len(kept), with_code, todo, bib_note))


def render_report(data: dict, seeds: list, diffs: dict, n_picks: int = 8) -> tuple:
    """-> (markdown lines, kept papers, papers for the .bib file, papers still missing a sentence)."""
    meta, papers = data["meta"], data["papers"]
    multi = len(seeds) > 1
    seeds_by_key = {s["key"]: label(s.get("title"), 30) for s in seeds}

    def diff_of(p):
        return diffs.get(p["key"]) or {"text": "", "exclude": False, "reason": "", "pick": None}

    sections = {"compared": [], "foundation": [], "follow_up": [], "latest": [], "related": [], "also": []}
    excluded = []
    for p in papers:
        d = diff_of(p)
        if d["exclude"]:
            if p["section"] != "also":
                excluded.append((p, d["reason"]))
            continue
        sections.setdefault(p["section"], []).append(p)
    kept = sections["compared"] + sections["foundation"] + sections["follow_up"] + sections["latest"]
    flagged = sorted([(diff_of(p)["pick"], i, p) for i, p in enumerate(kept) if diff_of(p)["pick"] is not None],
                     key=lambda t: t[:2])
    picks = [p for _, _, p in flagged] or auto_picks(sections, n_picks)

    def years(rows):
        ys = sorted(p.get("year") for p in rows if p.get("year"))
        return ("%s–%s" % (ys[0], ys[-1]) if ys[0] != ys[-1] else str(ys[0])) if ys else "—"

    def has_code(p):
        return bool(best_repo(p) or code_cell(p.get("code"))[0])

    the_seed = "the seeds" if multi else "the seed"
    lines = []
    if multi:
        lines += ["# Reading map: %s" % " + ".join(md_escape(label(s.get("title"), 30)) for s in seeds), ""]
    else:
        lines += ["# Reading map: %s" % md_escape(label(seeds[0].get("title"), 60)), ""]
    for s in seeds:
        authors = s.get("authors") or []
        first = first_author({"authors": authors if authors and isinstance(authors[0], dict) else
                              [{"name": a} for a in authors]})
        lines.append("**Seed:** [%s](%s) · %s · %s %s · %s citations · %s paper  " % (
            md_escape(s.get("title")), paper_link(s), first or "?", md_escape(short_venue(s.get("venue"))),
            s.get("year") or "", human(s.get("citationCount")), s.get("paper_type")))
    lines.append("**%d papers to read**, %d with code, picked from %s screened · searched %s · citation counts from "
                 "Semantic Scholar (click a count for Google Scholar's)" % (
                     len(kept), sum(1 for p in kept if has_code(p)), "{:,}".format(meta.get("pool") or 0),
                     meta.get("selected") or meta.get("generated")))
    lines.append("")

    h_a1 = "A1. What %s compare%s against" % (the_seed, "" if multi else "s")
    h_a2 = "A2. What the field builds on"
    h_b = "B. Influential follow-ups"
    h_c = "C. Latest models to keep an eye on"
    lines += ["| Section | Papers | With code | Published |", "|---|---:|---:|---|"]
    lines.append("| [Read these first](#read-these-first) | %d | %d | %s |" % (len(picks), sum(1 for p in picks if has_code(p)), years(picks)))
    for head, rows in ((h_a1, sections["compared"]), (h_a2, sections["foundation"]), (h_b, sections["follow_up"]),
                       (h_c, sections["latest"])):
        lines.append("| [%s](%s) | %d | %d | %s |" % (head, anchor(head), len(rows), sum(1 for p in rows if has_code(p)), years(rows)))
    lines.append("")

    lines += ["## Read these first", ""]
    if picks:
        lines += ["↩ = what %s build%s on · ↪ = newer work" % (the_seed, "" if multi else "s"), ""]
    else:
        lines.append("_Nothing to recommend yet._")
    for i, p in enumerate(picks, 1):
        repo = best_repo(p)
        code = (" · [%s](%s) ★%s" % (repo["repo"], repo["url"], human(repo.get("stars")))) if repo else ""
        lines.append("%d. %s · %s · %s citations%s  " % (i, paper_md(p), p.get("year") or "?", human(p.get("citationCount")), code))
        text = diff_of(p)["text"]
        lines.append("   %s%s" % (pick_reason(p, seeds_by_key), (" " + md_escape(text)) if text else ""))
    lines.append("")

    def table(rows, extra=None):
        head = "| # | Paper | Year | Cites | Code | " + ("%s | " % extra[0] if extra else "") + "Why read it |"
        sep = "|--:|---|--:|--:|---|" + ("---|" if extra else "") + "---|"
        out = [head, sep]
        for i, p in enumerate(rows, 1):
            paper = paper_md(p)
            if p.get("twins"):
                paper += "<br><sub>also published as %s</sub>" % md_escape("; ".join(p["twins"]))
            cells = [str(i), paper, str(p.get("year") or ""), cites_md(p), code_md(p)]
            if extra:
                cells.append(extra[1](p))
            cells.append(md_escape(diff_of(p)["text"]) or "_(to do)_")
            out.append("| " + " | ".join(cells) + " |")
        return out

    sel = meta["selection"]
    gates = sel["gates"]
    co = meta.get("cocitation") or {}
    n_b = co.get("backward_have") or 0
    lines += ["## A. Foundations", ""]
    lines.append("Read these to know the ground %s stand%s on: what %s compare%s against, and what %s and the works "
                 "%s compare%s against all build on." % (the_seed, "" if multi else "s", the_seed, "" if multi else "s",
                                                        the_seed, "they" if multi else "it", "" if multi else "s"))
    lines.append("")
    lines += ["### " + h_a1, ""]
    lines.append("Every paper %s compare%s against in %s tables, whatever its citation count: compare with these." % (
        the_seed, "" if multi else "s", "their" if multi else "its"))
    lines.append("")
    by_role = {}
    for p in sections["compared"]:
        by_role.setdefault(p.get("role"), []).append(p)
    heading = {"baseline": "Baselines", "benchmarked_model": "Benchmarked models", "compared_dataset": "Compared datasets",
               "compared_benchmark": "Compared benchmarks"}
    compared_by = ("Compared by", lambda p: ", ".join(seeds_by_key.get(k, k) for k in p.get("seeds") or [])) if multi else None
    for role in TABLE_ROLES:
        if by_role.get(role):
            lines += ["**%s (%d)**" % (heading[role], len(by_role[role])), ""] + table(by_role[role], compared_by) + [""]
    if not sections["compared"]:
        lines += ["_No comparison papers in the core set. Mark the seed's baselines with `core --role` / `--include`._", ""]
    if sections["related"]:
        lines.append("**Closest related work** (discussed, not compared in tables): " + " · ".join(
            "[%s](%s) (%s)" % (md_escape(label(p.get("title"), 50)), p.get("link") or "", p.get("year") or "?")
            for p in sections["related"][:12]))
        lines.append("")

    lines += ["### " + h_a2, ""]
    lines.append("Papers that %s and %d compared works cite again and again, still cited by recent work in the area, "
                 "plus older papers that at least %s of recent area papers cite. \"6 of 15\" = cited by 6 of %s and its "
                 "compared works; \"27%% of recent work\" = the share of the area's recent papers citing it. A paper "
                 "cited everywhere (COCO, CLIP) ranks below an equally co-cited specialist one." % (
                     the_seed, max(n_b - len(seeds), 0), pct(COCITE["consensus_share"]), the_seed))
    lines.append("")
    data_rows = [p for p in sections["foundation"] if p.get("kind") == "data"]
    model_rows = [p for p in sections["foundation"] if p.get("kind") != "data"]
    cited_by = ("Cited by", lambda p: "<br>".join(
        x for x in ("%d of %d" % (p["k_b"], p.get("n_b") or 0) if (p.get("k_b") or 0) >= 2 else "",
                    "%s of recent work" % pct(p.get("share_f")) if p.get("share_f") else "") if x) or "—")
    if data_rows:
        lines += ["**Datasets and benchmarks (%d)**" % len(data_rows), ""] + table(data_rows, cited_by) + [""]
    if model_rows:
        lines += ["**Models and methods (%d)**" % len(model_rows), ""] + table(model_rows, cited_by) + [""]
    if not sections["foundation"]:
        lines += ["_No foundations: the seed and its compared works share few references, or none is still cited by "
                  "recent work._", ""]

    lines += ["## " + h_b, ""]
    widened = [p for p in sections["follow_up"] if p.get("route") == "expand"]
    if sections["follow_up"]:
        lines.append("Papers citing %s, kept for enough citations for their age or a popular, well-documented repo, and "
                     "for citing what %s build%s on (the same-area check; bars in the footnote). Ranked by a SOTA "
                     "score: citations and GitHub stars per month, with a bonus for the last %d months." % (
                         the_seed, the_seed, "" if multi else "s", sel["recent_months"]))
        if widened:
            lines.append("")
            lines.append("Because %s, this section also has %d recent papers on the seed's topic that build on the "
                         "works it compares against (marked \"builds on\")." % (meta["expand_why"], len(widened)))
        lines.append("")

        def kept_because(p):
            if p.get("route") == "seed":
                base = ("cites " + ", ".join(seeds_by_key.get(k, k) for k in p.get("seeds") or [])) if multi else "cites the seed"
            else:
                base = "builds on " + md_escape(", ".join((p.get("builds_on") or [])[:3]) or "the compared works")
            extra = "<br>used by %s of recent work" % pct(p["share_f"]) if p.get("share_f") else ""
            return "%s<br>_%s_%s" % (base, md_escape(p.get("gate")), extra)
        lines += table(sections["follow_up"], ("Kept because", kept_because))
    else:
        lines.append("_No paper citing %s passes the influence gate yet (%s citations so far)._" % (
            the_seed, " + ".join(human(s.get("citationCount")) for s in seeds)))
    lines.append("")

    lines += ["## " + h_c, ""]
    lines.append("Papers from the last %d months that many of the area's newest papers build on: each is cited by at "
                 "least %s of the area papers published after it. They are found from the citing side, so they show "
                 "up even when Semantic Scholar has no reference list for them (as for SAM 3 and Qwen2.5-VL)." % (
                     sel["latest_months"], pct(COCITE["latest_share"])))
    lines.append("")
    if sections["latest"]:
        lines += table(sections["latest"], ("Used by", lambda p: "%s of later area papers<br>_%d of %d_" % (
            pct(p.get("share_f")), p.get("support_f") or 0, p.get("eligible") or 0)))
    else:
        lines.append("_None: too few recent papers in the area to tell (%d with reference lists)._" % (co.get("area_size") or 0))
    lines.append("")

    also = sections["also"][:sel["max_also"]]
    if also:
        lines += ["<details><summary>Also qualified, beyond the size caps (%d, not reviewed)</summary>" % len(also), ""]
        for p in also:
            lines.append("- %s (%s) · %s · %s" % (paper_md(p), p.get("year") or "?", md_escape(p.get("gate")),
                                                   "cites the seed" if p.get("route") == "seed" else
                                                   "builds on " + md_escape(", ".join(p.get("builds_on") or []))))
        lines += ["", "</details>", ""]
    if excluded:
        lines += ["<details><summary>Excluded after review (%d)</summary>" % len(excluded), ""]
        for p, reason in excluded:
            lines.append("- %s — %s" % (md_escape(p.get("title")), md_escape(reason)))
        lines += ["", "</details>", ""]

    def g(name):
        cites, stars = gates[name]
        if cites is None:
            return "nothing (closed)"
        return "≥%d citations%s" % (cites, " or ★≥%d" % stars if stars else "")
    area_def = ("papers from the last %d months that cite %s, or cite a compared work and match the topic (keywords: "
                "%s); %d had reference lists" % (sel["latest_months"], the_seed,
                                                 md_escape(", ".join(meta.get("keywords") or [])) or "-",
                                                 co.get("area_size") or 0))
    old = gates.get("expand.old") or [None, None]
    lines += ["---", "", "**How this map was made.**", ""]
    lines.append("- **A1** comes from %s own results and comparison tables (read from the arXiv HTML) and was reviewed "
                 "by the agent; no citation bar." % ("the seeds'" if multi else "the seed's"))
    lines.append("- **A2** = papers cited by at least %d of the %d reference lists of %s and its compared works and by "
                 "at least %s (and %d) of recent area papers, or older papers cited by at least %s of recent area "
                 "papers; all with at least %d citations, ranked by (backward share + forward share) x log(corpus size "
                 "/ citations). Recent area = %s." % (
                     max(COCITE["k_min"], math.ceil(COCITE["k_share"] * n_b)), n_b, the_seed,
                     pct(COCITE["confirm_share"]), COCITE["confirm_support"], pct(COCITE["consensus_share"]),
                     COCITE["min_citations"], area_def))
    lines.append("- **B** = papers citing %s. Kept with %s at any age; %s within %d months; %s within %d months. Stars "
                 "count only for a verified repo whose README covers at least %d of: setup, usage, training, "
                 "evaluation, released weights (twice the stars if the README could not be read). Each must also cite "
                 "one of the %d strongest co-citations of %s and its compared works; a paper whose reference list is "
                 "not available passes on its citation of %s. When fewer than %d pass, B adds papers that cite a "
                 "compared work and match the topic, with %s within %d months or %s within %d months (%s); citers of "
                 "hubs (≥%s citations, e.g. SAM or CLIP) are not searched%s." % (
                     the_seed, g("seed.any"), g("seed.recent"), sel["recent_months"], g("seed.mid"), sel["mid_months"],
                     sel["min_docs"], COCITE["coupling_top"], the_seed, the_seed, sel["min_forward"],
                     g("expand.recent"), sel["recent_months"], g("expand.mid"), sel["mid_months"],
                     "older papers are left out" if old[0] is None else
                     "older ones need ≥%d citations and citations of 2 compared works" % old[0],
                     human(HUB_MIN_CITATIONS),
                     "" if meta.get("expand_shown") else "; not needed this time: %s" % meta["expand_why"]))
    lines.append("- **C** = papers from the last %d months cited by at least %s (and %d) of the recent area papers "
                 "published after them, out of at least %d such papers." % (
                     sel["latest_months"], pct(COCITE["latest_share"]), COCITE["latest_support"], COCITE["min_eligible"]))
    if sel.get("min_citations"):
        lines.append("- A hard floor of ≥%d citations applied to B (stars could not replace it)." % sel["min_citations"])
    lines.append("- Screened %s papers (%s citing %s%s); %d reference lists checked. Sources: Semantic Scholar "
                 "(citations, references, counts, BibTeX), arXiv, Hugging Face and GitHub (code, stars, READMEs)%s. "
                 "Google Scholar is never queried. Generated by citation-snowball %s on %s." % (
                     "{:,}".format(meta.get("pool") or 0), "{:,}".format(meta.get("seed_citers") or 0), the_seed,
                     "; topic search %s since %s" % (meta["query"], meta["since"]) if meta.get("query") and meta.get("since") else "",
                     meta.get("verified") or 0, ", OpenAlex" if meta.get("openalex_calls") else "",
                     meta.get("version") or VERSION, meta.get("selected") or meta.get("generated")))
    todo = sum(1 for p in kept if not diff_of(p)["text"])
    bib_papers = picks + [p for p in kept if p not in picks]
    return lines, kept, bib_papers, todo


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
    latest = latest_release(writable_dir(args.cache_dir) if args.cache_dir else None, max_age_hours=0)
    newer = bool(latest) and version_tuple(latest["version"]) > version_tuple(VERSION)
    rows.append(("citation-snowball", "%s; latest release %s" % (VERSION, latest["version"] if latest else "unknown")
                 + (" - update with `fbsearch.py update`" if newer else " (up to date)" if latest else ""), "this script"))
    if newer:
        tips.append("citation-snowball %s is out: `python3 %s update` (what's new: %s)."
                    % (latest["version"], os.path.realpath(__file__), latest.get("url")))
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
    rows.append(("Hugging Face", "ok" if code == 200 else str(code), "code links, likes, star probe"))
    token = github_token()
    if token:
        code, hdrs = probe("https://api.github.com/rate_limit", {"Authorization": "bearer " + token})
        rows.append(("GitHub", "token found; %s" % ("ok" if code == 200 else code), "stars, READMEs"))
    else:
        code, hdrs = probe("https://api.github.com/rate_limit")
        rows.append(("GitHub", "no token (60 requests/hour)", "stars, READMEs"))
        tips.append("No GitHub token: stars come from Hugging Face's counts (sometimes stale) and READMEs from "
                    "raw.githubusercontent.com, so the stars route of the influence gate is less reliable. "
                    "`gh auth login` (or GH_TOKEN) fixes that.")
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


def add_selection_args(p, defaults: bool) -> None:
    """Gate and cap flags shared by `forward` and `select` (`select` falls back to what `forward` used)."""
    d = SELECT_DEFAULTS if defaults else {k: None for k in SELECT_DEFAULTS}
    note = "" if defaults else " (default: what `forward` used)"
    p.add_argument("--min-forward", type=int, default=d["min_forward"],
                   help="widen B to the compared works' citers when fewer papers citing the seed pass (default 8)" + note)
    p.add_argument("--max-follow-ups", type=int, default=d["max_follow_ups"],
                   help="max papers in section B (default 15)" + note)
    p.add_argument("--max-foundations", type=int, default=d["max_foundations"],
                   help="max datasets/benchmarks, and max models/methods, in section A2 (default 6 each)" + note)
    p.add_argument("--max-latest", type=int, default=d["max_latest"],
                   help="max papers in section C (default 8)" + note)
    p.add_argument("--latest-months", type=int, default=d["latest_months"],
                   help="how recent section C's models and the area's papers are, in months (default 30)" + note)
    p.add_argument("--min-coupling", type=int, default=d["min_coupling"],
                   help="same-area check: how many of the 30 strongest co-citations a follow-up must cite (default 1; "
                        "0 turns it off)" + note)
    p.add_argument("--min-area", type=int, default=d["min_area"],
                   help="also widen when fewer recent papers cite the seed, so section C has enough to go on "
                        "(default 30)" + note)
    p.add_argument("--max-also", type=int, default=d["max_also"],
                   help="max gate passers listed beyond the caps (default 20)" + note)
    p.add_argument("--expand", choices=["auto", "always", "never"], default=d["expand"],
                   help="widening B: auto = only when it is short (default)" + note)
    p.add_argument("--gate", action="append", metavar="ROUTE.BRACKET=CITES[,STARS]",
                   help="override one gate, e.g. seed.recent=10,100 or expand.mid=80,- (repeatable). Gates: %s"
                        % ", ".join("%s=%s" % (k, ",".join("-" if x is None else str(x) for x in v)) for k, v in GATES.items()))
    p.add_argument("--min-citations", type=int, default=d["min_citations"],
                   help="hard citation floor for section B; also turns the stars routes off (default none)" + note)
    p.add_argument("--min-docs", type=int, default=d["min_docs"],
                   help="README docs score (0-5) a repo needs for the stars routes (default 4)" + note)
    p.add_argument("--recent-months", type=int, default=d["recent_months"],
                   help="age of the 'recent' bracket in months (default 12)" + note)
    p.add_argument("--mid-months", type=int, default=d["mid_months"],
                   help="age of the 'mid' bracket in months (default 24)" + note)
    # 1.2 flags, kept (deprecated) so old commands still run
    p.add_argument("--max-forward", type=int, help=argparse.SUPPRESS)
    p.add_argument("--max-backward", type=int, help=argparse.SUPPRESS)
    p.add_argument("--recent-min-citations", type=int, help=argparse.SUPPRESS)
    p.add_argument("--no-code", action="store_true", help="skip the GitHub / Hugging Face lookup")
    p.add_argument("--no-github-search", action="store_true", help="only use repos linked from the paper itself")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Forward/backward citation search helper (citation-snowball skill).")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cache-dir", default=default_cache_dir(),
                        help="HTTP cache shared across runs (default: %(default)s; FBS_CACHE_DIR overrides)")
    common.add_argument("--refresh", action="store_true", help="ignore cached HTTP responses")

    p = sub.add_parser("doctor", parents=[common], help="check APIs, keys, tools and whether a newer version is out")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("resolve", parents=[common],
                       help="resolve a seed (arXiv id/URL, DOI, S2 id/URL, ACL id, OpenReview URL, or title)")
    p.add_argument("seed")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("backward", parents=[common],
                       help="resolve the seed(s), digest full text, fetch references, propose the core set")
    p.add_argument("seed", nargs="+", help="one or more seeds; extra seeds are added to the same workdir")
    p.add_argument("--workdir", help="default: ./fbsearch-<first-title-slug>[-and-N-more]")
    p.add_argument("--type", choices=["method", "dataset", "benchmark", "mixed"],
                   help="override the auto-guessed paper type (applies to every seed given)")
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

    p = sub.add_parser("forward", parents=[common],
                       help="co-citation, citers of the seeds (B, widened when short), the area's recent papers; then select")
    p.add_argument("--workdir", required=True, help="the WORKDIR printed by `backward`")
    p.add_argument("--query", help='Semantic Scholar boolean query for the widening topic search, e.g. \'"interactive '
                                   'segmentation" | "part segmentation"\' (default: built from --keywords)')
    p.add_argument("--keywords", help="comma-separated phrases that define the seed's topic: widening keeps only papers "
                                      "matching one (in the title, or a multi-word phrase in the abstract) or found by "
                                      "the topic search. Prefer specific multi-word phrases (default: words of the seed "
                                      "titles)")
    p.add_argument("--seed-cap", type=int, help="max citers per seed (default 3000 without an S2 key, 9999 with)")
    p.add_argument("--cap", type=int, help="max newest citers per compared work when widening (default 1000 without an S2 "
                                           "key, 3000 with)")
    p.add_argument("--topic-limit", type=int, default=1000, help="max papers from the topic search (default 1000)")
    p.add_argument("--no-topic-search", action="store_true", help="skip the widening topic search")
    p.add_argument("--verify-cap", type=int, default=3000,
                   help="max candidates whose reference lists are checked, most cited first (default 3000)")
    p.add_argument("--area-cap", type=int,
                   help="max recent area papers whose reference lists are read for co-citation (default 1000 with an S2 "
                        "key, 400 without)")
    p.add_argument("--probe-cap", type=int, default=300,
                   help="max papers probed for GitHub stars in each probe step, fastest-rising first (default 300)")
    p.add_argument("--openalex-budget", type=int, default=40,
                   help="max billable OpenAlex calls for most-cited citers of partially fetched papers (0 = off)")
    add_selection_args(p, defaults=True)
    p.set_defaults(func=cmd_forward)

    p = sub.add_parser("select", parents=[common],
                       help="re-apply gates and caps to pool.json without re-searching (e.g. after EXCLUDEs)")
    p.add_argument("--workdir", required=True, help="the WORKDIR printed by `backward`")
    add_selection_args(p, defaults=False)
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("report", help="render report.md (+ references .bib) from candidates.json + diffs.json")
    p.add_argument("--workdir", required=True, help="the WORKDIR printed by `backward`")
    p.add_argument("--out", help="output path (default: <workdir>/report.md); the .bib file goes next to it")
    p.add_argument("--read-first", type=int, default=8, help="papers in 'Read these first' without agent picks (default 8)")
    p.add_argument("--no-bib", action="store_true", help="do not write the BibTeX file")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("update", parents=[common],
                       help="update this copy of the skill to the latest release (git pull --ff-only in its folder)")
    p.set_defaults(func=cmd_update)

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
