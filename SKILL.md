---
name: citation-snowball
description: "Turns one or more seed papers into a short, ranked reading map instead of a long list. (A) what the seed compares against in its tables, plus the foundations it and those works all cite (datasets, benchmarks, base models, found by co-citation); (B) influential follow-ups: papers citing the seed with at least 50 citations, fewer if recent, or a popular, well-documented GitHub repo, widened to recent on-topic work on the compared models when few cite the seed; (C) the latest models recent work in the area builds on, found from the citing side even when Semantic Scholar has no reference list for them. Each paper gets code with stars and a README-quality score, one line on why to read it, a 'read these first' list and BibTeX. Use when the user shares a paper they like (arXiv link or ID, DOI, title, OpenReview or Semantic Scholar URL), or several, and wants related work, baselines, datasets or foundations, papers that cite it, follow-up or recent SOTA work, models to watch, a reading list, or code."
metadata:
  version: "1.4.0"
  requirements: "Python 3.8+ (standard library only) with internet access to Semantic Scholar, arXiv, Hugging Face, GitHub and OpenAlex. Optional: S2_API_KEY for much faster runs, a gh CLI login or GH_TOKEN for GitHub stars and READMEs."
---

# Citation snowball: a reading map from the papers you like

Turn one paper the user likes (the **seed**), or a few, into a short map of what to read and cite next. The seed is
assumed to be good: what it compares against is worth knowing, what it and those works all cite is the field's
foundation, and the forward search keeps only influential work in the same area.

| Section | What it holds | How it is chosen |
|---|---|---|
| **Read these first** | 5-8 picks across the sections, one line each | your judgment (or automatic) |
| **A1. What the seed compares against** | every baseline, benchmarked model, compared dataset and benchmark in its tables | none: the seed's authors vetted them |
| **A2. What the field builds on** | ≤6 datasets/benchmarks, then ≤6 models/methods | co-cited by ≥20% (and 3) of the seed + compared works and still cited by recent area papers, or older papers ≥25% of recent area papers cite |
| **B. Influential follow-ups** | ≤15 papers citing the seed; if fewer than 8 qualify, also recent on-topic papers citing the compared works | the influence gate below, plus the same-area check: cites one of the 30 strongest A2-style co-citations |
| **C. Latest models to keep an eye on** | ≤8 papers from the last 30 months | cited by ≥15% of the area papers published after them |

The **recent area** is the papers from the last 30 months that cite the seed, or cite a compared work and match the
topic. C works from the citing side, so it finds new models no citation search reaches: Semantic Scholar has no
reference list for SAM 3 or Qwen2.5-VL, but a quarter of InstructPart's recent area cites them.

**Influence gate.** A follow-up is kept if it has enough citations for its age, or, instead, enough GitHub stars on a
verified repo whose README documents the implementation (at least 4 of: setup, usage, training, evaluation, released
weights). Counts are Semantic Scholar's.

| Follow-up | Any age | Published ≤ 12 months ago | 12-24 months | Older |
|---|---|---|---|---|
| cites the seed | ≥50 citations, or ★≥1000 | ≥10 citations, or ★≥100 | ≥25 citations, or ★≥250 | (any-age bar only) |
| widening: cites a compared work, on topic | - | ≥20 citations, or ★≥150 | ≥40 citations, or ★≥250 | left out (`--gate expand.old=200` reopens) |

B is ranked by a SOTA score: citations per month plus half the stars per month (log scale), a small bonus for the
last 12 months, and a penalty for surveys (at most one survey per section). A helper script does all the API work.
You supply judgment at three checkpoints: the core set (step 2), the keywords that define the topic (step 3), and the
one-line reasons, picks and dataset labels (step 4).

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
  If the key is in a shell profile your environment did not load, load just that line (e.g.
  `eval "$(grep -E '^export S2_API_KEY=' ~/.zshrc)"`) without printing it. GitHub stars and READMEs use `GH_TOKEN`
  or the gh CLI login when present.
- `forward` can stay quiet for minutes while it retries. Run it in the background if your environment supports that,
  and don't kill it for being slow. Results are cached in `~/.cache/citation-snowball`, so re-runs are fast.
