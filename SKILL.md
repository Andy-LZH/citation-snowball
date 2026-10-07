---
name: citation-snowball
description: "Forward and backward citation search (snowballing) from one seed paper. Reads the seed's results tables, dataset-comparison tables and related work to find what it compares against (baselines, benchmarked models, compared datasets and benchmarks, closest methods), then finds influential papers citing the seed or that comparison set (at least 30 citations, or 10 if under 18 months old), looks up their GitHub and Hugging Face code with stars and likes, and writes a markdown report ranked by citations with one line on how each paper differs from the seed. Use when the user shares a paper they like (arXiv link or ID, DOI, title, OpenReview or Semantic Scholar URL) and wants related work, baselines or datasets to compare against, papers that cite it, follow-up work, a literature map, or code for similar methods, datasets or benchmarks."
metadata:
  version: "1.2.0"
  requirements: "Python 3.8+ (standard library only) with internet access to Semantic Scholar, arXiv, Hugging Face, GitHub and OpenAlex. Optional: S2_API_KEY for much faster runs, a gh CLI login or GH_TOKEN for GitHub stars."
---

# Citation snowball: forward and backward citation search

Turn one paper the user likes (the **seed**) into a ranked, code-linked map of the influential work around it.

- **Backward:** what the seed builds on and compares against. That means the baselines in its results tables (method
  papers), the datasets in its dataset-comparison table (dataset papers), the benchmarks and evaluated models (benchmark
  papers), and the closest related work.
- **Forward:** influential papers that cite the seed or that comparison set.
- **Output:** one markdown file listing every kept paper with its citation count, GitHub / Hugging Face code with
  stars / likes, and one sentence on how it differs from the seed. Papers with code come first; both sections are
  sorted by citations, then stars / likes.

A helper script does all the API work. You supply judgment at two checkpoints: the core comparison set (step 2) and
the one-line differences (step 4).

## Setup

- The script is `scripts/fbsearch.py` in this skill's folder. Paths in this file are relative to the folder that
  contains this SKILL.md; call the script by its absolute path, e.g.
  `python3 /abs/path/to/citation-snowball/scripts/fbsearch.py backward ...`. It needs Python 3.8+ and nothing
  outside the standard library.
- It needs network access (Semantic Scholar, arXiv, Hugging Face, GitHub, OpenAlex). In a sandboxed agent such as
  Codex, request network access or escalation for these commands.
