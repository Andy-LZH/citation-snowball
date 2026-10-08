# Files and report format

## Workdir files

`backward` creates `./fbsearch-<first-title-slug>[-and-N-more]/` (or `--workdir`). Every later stage reads and
writes there.

| File | Written by | Contents |
|---|---|---|
| `seeds.json` | backward | one entry per seed: `key`, `paperId`, `title`, `year`, `venue`, `citationCount`, `externalIds`, `authors`, `paper_type`, `paper_type_source`, `dir`, `cites_seeds` |
| `seeds/<key>/seed.json` | backward | the seed's full Semantic Scholar record, plus `paper_type` |
| `seeds/<key>/refs.json` | backward | the seed's references; recovered ones carry `"source": "bibliography"` |
| `seeds/<key>/digest.json` | backward | from the arXiv HTML: `sections`, `tables`, `bib` and `contexts` (see below) |
| `seeds/<key>/fulltext.txt` | backward | plain text of the seed: one line per paragraph, heading and table row (cells joined by ` \| `) |
| `seeds/<key>/paper.pdf` | backward | only when there is no HTML |
| `seeds/<key>/core.json` | backward | this seed's proposed core set |
| `core.json` | backward, `core` | the merged core set: every reference with `role`, `score`, `overlap`, `evidence`, `include`, `seeds` |
| `pool.json` | forward | `meta` (settings, query, keywords, counts), `papers` (every follow-up candidate screened) and `cocitation` (the co-citation counts); see below |
| `candidates.json` | select (run by forward) | `meta` (pool meta, settings used, widening reason, co-citation summary) and `papers`: the kept papers and those that also qualified |
| `briefs.md` | select | one block per kept paper (TL;DR or abstract, why it was kept, code): the agent's input for `diffs.json` |
| `diffs.json` | the agent | the one-line reasons, picks, dataset labels and exclusions (see below) |
| `report.md` | report | the reading map (or `--out <path>.md`) |
| `report.bib` | report | BibTeX for every kept paper, next to the report (`--no-bib` to skip) |

A workdir made by 1.2 or earlier, which has `seed.json` at the top and no `seeds.json`, cannot be reused: run
`backward` again.

`digest.json` holds:

- `sections`: each section's kind, the bib ids it cites, and the related-work text;
- `tables`: each table's caption, kind, header and row bib ids, and row labels;
- `bib`: each entry's text, blocks, links and matched reference index;
- `contexts`: the citation sentences for each bib id.

## pool.json

Each record in `papers` (a follow-up candidate) has:

- `paperId`, `title`, `year`, `venue`, `publicationDate`, `citationCount`, `externalIds`.
- `sources`: what found it: `seed-citers`, `core-citers`, `topic-search`, `openalex`.
- `cited`: the seeds, seed references and strongest backward co-citations it cites, known from citation edges or a
  checked reference list.
- `verified`: whether its reference list was checked.
- `influential`: how many of its citation edges Semantic Scholar marks influential.
- `code`: only for probed papers. It has the same shape as in `candidates.json`; the first verified repo may carry
  `docs`.
- `abstract`: only when a multi-word keyword phrase matched it.

The `cocitation` block holds everything sections A2 and C are computed from:

| Key | Contents |
|---|---|
| `backward_set`, `backward_have` | the seeds and compared works whose reference lists were read, and how many were found (`n_b`) |
| `backward` | paperId → how many of them cite it (`k_b`, at least 2) |
| `area_size`, `area_dates` | how many recent area papers had reference lists, and their publication dates (for "published after") |
| `forward` | paperId → how many recent area papers cite it, for the 300 most cited |
| `meta` | paperId → `title`, `year`, `venue`, `publicationDate`, `citationCount`, `externalIds`, `abstract` (first 1,500 characters) and `cited_as_data` ([how many compared works cite it as data, out of how many with citation sentences]) |

## candidates.json

Each record in `papers` has:

- **Identity:** `key` (the first 8 characters of the paperId, used in `diffs.json`), `paperId`, `title`, `year`,
  `venue`, `publicationDate`, `citationCount`, `influentialCitationCount`, `externalIds`, `authors`.
