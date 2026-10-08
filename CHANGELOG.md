# Changelog

All notable changes to citation-snowball are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): see [CONTRIBUTING.md](CONTRIBUTING.md#versioning) for
what counts as major, minor and patch here.

## [Unreleased]

### Changed

- **A shorter README, in the order you need it:** prerequisites (with where to get a Semantic Scholar key), then
  install (the Claude Code plugin first), use, and what you get. The flow diagram and the limitations moved to
  [`references/methodology.md`](references/methodology.md).

## [1.4.0] - 2026-10-08

**Know when there is something new.** The skill now tells you when a newer release is out, in every agent that runs
it (Claude Code, Codex, Copilot), and updates itself with one command when you agree.

### Added

- **Update check.** `backward` asks GitHub for the latest release of the skill at most once a day, with a 3-second
  timeout.
  - The answer, or the failure, is cached in `~/.cache/citation-snowball/update-check.json`.
  - When a newer release exists, the summary shows an `UPDATE` line with the "what's new" link. SKILL.md tells the
    agent to mention it once and ask before updating.
  - The check never breaks a run, and `FBS_NO_UPDATE_CHECK=1` turns it off.
- **`fbsearch.py update`.** Runs `git pull --ff-only` in the skill's folder and prints the old and new version.
  - It refuses, and says what to do, when the folder has local changes, is on a branch other than the one releases
    land on (`main`), or is not a git clone (then it says where to download the release).
  - Claude Code, Codex and Copilot read the same folder, so one update covers all three.
- **Claude Code plugin marketplace** (`.claude-plugin/marketplace.json`).
  - Claude Code users can install with `/plugin marketplace add Andy-LZH/citation-snowball` and
    `/plugin install citation-snowball@citation-snowball`.
  - Turning on auto-update under `/plugin` → Marketplaces makes new releases install themselves.
  - The repo root stays the skill, so `git clone` installs work as before. There is deliberately no `plugin.json`,
    which would make a clone in `~/.claude/skills` load twice.
  - Validated with `claude plugin validate` (passes), and loads as one skill with no agents or hooks.
- **Plugin-aware updates.** In a copy installed through `/plugin`, the `UPDATE` line and `fbsearch.py update` point
  to `/plugin` instead of git, because Claude Code manages that folder.
- **`doctor` shows the latest release** next to the installed version, and a tip when an update is available.
- `FBS_UPDATE_REPO=owner/name` points the check at a fork.

## [1.3.0] - 2026-10-08

**A reading map instead of a long list.** 1.2 listed every influential paper it could find; on a 2025 benchmark
seed (InstructPart) it kept 81 papers plus 641 overflow candidates. 1.3 assumes the seed is good and answers four
questions:

- what does it compare against;
- what do it and those works all build on;
- who influential has built on it;
- which new models does recent work in the area rely on.

The same seed now gives 45 papers in four short sections, 8 of them in "read these first", plus a BibTeX file. The
foundations include ADE20K and the RefCOCO family, and the latest models are SAM 3, Qwen3-VL, Qwen2.5-VL and SAM 2.
See [examples/instructpart.md](examples/instructpart.md).

### Added

- **Four sections.**
  - **A1:** every paper the seed compares against in its tables, with no citation bar.
  - **A2:** what the field builds on (co-citation, below).
  - **B:** influential follow-ups.
  - **C:** the latest models to keep an eye on.
- **Backward co-citation (A2).** The reference lists of the seed and its compared works are counted. A paper cited by
  at least 20% (and 3) of them is a foundation when recent work in the area still cites it (at least 5% and 3
  papers). An older paper also qualifies when at least 25% of recent area papers cite it. Both need 100 citations.
  - Ranked by (backward share + forward share) × log(corpus size / citations), so COCO and CLIP, cited everywhere,
    rank below an equally co-cited specialist paper.
  - Datasets and benchmarks are shown before models and methods, up to 6 of each. The split comes from the title,
    the abstract and how the compared works cite the paper ("we evaluate on RefCOCO [12]"); the agent can correct
    it with `"kind"` in `diffs.json`.
  - Journal and conference versions of one dataset are merged into one row (the two ADE20K records).
- **Forward co-citation (C).** The reference lists of up to 1,000 recent area papers are counted. The recent area is
  papers from the last `--latest-months` (30) that cite the seed, or cite a compared work and match the topic.
  - A paper from the same window is a latest model when at least 15% (and 4) of the area papers published after it
    cite it, out of at least 10 that could have.
  - This reaches models no citation search can. Semantic Scholar has no reference list for SAM 3 or Qwen2.5-VL, so
    they never appear as citers of anything; 22-25% of InstructPart's recent area cites them.
- **Influence gates with a stars route** for follow-ups. A paper passes on citations for its age, or on GitHub stars
  of a verified repo whose README is well documented. Defaults (Semantic Scholar counts):

  | Follow-up | Any age | ≤ 12 months | 12-24 months | Older |
  |---|---|---|---|---|
  | cites the seed | ≥50 citations or ★≥1000 | ≥10 or ★≥100 | ≥25 or ★≥250 | - |
  | widening: cites a compared work, on topic | - | ≥20 or ★≥150 | ≥40 or ★≥250 | left out |

  The bars were calibrated on the InstructPart run.
  - Opening the older bracket for widening gave 4 of 10 slots to 2023 papers.
  - A 12-24-month bar of 50 citations / ★300 dropped the 2025 work it is for: UniPixel (40 citations), Seg-R1 (42)
    and UFO (★285).

  Override any bar with `--gate ROUTE.BRACKET=CITES[,STARS]`. `-` closes a bracket, and `--gate expand.old=200`
  reopens the older one (it then also needs citations of 2 compared works).
- **Same-area check for follow-ups.** Each must cite one of the 30 strongest backward co-citations. CRISP, a real2sim
  method that cites InstructPart but none of its field's foundations, is left out. Papers whose reference list is
  unavailable pass on their citation of the seed. `--min-coupling 0` turns the check off.
- **Widening.** When fewer than `--min-forward` (8) papers citing the seed pass, or fewer than `--min-area` (30) recent
  papers cite it, B also takes recent, on-topic papers citing the compared works. The rows are marked "builds on
  …" and balanced across the compared works.
- **README docs score (0-5).** One point each for setup, usage, training, evaluation and released weights. The stars
  route needs at least `--min-docs` (4), or twice the stars when the README cannot be read.
- **SOTA score** for ranking follow-ups: citations per month plus half the GitHub stars per month (log scale), +0.5
  for the last 12 months, -0.5 for surveys, and at most one survey per section.
- **Several seeds per map.** `backward A B C` merges their comparison sets. Each core paper records which seeds
  compare against it, and B holds papers citing any seed.
- **`select` command.** Re-applies gates, caps, `EXCLUDE`s and `kind` fixes to the saved pool in seconds. Excluded
  papers leave every section, and the next candidates move up.
- **`pool.json`.** Everything `forward` screened, including the co-citation counts, so a later `select` needs no new
  search.
- **"Read these first"** at the top of the report. The agent picks with `{"diff": "...", "pick": 1}` in
  `diffs.json`; without picks, the report takes 2 foundation datasets, 2 compared papers, 2 follow-ups and 2 latest
  models.
- **BibTeX file** next to the report: Semantic Scholar's entries with a `url` field added, deduplicated keys, and
  a fallback built from metadata. `report --no-bib` turns it off.
- The widening search only looks where recent, relevant work can be:
  - the newest citers of non-hub compared works;
  - a topic search limited to the last 30 months (`publicationDateOrYear`);
  - OpenAlex's most-cited citers since that date (`from_publication_date`);
  - abstracts for topic matching.
- New `forward` / `select` flags:
  - gates: `--gate`, `--min-citations` (a hard floor), `--min-docs`, `--recent-months`, `--mid-months`;
  - widening: `--min-forward`, `--min-area`, `--expand auto|always|never`;
  - caps: `--max-follow-ups`, `--max-foundations`, `--max-latest`, `--max-also`;
  - co-citation: `--latest-months`, `--min-coupling`, `--area-cap`;
  - code: `--probe-cap`.
- Offline test suite (`tests/`, standard library `unittest`, 100+ tests), GitHub Actions CI on Python 3.8 and 3.13,
  `CONTRIBUTING.md`, and issue and pull request templates.
- `doctor` prints the script version.

### Changed

- **Report layout.**
  - "Reading map" header and an at-a-glance table.
  - Read these first, then sections A1/A2, B and C, with compact columns (Paper · Year · Cites · Code · Why read it).
  - Paper titles bold their short name, and each citation count links to a Google Scholar search.
  - Code cells show the repo, stars and README docs score.
- **Comparisons have no citation bar.** 1.2 applied the threshold to the papers the seed compares against and moved
  the rest to an appendix.
- **Widening requires a topic match.** A paper must match `--keywords` (in its title, or, for multi-word phrases, its
  abstract) or have been found by the topic search. 1.2 also accepted papers that only co-cited two core papers,
  which let in every new general-purpose model that cites the same famous baselines.
- **`core --exclude` no longer hides a paper from the whole report.** It keeps the paper out of A1 and the citer
  search; a hub such as SAM still appears in A2 when the field builds on it. `EXCLUDE` in `diffs.json` removes it
  everywhere.
- **Workdir layout.** `seeds.json` lists the seeds. Each seed's `seed.json`, `refs.json`, `digest.json`,
  `fulltext.txt` and per-seed `core.json` live in `seeds/<key>/`; the merged `core.json` stays at the top.
- `forward` fetches the citers of compared works only when it widens, so well-cited seeds finish much faster.
- `briefs.md` and `diffs.json` ask for **why to read** each paper: what it contributes and how it differs from the
  seed.
- GitHub lookups also fetch `createdAt` (for stars per month). READMEs are read through the API, or from
  raw.githubusercontent.com without a token.
- The SKILL.md description states the new sections and bars.

### Fixed

- **Hugging Face artifacts that only cite a paper are no longer shown as its code.** 1.2 verified any artifact owned by
  the GitHub repo's owner, so Set-of-Mark showed Microsoft's unrelated Magma-8B. An artifact now has to carry the
  paper's short name or repo name, or cite no other arXiv paper.
- The GitHub search fallback no longer discards a repo described as "Official implementation of **the paper** ...".
  The paper-list filter matched the singular "paper"; this lost SEEM's repo (★4.8k).
- The GitHub search fallback accepts a repo whose description names the paper. For example, MiniGPT-v2 lives in
  Vision-CAIR/MiniGPT-4, "Open-sourced codes for MiniGPT-4 and MiniGPT-v2".
- The most-starred repo named exactly like the paper's short name counts as its own (SAM 2 → facebookresearch/sam2,
  ★20k, which 1.2 left unverified).