- `fbsearch.py doctor` checks reachability, keys and tools in about 10 seconds. Run it first if anything seems off.
- **Speed.** Without `S2_API_KEY`, Semantic Scholar's shared keyless pool is often busy and the script retries
  patiently, so a full run can take 5-20 minutes. Tell the user once that a free key
  (https://www.semanticscholar.org/product/api#api-key-form), exported as `S2_API_KEY`, makes runs fast and reliable.
  GitHub stars use `GH_TOKEN` or the gh CLI login when present.
- `forward` can stay quiet for minutes while it retries. Run it in the background if your environment supports that,
  and don't kill it for being slow. Results are cached in `~/.cache/citation-snowball`, so re-runs are fast.

## Workflow

### 0. Pin down the seed and preferences

- **Seed:** an arXiv ID or URL, DOI, Semantic Scholar / OpenReview / ACL Anthology URL, or the exact title.
- **Defaults, unless the user says otherwise:**
  - Keep papers with at least 30 citations, or at least 10 if published in the last 18 months.
  - List all kept papers, with code first.
  - Sort by citations, then stars / likes.
- **Output path:** default `<seed-short-name>-snowball.md` in the current directory.

### 1. Backward

```
python3 <skill>/scripts/fbsearch.py backward "<seed>"
```

It prints `WORKDIR`, the paper type it guessed (method / dataset / benchmark / mixed), the tables it parsed, and a
**PROPOSED CORE SET**: every relevant reference with a role (baseline, benchmarked model, compared dataset or
benchmark, related work, evaluation dataset) and the evidence for it, e.g. `in S4.T2(row)` or
`compared: "..."`. If the type is wrong, re-run with `--type method|dataset|benchmark|mixed`; that is fast because
of the cache.

### 2. Review the core set

This is your judgment, and it shapes the whole forward search. Check the proposal against the paper:
`<WORKDIR>/fulltext.txt` has the whole paper, one line per paragraph and per table row (cells separated by ` | `),
and `digest.json` has the table captions, row labels and related-work text. A good core set is:

- **Method paper:** the methods in its main results tables, plus the 5-10 closest methods from related work.
- **Dataset paper:** the datasets it compares against (comparison or statistics table, or reviewed in related work)
  and the closest related work.
- **Benchmark paper:** the benchmarks it positions itself against, plus the models it evaluates.
- **Mixed paper** (e.g. a new dataset whose paper also benchmarks models): both of the above. The script picks
  `mixed` when a dataset paper says it benchmarks or evaluates models.

Rules of thumb:

- Drop generic hubs cited in passing, such as GPT-3, ViT, CLIP, ResNet or Adam, unless the seed directly compares
  against them. Hubs flood the forward search with unrelated papers.
- Drop components that were only used: backbones, detectors, prompt generators, and the evaluation datasets of a
  method paper.
- Add comparisons the script missed, e.g. ones that appear only in figures.
- Aim for roughly 8-25 core papers.

Apply your edits with:

```
python3 <skill>/scripts/fbsearch.py core --workdir <WORKDIR> --include id1,id2 --exclude id3 --role id4=baseline
python3 <skill>/scripts/fbsearch.py core --workdir <WORKDIR> --add 2310.11454=baseline   # a paper it missed
```

- The ids are the 8-character keys that `backward` prints.
- `core --workdir <WORKDIR> --all` lists every reference, including background ones.
- `--add` takes an arXiv ID, DOI or Semantic Scholar paperId. Use `fbsearch.py resolve "<title>"` to find one.
- Excluded papers are also kept out of the report.
- If the seed has no arXiv HTML, the script saves `paper.pdf` instead: read its tables yourself and set the core set
  with `core`.

### 3. Forward

```
python3 <skill>/scripts/fbsearch.py forward --workdir <WORKDIR> --query '<topic query>' --keywords '<phrases>'
```

- `--query` is a Semantic Scholar boolean query for a citation-sorted topic search. It catches older influential
  papers, because citer lists only reach a heavily cited paper's newest citers. Join 2-5 quoted key phrases of the
  seed's topic with `|`, e.g. `'"interactive segmentation" | "part segmentation" | "segment anything"'`. The syntax is
  `"phrase"`, `|` for OR, `+` for AND, `-` for NOT, and `prefix*`.
- `--keywords` takes comma-separated topic phrases, used to judge topical relevance from titles.
- Threshold overrides: `--min-citations 50`, or `--recent-min-citations 10 --recent-months 18`.
- It prints the kept papers (`B` = backward, `F` = forward) and writes `candidates.json` and `briefs.md`.
- Forward slots (`--max-forward`, default 60) are shared across lineages: the seed's own citers and the citers of
  each part of the comparison set. Inside each lineage, papers rank by relevance plus citations. Everything else
  that qualified lands in Appendix B. If papers the user would expect are only there (check `candidates.json`
  entries with `"bucket": "overflow"`), re-run `forward` with `--max-forward 100`. It is fast, because responses
  are cached.

### 4. Write the differences

Read `<WORKDIR>/briefs.md`, which has each kept paper's TL;DR or abstract and how it connects to the seed. Write
`<WORKDIR>/diffs.json`: one JSON object that maps each key to one concrete sentence on how that paper differs from the
seed. Name the axis that differs: task, input or prompt type, supervision, architecture, training data or domain,
granularity, evaluation. For example:

```json
{"1628a19c": "Refines masks from any segmenter with a separate post-processing network, whereas the seed adds a learned output token inside SAM and stays zero-shot."}
```

Write `"EXCLUDE: <reason>"` for an off-topic paper; it moves to a collapsed list. Stick to what the brief supports.
If a brief is empty, skim the paper or say what is known.

### 5. Report

```
python3 <skill>/scripts/fbsearch.py report --workdir <WORKDIR> --out <path>.md
```

Then reply with:

- the report path and how many papers it lists, with and without code;
- the 3-5 most useful findings, e.g. the strongest baselines with code or the newest influential follow-ups;
- any caveat the script printed, e.g. a missing reference list, a non-arXiv seed, or many Semantic Scholar retries.

## Rules

- **Never query Google Scholar** in any automated way: curl, fetch tools, the `scholarly` package, SerpApi, or a
  browser on its search pages. It has no API, robots.txt disallows it, and Google blocks automated traffic. The report
  links each paper to Google Scholar so the user can check counts themselves.
- Citation counts are Semantic Scholar's; say so. Google Scholar's are usually 1.4-1.8x higher.
- Only verified code links put a paper in the "with code" section. Weak matches are listed as "Possible code
  (unverified)"; check one before calling it official.
- Keep intermediate files in the workdir and hand the user the final report.

## Troubleshooting

| Script says | Do |
|---|---|
| "shared keyless pool is busy" | Normal without a key. Let it keep retrying and suggest `S2_API_KEY`. |
| "Could not resolve the seed unambiguously" | Re-run with one of the printed paperIds, or an arXiv ID / DOI. |
| "N of the M bibliography entries are unmatched, resolving them" | Nothing to do: it looks them up itself. |
| "No arXiv HTML. Saved the PDF" | Read `paper.pdf` and fix the core set with `core`. |
| "requests failed even after retries" | The network dropped or a service was down. Re-run the same command; cached parts are reused. |
| Few forward results | Broaden `--query`, add core papers, or lower the thresholds, then re-run `forward` (cached). |
| Many off-topic results | Remove hubs from the core set, tighten `--query` / `--keywords`, re-run `forward`. |

## Reference files

- `references/methodology.md`: how roles are inferred, candidate sources, relevance scoring, selection,
  thresholds, and how to judge the core set. Read it when a result looks wrong.
- `references/data-sources.md`: the APIs used, their limits and keys, and why Google Scholar is not used.
- `references/report-format.md`: the workdir files, the JSON formats, and the report layout.
