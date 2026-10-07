# Methodology

How `fbsearch.py` decides what goes into the report, and how to judge its output.

Contents:

1. Background: snowballing
2. Paper types and the core set
3. How reference roles are inferred (backward)
4. Where forward candidates come from
5. Relevance
6. Citation threshold
7. Selection and ordering
8. Judging the result

## 1. Background: snowballing

Backward snowballing reads a paper's reference list; forward snowballing collects the papers that cite it (Wohlin,
"Guidelines for snowballing in systematic literature studies", EASE 2014). Done naively, both directions are noisy:
a paper cites dozens of things it merely uses, and a popular baseline has tens of thousands of citers. This skill
narrows both directions to the seed's **comparison set**, i.e. the work the seed measures itself against, and then
keeps only influential, connected papers.

## 2. Paper types and the core set

The seed's type decides which references count as its comparison set:

| Type | Comparison set (backward) | Forward search starts from |
|---|---|---|
| method | methods in its results tables (baselines), closest related methods | seed + baselines + closest methods |
| dataset | datasets in its comparison / statistics table or reviewed in related work, closest related work | seed + compared datasets + closest related work |
| benchmark | benchmarks it positions against, models it evaluates | seed + compared benchmarks + evaluated models |
| mixed | the union | the union |

The type is guessed from the title and abstract ("we introduce a dataset / benchmark ..."). A paper that
introduces a dataset and also says it benchmarks or evaluates existing models ("We benchmark modern models ...") is
treated as `mixed`. For a pure dataset paper, the models it evaluates are still listed as backward papers, but they
don't drive the forward search. Their citers are mostly a neighbouring model literature, not the dataset's lineage.
Override the type with `--type`.

The **core set** is the subset marked `include: true` in `core.json`. It drives the forward search: candidates must
cite the seed or core papers to be kept, so a bad core set gives a bad report. `backward` proposes one; the agent
reviews it.

## 3. How reference roles are inferred (backward)

The script reads the seed's arXiv HTML (arxiv.org/html, else ar5iv), so it knows each table, each section and the
sentence around every citation. For each reference it collects this evidence, strongest first:

