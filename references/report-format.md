# Files and report format

## Workdir files

`backward` creates `./fbsearch-<title-slug>/` (or `--workdir`). Every later stage reads and writes there.

| File | Written by | Contents |
|---|---|---|
| `seed.json` | backward | Semantic Scholar record of the seed, plus `paper_type` and `paper_type_source` |
| `refs.json` | backward | the seed's references; recovered ones carry `"source": "bibliography"` |
| `digest.json` | backward | from the arXiv HTML: `sections` (kind, cited bib ids, related-work text), `tables` (caption, kind, header / row bib ids, row labels), `bib` (entry text, blocks, links, matched reference index), `contexts` (citation sentences per bib id) |
| `fulltext.txt` | backward | plain text of the seed, one line per paragraph, heading and table row (cells joined by ` \| `) |
| `paper.pdf` | backward | only when there is no HTML |
| `core.json` | backward, `core` | every reference with `role`, `score`, `overlap`, `evidence`, `include` |
| `candidates.json` | forward | `meta` (thresholds, query, counts, Semantic Scholar retries) and `papers` (see below) |
| `briefs.md` | forward | one block per kept paper (TL;DR or abstract, relation), the agent's input for `diffs.json` |
| `diffs.json` | the agent | `{"<key>": "one sentence"}` or `{"<key>": "EXCLUDE: reason"}` |
| `report.md` | report | the deliverable (or `--out <path>`) |

Each record in `candidates.json` `papers` has these fields:

- `key`: the first 8 characters of the Semantic Scholar paperId, used in `diffs.json`.
- `title`, `year`, `venue`, `citationCount`, `authors`, `tldr`, `abstract`, `link`.
- `direction`: `backward` (cited by the seed) or `forward`.
- `role`: for backward papers.
- `tier`, `relevance`, `rank`, `lineage`, `cites_seed`, `n_core`.
- `found_via`: the sources that surfaced it (seed-citers, core-citers, topic-search, openalex, backward).
- `verified`: whether its reference list was checked.
- `relation`: a human-readable summary, e.g. "cites seed; cites 2 core: SAM, CascadePSP".
- `code`: `{"github": [{"repo", "url", "stars", "source"}], "hf": [{"kind", "id", "url", "likes", "verified"}]}`.
  A GitHub `source` containing "verify" means an unverified search match.
- `bucket`: `main`, `below_threshold` or `overflow`.

## diffs.json

```json
{
  "1628a19c": "Refines masks from any segmenter with a separate post-processing network, whereas the seed adds a learned output token inside SAM and stays zero-shot.",
  "5d7cd5c4": "EXCLUDE: medical-imaging application that only uses SAM off the shelf"
}
```

One sentence each, concrete, comparing against the seed. A paper with no entry shows "_(to do)_" in the report, and
`report` prints how many are missing.

## report.md layout

1. **Title and seed line:** title link, first author, venue, year, citations.
2. **Paper type, search date, threshold**, and how many papers were kept, with and without code.
3. **What the seed compares against:** the core set, grouped as baselines, benchmarked models, compared datasets,
   compared benchmarks and closest related work.
4. **Section 1, influential papers with code.** Columns: #, Paper (with a GS link), Year, Cites, Found via (↩
   backward / ↪ forward plus the relation), Code (GitHub, 🤗 Hugging Face), ★ / ♥, How it differs from the seed.
   Sorted by citations, then stars / likes.
5. **Section 2, influential papers without verified code.** The same columns minus the code columns, plus a
   "Possible code (unverified)" column when any row has an unverified candidate.
6. **Appendix A:** papers directly compared (core) but below the citation threshold.
7. **Appendix B:** overflow candidates beyond the size caps, not reviewed.
8. **Excluded as off-topic:** collapsed list with the reasons from `diffs.json`.
9. **Method footnote:** sources used, the topic query, keywords, the counts' source and date.