- **Updates.** `backward` checks GitHub for a newer release at most once a day. When there is one, it prints an
  `UPDATE` line. Tell the user once, with the "what's new" link, and ask before updating. Don't update in the middle
  of a run, because a new version may change the workdir format; run `fbsearch.py update` before the next run
  instead. It does a `git pull --ff-only` in the skill's folder, and refuses if there are local changes or the
  folder is not on `main`. A copy installed as a Claude Code plugin updates through `/plugin` (or by itself once
  auto-update is on), and the `UPDATE` line says so.
  `FBS_NO_UPDATE_CHECK=1` turns the check off.

## Workflow

### 0. Pin down the seeds and preferences

- **Seeds:** one or more of an arXiv ID or URL, DOI, Semantic Scholar / OpenReview / ACL Anthology URL, or the exact
  title. Several seeds share one map: their comparison sets are merged, and B holds papers citing any of them.
- **Defaults,** unless the user says otherwise: the gates above, A2 ≤ 6 + 6, B ≤ 15, C ≤ 8 over 30 months.
  - "Only papers with at least N citations" → `--min-citations N` (a hard floor for B; stars no longer count).
  - "Always include recent work on the baselines" → `--expand always`; "only papers citing the seed" →
    `--expand never`.
  - Different bars → `--gate ROUTE.BRACKET=CITES[,STARS]`, e.g. `--gate seed.recent=5,100`; more or fewer rows →
    `--max-follow-ups`, `--max-foundations`, `--max-latest`; a longer "latest" window → `--latest-months 36` (see
    `forward --help`).
- **Output path:** default `<seed-short-name>-reading-map.md` in the current directory; the `.bib` goes next to it.

### 1. Backward

```
python3 <skill>/scripts/fbsearch.py backward "<seed>" ["<seed 2>" ...]
```

It prints `WORKDIR`, then for each seed the paper type it guessed (method / dataset / benchmark / mixed), the tables
it parsed and where its full text is, and finally a merged **PROPOSED CORE SET**: every relevant reference with a
role (baseline, benchmarked model, compared dataset or benchmark, related work, evaluation dataset) and the evidence,
e.g. `in S4.T2(row)` or `compared: "..."`. If a type is wrong, re-run `backward` for that seed with
`--workdir <WORKDIR> --type method|dataset|benchmark|mixed`; the other seeds in the workdir are kept, and it is fast
because of the cache.

### 2. Review the core set

This is your judgment, and it decides section A1 outright: **every included paper with a table role (baseline,
benchmarked_model, compared_dataset, compared_benchmark) is listed in the report, whatever its citation count.**
The included papers are also the "compared works" whose reference lists define A2 and whose citers widen B.

Check the proposal against the paper. Each seed's `<WORKDIR>/seeds/<key>/fulltext.txt` has the whole paper, one line
per paragraph and per table row (cells separated by ` | `), and `digest.json` has the table captions, row labels and
related-work text. A good core set is:

- **Method paper:** every method in its main results tables (role `baseline`), plus the 5-10 closest methods from
  related work (role `related`).
- **Dataset paper:** the datasets it compares against in a comparison or statistics table (`compared_dataset`), plus
  the closest related work.
- **Benchmark paper:** the benchmarks it positions itself against (`compared_benchmark`) and every model it evaluates
  (`benchmarked_model`).
- **Mixed paper** (e.g. a new dataset whose paper also benchmarks models): both.

Rules of thumb:

- Make sure every model row of the main results table is in, with a table role. The proposal often misses models
  that are named in rows without a citation, or cited only in related work; fix them with `--include` and `--role`.
- Drop generic hubs cited in passing, such as GPT-3, ViT, CLIP, SAM, DINOv2, ResNet or Adam, unless the seed
  directly compares against them in a table. Dropping them from the core does not hide them: if the field really
  builds on them, A2 lists them.
- Drop components that were only used: backbones, detectors, prompt generators, and the evaluation datasets of a
  method paper.
- Add comparisons the script missed, e.g. ones that appear only in figures.
- Aim for roughly 8-25 core papers.