1. **Table row:** the citation sits in a row of a results table (`S4.T2(row)`), or the row names the method without
   citing it and the name matches a reference's short name (`S5.T1(row-label)`: "LoRA" matches "LoRA: Low-Rank
   Adaptation ...", "Prefix" matches "Prefix-Tuning"). For a method paper this is the classic baseline signal. In a
   table whose caption looks like a dataset comparison ("comparison with existing datasets", "statistics of
   benchmarks"), rows are compared datasets. Cells spanning several rows ("LLaMA-7B" above six methods) name the
   backbone or setting, so a reference matching one is treated as a component, not a baseline.
2. **Table header:** column headers usually name datasets (or benchmarks), so a header-only reference is treated as
   data: an evaluation dataset for a method paper, a compared dataset for a dataset paper.
3. **Comparison sentence:** a citation sentence with a strong cue ("compared with", "outperforms", "baseline",
   "unlike", "vs."). Sentences that also say "we use / adopt / follow / build on" are treated as **usage**, not
   comparison. So are citations whose marker directly follows "on X", "using X", "fine-tuning X" or "X backbone"
   ("outperforms LoRA when fine-tuning LLaMA [37]") in at least half of their occurrences.
4. **Related work:** the reference is cited in a related-work / background section.

Table **captions** are weak evidence: they mostly cite evaluation data or components ("using a FocalNet-DINO
detector [53]"). A reference seen only in captions becomes `eval_dataset` (if it looks like data) or `background`.
A table counts as a results table whenever its caption has a results cue ("accuracy", "comparison", "results"),
even if it also mentions hyperparameters. Only captions about configuration alone are ignored.

Roles: `baseline`, `benchmarked_model`, `compared_dataset`, `compared_benchmark`, `related`, `eval_dataset`,
`background`. The default core set takes every table-backed comparison role, plus the top `related` papers ranked by
evidence and **topic overlap**, i.e. the share of the reference title's words (crudely stemmed) that also occur in
the seed's title and abstract. A related paper needs an overlap of at least 0.2, or an explicit comparison sentence,
to be included by default. That keeps out hubs such as CLIP that are merely cited in passing.

When the HTML is missing (non-arXiv papers), the script falls back to Semantic Scholar's citation contexts and
intents. It saves `paper.pdf` so the agent can read the tables. Expect to adjust the core set more in that case.

**Missing references.** Semantic Scholar sometimes lists few or none of a paper's references (e.g. SAM 3 had
zero). It may also miss a key one (DoRA's list lacked LoRA), or know one only by title, without a paper id (DoRA's
VeRA). The script resolves every unmatched or title-only bibliography entry itself:

- arXiv IDs and DOIs found in the entries go through one batch lookup;
- titles go through an arXiv title search, then Semantic Scholar title matching.

It looks up at most `--max-bib-lookups` titles (default 25), in this order: entries cited or named in tables, then
entries in comparison sentences, then related work.

## 4. Where forward candidates come from

Semantic Scholar lists citers **newest first**, and only the first 10,000 can be reached. For a heavily cited
baseline, the older, influential citers are therefore out of reach through citer lists alone. The script combines
four sources:

1. **Citers of the seed:** up to 3,000 (9,999 with an API key), complete for most seeds.
2. **Newest citers of each core paper:** up to 1,000 (3,000 with a key). This finds recent work.
3. **Citation-sorted topic search** (`--query`): Semantic Scholar bulk search sorted by citation count, restricted to
   papers with at least the lower threshold (10 by default). This finds influential papers on the topic however old.
4. **OpenAlex most-cited citers** (optional, `--openalex-budget`): for core papers whose citers were only partly
   fetched, OpenAlex's `cites:` filter sorted by citation count gives up to 100 more. Each hit is re-resolved in
   Semantic Scholar and kept only if the titles match, because OpenAlex has bad merges and undercounts arXiv-heavy
   fields.

**Verification.** For every candidate above the citation threshold (up to `--verify-cap`, most cited first), the
script fetches the full reference list. That gives exact answers to "does it cite the seed?" and "how many core
papers does it cite?", whichever source found it.

- It uses batch calls of 100 papers. Larger batches silently return empty lists: in one test, 143 of 250 papers
  with references came back empty.
- Empty results are retried in batches of 10.
- Topic-search and OpenAlex hits that are still empty are checked one paper at a time, up to 40. Without
  verification they would have no known citation edge at all.
- The summary reports how many candidates had no retrievable reference list; typically 10-20%, from publisher
  restrictions.

## 5. Relevance

A forward candidate (a paper not cited by the seed) is **eligible** if any of these holds:

- it cites the seed;
- it cites core papers with a combined weight of at least 2;
- it cites at least one core paper and is topical (its title matches `--keywords`, or the topic search found it);
- it cites at least one core paper "influentially" (Semantic Scholar's flag).

Eligible papers are scored by

```
relevance = 1.5 x cites_seed + min(core_weight, 4) x (1.0 if topical else 0.6) + 0.5 x topical + 0.5 x influential
rank      = relevance + log10(1 + citations)
```

Capping the core weight at 4 matters: papers that cite every classic baseline (e.g. a small LoRA variant citing
ten PEFT methods) would otherwise outrank influential follow-ups such as a 2,000-citation toolkit that cites the
seed. The log-citation term then orders by influence among comparably relevant papers.

- `core_weight` is the number of core papers cited, with **hubs** counting 0.5. A hub is a core paper with at least
  max(5,000, 10 x the seed's citations) citations (constant `HUB_MIN_CITATIONS`). ViT, CLIP and GPT-3 are hubs for
  almost any seed, and SAM is a hub for a SAM-variant seed: nearly everyone in the area cites it, so citing it says
  little.
- Co-citation without a topic match counts 0.6x, because a neighbouring field can cite the same famous papers.
- Surveys and reviews are capped at relevance 2.5: they cite everything, so they are kept but rank below focused
  work.

Backward papers are references of the seed. They come from the core set, plus non-core references with a comparison
role, plus related-work references with topic overlap of at least 0.2. Papers removed with `core --exclude` are left
out entirely.

## 6. Citation threshold

The defaults are age-adjusted: at least 30 citations, or at least 10 for papers published within the last 18 months.
That way strong recent work isn't dropped just for being new. Counts are Semantic Scholar's, which typically run
55-70% of Google Scholar's for ML papers. Change the threshold with `--min-citations`, `--recent-min-citations` and
`--recent-months`.

Core papers below the threshold still appear, in Appendix A ("directly compared, but below the citation threshold"),
because the user usually wants to know about them.

## 7. Selection and ordering

- **Backward:** the best `--max-backward` (30) papers, core papers first, then by connectedness and citations.
- **Forward:** `--max-forward` (60) slots, shared **across lineages**. A paper's lineage is "seed" if it cites the
  seed, otherwise the role of the core papers it mostly cites (baseline, compared dataset, benchmarked model,
  related), else "topic". Each lineage gets slots in proportion to the square root of its size, with the seed's own
  citers weighted double, and is consumed best rank first. So one tightly co-cited cluster (the many papers citing a
  dataset paper's benchmarked models) cannot take every slot, and a small lineage cannot pad the list with weak
  papers.
- Everything else that qualified goes to Appendix B (overflow, not reviewed).

The report then sorts the kept papers by **citations, then GitHub stars / Hugging Face likes**. Section 1 holds
papers with verified code; Section 2 the rest, with any unverified code candidates in their own column.

## 8. Judging the result

- **Core set too broad** (hubs, used components): the forward list fills with generic papers. Remove them with
  `core --exclude` and re-run `forward`, which is fast because responses are cached.
- **Core set too narrow:** few forward papers. Add related methods with `core --include` (see `core --all`).
- **Topic query too broad** (e.g. just `"segment anything"` for a niche seed): many papers that only cite the one
  famous baseline. Make the query specific to the seed's angle.
- **Expected papers only in Appendix B:** raise `--max-forward` (e.g. 100) and re-run `forward`.
- **Seed has no reference list and no HTML:** read the PDF and set the core set by hand. The forward search still
  works.
- **Off-topic papers that survive:** mark them `EXCLUDE: <reason>` in `diffs.json`.
