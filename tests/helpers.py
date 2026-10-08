"""Small fixture builders shared by the tests. Everything here is offline: no test touches the network."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import fbsearch  # noqa: E402

TODAY = dt.date(2026, 10, 8)
_counter = [0]


def pid(n=None) -> str:
    """A fake 40-character Semantic Scholar paperId whose first 8 characters (the report key) are unique."""
    if n is None:
        _counter[0] += 1
        n = _counter[0]
    return ("%08x" % n) + "0" * 32


def date_months_ago(months: int) -> str:
    y, m = TODAY.year, TODAY.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return "%04d-%02d-15" % (y, m)


def seed(n=1, cites=20, title="SeedNet: A Seed Paper for Part Segmentation", ptype="method") -> dict:
    p = pid(1000 + n)
    return {"key": p[:8], "paperId": p, "title": title, "year": 2025, "venue": "ACL", "publicationDate": "2025-05-23",
            "citationCount": cites, "externalIds": {"ArXiv": "2505.%05d" % n}, "authors": [{"name": "Ada Lovelace"}],
            "paper_type": ptype, "paper_type_source": "auto-guess", "dir": "seeds/%s" % p[:8]}


def core_row(title, role="baseline", cites=500, include=True, year=2023, paper_id=None, seeds=None) -> dict:
    p = paper_id or pid()
    return {"id": p[:8], "paperId": p, "title": title, "year": year, "venue": "CVPR", "citationCount": cites,
            "publicationDate": "%d-06-01" % year, "externalIds": {}, "role": role, "score": 3.0, "overlap": 0.4,
            "evidence": ["in S4.T2(row)"], "include": include, "seeds": seeds or []}


def rec(title, cites=0, months=6, cited=(), sources=("seed-citers",), stars=None, docs=None, created_months=None,
        verified=True, paper_id=None) -> dict:
    """A pool record as `forward` saves it."""
    r = {"paperId": paper_id or pid(), "title": title, "year": int(date_months_ago(months)[:4]), "venue": "",
         "publicationDate": date_months_ago(months), "citationCount": cites, "externalIds": {"ArXiv": "2501.00001"},
         "sources": list(sources), "cited": list(cited), "influential": 0, "verified": verified}
    if stars is not None:
        r["code"] = {"github": [{"repo": "lab/" + title.split(":")[0].replace(" ", "-")[:20], "url": "https://github.com/x/y",
                                 "stars": stars, "source": "hf-paper", "docs": docs,
                                 "created": date_months_ago(created_months if created_months is not None else months)}],
                     "hf": []}
    return r


def pool(papers, keywords=("part segmentation",), expanded=True) -> dict:
    return {"meta": {"keywords": list(keywords), "expanded": expanded, "pool": len(papers)}, "papers": list(papers)}


def cfg(**over) -> dict:
    class Args:
        pass
    args = Args()
    for k, v in over.items():
        setattr(args, k, v)
    return fbsearch.selection_config(args)


def meta_row(title, cites, months, paper_id=None, abstract="") -> dict:
    """A paper in a pool's `cocitation.meta` block."""
    p = paper_id or pid()
    return {"paperId": p, "title": title, "year": int(date_months_ago(months)[:4]), "venue": "",
            "publicationDate": date_months_ago(months), "citationCount": cites, "externalIds": {}, "abstract": abstract}


def cocite_block(rows, backward=None, forward=None, n_b=15, area_months=None) -> dict:
    """A pool's `cocitation` block. `rows` are meta_row()s; backward / forward map their titles to how many of the
    seed + compared works (n_b) / of the recent area papers cite them. The area is one paper per month for each age in
    `area_months` (default: 100 papers, the newest 2 months old, spread over the last 30 months)."""
    by_title = {r["title"]: r["paperId"] for r in rows}
    ages = area_months if area_months is not None else [2 + (i * 28) // 100 for i in range(100)]
    return {"backward_set": ["x"] * n_b, "backward_have": n_b,
            "backward": {by_title[t]: k for t, k in (backward or {}).items()},
            "forward": {by_title[t]: n for t, n in (forward or {}).items()},
            "area_size": len(ages), "area_dates": sorted(date_months_ago(m) for m in ages),
            "meta": {r["paperId"]: r for r in rows}}