```
python3 <skill>/scripts/fbsearch.py core --workdir <WORKDIR> --include id1,id2 --exclude id3 --role id4=baseline
python3 <skill>/scripts/fbsearch.py core --workdir <WORKDIR> --add 2310.11454=baseline   # a paper it missed
```

- The ids are the 8-character keys that `backward` prints.
- `core --workdir <WORKDIR> --all` lists every reference, including background ones.
- `--add` takes an arXiv ID, DOI or Semantic Scholar paperId. Use `fbsearch.py resolve "<title>"` to find one.
- `--exclude` keeps a paper out of A1 and the search. To remove it from the report entirely, use `EXCLUDE` in
  `diffs.json` (step 4).
- If a seed has no arXiv HTML, the script saves `paper.pdf` instead: read its tables yourself and set the core set
  with `core`.
- Re-running `backward` rebuilds the core set, so make `core` edits afterwards.

### 3. Forward

```
python3 <skill>/scripts/fbsearch.py forward --workdir <WORKDIR> --query '<topic query>' --keywords '<phrases>'
```

- `--keywords` defines the seed's topic: 6-15 comma-separated phrases. **Widening keeps only papers that match one**
  (in the title, or, for multi-word phrases, in the abstract) **or that the topic search found,** and the recent area
  behind A2 and C is built the same way. Prefer specific multi-word phrases (`reasoning segmentation`,
  `part grounding`, `low-rank adaptation`) and avoid bare generic words (`segmentation`, `transformer`,
  `language model`): those let in every neighbouring paper.
- `--query` is a Semantic Scholar boolean query for the citation-sorted topic search used when widening. Join 3-7
  quoted key phrases with `|`, e.g. `'"interactive segmentation" | "part segmentation"'`. The syntax is `"phrase"`,
  `|` for OR, `+` for AND, `-` for NOT, and `prefix*`.
- What it does, in order:
  1. Reads the reference lists of the seed and its compared works (with their citation sentences) and counts what
     they all cite.
  2. Screens every paper citing the seed. Papers below the citation bars get a cheap GitHub stars probe, and the ones
     that pass get their reference lists checked for the same-area check.
  3. If fewer than 8 pass, or fewer than 30 recent papers cite the seed, it widens: it screens recent citers of the
     compared works (hubs are skipped), the topic search and OpenAlex's most-cited recent citers, and checks
     reference lists, abstracts and stars.
  4. Reads the reference lists of up to 1,000 recent area papers and counts what they cite.

  It saves everything to `pool.json`, then runs `select`. That writes `candidates.json` and `briefs.md`, and prints the
  kept papers by section (`A1`, `A2`, `B`, `C`) with the reason each was kept.
- Read the `WIDEN` line (whether B was widened and why) and the `COCITE` line (how many reference lists the
  co-citation counts rest on).

### 4. Write the one-line reasons, picks and labels

Read `<WORKDIR>/briefs.md`, which has each kept paper's TL;DR or abstract, why it was kept and its code. Write
`<WORKDIR>/diffs.json`: one JSON object that maps each key to one sentence (at most ~30 words) on **why to read it**:
what it contributes and how it differs from the seed. Name the axis that differs: task, input or prompt type,
supervision, architecture, training data or domain, granularity, evaluation. For a foundation, say what it gives the
field ("the referring-expression benchmark the compared models report on"); for a latest model, what to try it for.
For example:

```json
{
  "1628a19c": "Refines masks from any segmenter with a separate post-processing network, whereas the seed adds a learned output token inside SAM and stays zero-shot.",
  "ad113d8b": {"diff": "Defines reasoning segmentation and the [SEG]-token MLLM the seed extends to parts; the strongest baseline with code.", "pick": 1},
  "29efbe39": {"diff": "Releases RefCOCO and RefCOCO+, the referring benchmarks the compared models report on.", "kind": "data"},
  "5d7cd5c4": "EXCLUDE: medical-imaging application that only uses SAM off the shelf"
}
```