- **Text:** `tldr`, `abstract`, `link`, `bibtex` (Semantic Scholar's).
- **Selection:**
  - `section`: `compared` (A1), `foundation` (A2), `follow_up` (B), `latest` (C), `related` (one line under A1) or
    `also`;
  - `role` (A1); `kind` (A2: `data` or `model`); `route` (B: `seed`, or `expand` for a widening row);
  - `seeds`: which seeds compare against it (A1) or which it cites (B);
  - `gate`: why a follow-up passed, e.g. `"241 citations in 19 months"` or `"★183, docs 5/5"`;
  - `score` (B: the SOTA score; A2: the co-citation score; C: the share), `age_months`;
  - co-citation numbers: `k_b` and `n_b` (A2), and `support_f`, `eligible` and `share_f` (A2, C, and B rows the
    recent area builds on);
  - `coupling`: how many of the strongest backward co-citations a follow-up cites;
  - `builds_on`: the compared works it cites, most specific first;
  - `twins`: other records of the same dataset (A2);
  - `lineage`, `n_core`, `found_via`, `verified`, `evidence`.
- **Code:** `code` is `{"github": [{"repo", "url", "stars", "source", "created", "docs", "docs_hits"}],
  "hf": [{"kind", "id", "url", "likes", "verified"}]}`. A GitHub `source` containing "verify" marks an unverified
  search match.

## diffs.json

One JSON object keyed by the 8-character keys. A value is one of:

```json
{
  "1628a19c": "One sentence: why read it - what it contributes and how it differs from the seed.",
  "ad113d8b": {"diff": "The same kind of sentence.", "pick": 1},
  "29efbe39": {"diff": "Releases RefCOCO and RefCOCO+, the referring benchmarks the compared models report on.", "kind": "data"},
  "5d7cd5c4": "EXCLUDE: medical-imaging application that only uses SAM off the shelf"
}
```

- **A sentence** fills the "Why read it" column. A paper without one shows "_(to do)_", and `report` prints how many
  are missing.
- **`"pick": 1, 2, ...`** (or `true`) puts the paper in **Read these first**, in that order. If any paper is picked,
  the agent's picks replace the automatic ones.
- **`"kind": "data"` or `"model"`** moves an A2 paper to the datasets or the models table. Run `select` afterwards so
  the caps apply to the corrected split.
- **`"EXCLUDE: reason"`** (or `{"exclude": "reason"}`) drops the paper from every section. It is listed in a collapsed
  "Excluded after review" block. Run `select` afterwards, so the next candidates move up.

## report.md layout

1. **Title and seed line(s):** `# Reading map: <short name>`, then one line per seed: title link, first author,
   short venue, year, citations, paper type.
2. **Counts:** papers to read and how many have code, papers screened, search date.
3. **At a glance:** a table of the sections (Read these first, A1, A2, B, C) with their paper counts, how many have
   code, and their years. Each row links to its section.
4. **Read these first:** 5-8 numbered picks with year, citations and repo with stars. Each has a ↩ / ↪ line saying
   why it is there, followed by the agent's sentence:
   - compared in the seed;
   - a foundation, with how many cite it;
   - cites the seed, or builds on named compared works;
   - a latest model, with the share of the area using it.
5. **A. Foundations:**
   - **A1. What the seed compares against:** one table per role (baselines, benchmarked models, compared datasets,
     compared benchmarks), then one line of closest related work.
   - **A2. What the field builds on:** a datasets-and-benchmarks table, then a models-and-methods table, each with a
     "Cited by" column (`6 of 15` = the seed and compared works; `14% of recent work`). Twins appear as "also
     published as".
6. **B. Influential follow-ups,** with a "Kept because" column: "cites the seed" (or which seed) or "builds on …",
   the gate, and "used by N% of recent work" when the area builds on it. One sentence explains any widening.
7. **C. Latest models to keep an eye on,** with a "Used by" column: the share of later area papers citing it, and
   the counts.
8. **Also qualified,** collapsed, at most `--max-also` (20) follow-ups beyond the cap, with why they qualified.
9. **Excluded after review,** collapsed.
10. **How this map was made:** the exact bars used for A2, B and C, the recent area's definition and size, the
    keywords, the query, what was screened, and the sources.

**Table columns:** `# · Paper · Year · Cites · Code · [extra] · Why read it`.

- **Paper** bolds the short name: `**LISA**: Reasoning Segmentation ...`.
- **Cites** is Semantic Scholar's count, linked to a Google Scholar title search the reader can click.
- **Code** shows the best verified repo as `owner/repo ★stars · docs n/5`, and a verified Hugging Face artifact
  (`🤗 id ♥likes`). With neither, it shows `maybe owner/repo (unverified)` or `—`.

## report.bib

The kept papers in read-first order, then sections A1, A2, B and C:

- Semantic Scholar's `citationStyles.bibtex`, with a `url` field added when it has none.
- Keys made unique with a letter suffix (`Lai2023LISARSa`).
- A minimal `@article` built from title, authors, venue or arXiv id, and year when Semantic Scholar has no BibTeX.