### Deprecated

- `--max-forward` now sets `--max-follow-ups`.
- `--recent-min-citations` now sets `--gate seed.recent=N`.
- `--max-backward` is ignored: section A1 lists every compared paper.

All three print a notice and will be removed in 2.0.

### Removed

- Appendix A ("directly compared, but below the citation threshold"): those papers are now in section A1.
- Appendix B (up to 80 unreviewed overflow papers): replaced by a collapsed "Also qualified" list of at most
  `--max-also` (20).
- The "Found via" and separate Google Scholar columns.
- Lineage balancing across core roles for the seed's own citers. Widening still balances across the compared works.

### Migration from 1.2

- 1.2 workdirs cannot be reused. Re-run `backward`; the HTTP cache makes it fast.
- `--min-citations N` is now a hard floor for follow-ups, and switches off the stars routes. To get something close
  to 1.2's breadth, use `--gate seed.any=30 --gate seed.recent=10 --max-follow-ups 60 --min-coupling 0`.

## [1.2.0] - 2026-10-06

First public release. Earlier versions were developed privately.

### Added

- `backward`: resolves the seed (arXiv ID or URL, DOI, Semantic Scholar, OpenReview, ACL Anthology, or title).
  - It reads the seed's arXiv HTML (tables, sections, citation sentences) to give each reference a role: baseline,
    benchmarked model, compared dataset or benchmark, related work, evaluation dataset or background.
  - It recovers references that Semantic Scholar lacks from the bibliography, with Crossref and PDF fallbacks.
- `core`: review and edit the proposed comparison set.
- `forward`:
  - candidates are the seed's citers, the newest citers of each core paper, a citation-sorted topic search and
    OpenAlex's most-cited citers;
  - reference lists are verified in batches;
  - relevance is scored with half-weight hubs, and slots are balanced across lineages;
  - the threshold was 30 citations, or 10 within 18 months.
- Code discovery:
  - repos linked from Hugging Face paper pages and arXiv comments, with a GitHub search fallback;
  - Hugging Face models, datasets and spaces;
  - verified and unverified links kept apart.
- `report`: markdown with code-first sections sorted by citations, two appendices, and a list of papers excluded as
  off-topic.
- `doctor`, `resolve` and `code` helpers, and an HTTP cache in `~/.cache/citation-snowball`.
- Works in Claude Code, OpenAI Codex and GitHub Copilot.