- `"pick": 1`, `2`, ... (or `true`) puts a paper in **Read these first**, in that order. Pick 5-8 a newcomer should
  read first, usually:
  - the 1-2 most important foundation datasets or benchmarks;
  - the strongest compared baselines with code;
  - the best follow-ups;
  - 1-2 latest models.

  Without any pick, the report chooses 2 of each automatically.
- `"kind": "data"` or `"model"` fixes A2's split when a paper is filed in the wrong table. The script guesses from
  the title, the abstract and how the compared works cite it; dataset papers that introduce themselves by name
  (RefCOCO+, PACO) are often missed.
- `"EXCLUDE: <reason>"` drops an off-topic paper from every section.
- After any `kind` change or `EXCLUDE`, run `python3 <skill>/scripts/fbsearch.py select --workdir <WORKDIR>`. It takes
  seconds and is cached. The next candidates move up, and it lists the keys that still need a sentence.
- Stick to what the brief supports. If a brief is empty, skim the paper or say what is known.

### 5. Report

```
python3 <skill>/scripts/fbsearch.py report --workdir <WORKDIR> --out <path>.md
```

It writes the reading map and `<path>.bib`. Then reply with:

- the report path and the counts per section (A1 / A2 / B / C, and how many have code), plus the `.bib` path;
- the read-first list in a few lines, whether B was widened and why, and which latest models C found;
- any caveat the script printed, e.g. a missing reference list, a non-arXiv seed, no GitHub token, few recent area
  papers, or many Semantic Scholar retries.

## Rules

- **Never query Google Scholar** in any automated way: curl, fetch tools, the `scholarly` package, SerpApi, or a
  browser on its search pages. It has no API, robots.txt disallows it, and Google blocks automated traffic. The report
  links each citation count to a Google Scholar search so the user can check counts themselves.
- Citation counts are Semantic Scholar's; say so. Google Scholar's are usually 1.4-1.8x higher.
- Only verified code links count: they put a paper's code in the table and are the only stars the gate accepts.
  Weak matches are shown as "maybe ... (unverified)"; check one before calling it official.
- Keep intermediate files in the workdir and hand the user the final report.

## Troubleshooting

| Script says | Do |
|---|---|
| "shared keyless pool is busy" | Normal without a key. Let it keep retrying and suggest `S2_API_KEY`. |
| "Could not resolve the seed unambiguously" | Re-run with one of the printed paperIds, or an arXiv ID / DOI. |
| "N of the M bibliography entries are unmatched, resolving them" | Nothing to do: it looks them up itself. |
| "No arXiv HTML. Saved the PDF" | Read `paper.pdf` and fix the core set with `core`. |
| "requests failed even after retries" | The network dropped or a service was down. Re-run the same command; cached parts are reused. |
| "made by citation-snowball 1.2 or earlier" | Re-run `backward` into a new workdir. |
| `WIDEN no: needed, but forward did not search it` | Re-run `forward` with the same arguments (cached). |
| A2 or C empty, or `COCITE` shows few lists | Few reference lists to count on: add compared works (`core --include`), broaden `--keywords`, or lengthen `--latest-months`. |
| A2 lists a paper in the wrong table | Set `"kind"` in `diffs.json`, then run `select`. |
| B too thin | Add core papers, broaden `--keywords` / `--query`, relax a gate (`--gate seed.mid=15`), or `--min-coupling 0` if the same-area check is too strict for this seed; then re-run `forward`. |
| B off-topic | Make `--keywords` more specific (drop bare generic words), remove generic models from the core set, re-run `forward`. |
| An expected paper is missing | Look it up in `pool.json`. Not in `papers` means it was never screened as a follow-up (add the core paper it builds on, or broaden the query). In `cocitation.meta` means it was counted for A2 / C but fell below the bars. |

## Reference files

- `references/methodology.md`: how roles are inferred, the co-citation counts behind A2 and C, the influence gate,
  the same-area check, the SOTA score, selection, and how to judge the result. Read it when a result looks wrong.
- `references/data-sources.md`: the APIs used, their limits and keys, and why Google Scholar is not used.
- `references/report-format.md`: the workdir files, the JSON formats, and the report layout.
