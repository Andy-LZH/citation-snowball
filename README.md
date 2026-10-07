# citation-snowball

An [Agent Skill](https://agentskills.io) that turns one paper you like into a ranked, code-linked map of the
influential work around it. It runs a **forward and backward citation search** ("snowballing").

Works with **Claude Code**, **OpenAI Codex** and **GitHub Copilot** (VS Code and Copilot CLI), and any other agent
that supports `SKILL.md` skills.

## What it does

Give it a seed paper: an arXiv link or ID, DOI, title, or OpenReview / Semantic Scholar URL.

1. **Backward:** reads the paper's own results tables, dataset-comparison tables and related work to find what it
   compares against. That depends on the paper type:
   - method papers: the baselines;
   - dataset papers: the compared datasets;
   - benchmark papers: the benchmarks and evaluated models.

   It also picks out the closest related work.
2. **Forward:** finds influential papers that cite the seed or that comparison set, then checks each candidate's
   reference list. It keeps papers with at least 30 citations, or at least 10 if published in the last 18 months.
3. **Code:** finds each paper's GitHub repo (with stars) and Hugging Face models, datasets and spaces (with likes).
4. **Report:** writes one markdown file listing every kept paper with its citation count, code links and a
   one-sentence note on how it differs from your paper.
   - Papers with verified code come first.
   - Within each section, papers are sorted by citations, then stars / likes.
   - Each paper has a Google Scholar link so you can check its count.

The agent supplies judgment at two points: confirming the comparison set, and writing the per-paper differences.
A helper script does all the API work.

## Install

**Claude Code**

```sh
git clone https://github.com/Andy-LZH/citation-snowball ~/.claude/skills/citation-snowball
```

**Claude Code + Codex + Copilot**, with one shared copy:

```sh
git clone https://github.com/Andy-LZH/citation-snowball ~/.agents/skills/citation-snowball
ln -s ~/.agents/skills/citation-snowball ~/.claude/skills/citation-snowball
```

- Codex and VS Code Copilot read `~/.agents/skills` directly.
- Copilot CLI needs version 1.0.11 or later.
- Codex sandboxes network access by default; approve it when the skill runs.

**Update:** `git -C ~/.agents/skills/citation-snowball pull` (or the `~/.claude/skills` path).

**claude.ai:** zip the `citation-snowball` folder and upload it under Customize → Skills. The script needs internet
access to the APIs below, so it only works if your code-execution settings allow it.

## Use

```
/citation-snowball https://arxiv.org/abs/2306.01567
```

Or ask in plain language, e.g. *"What should I compare DoRA against, and who has built on it since?"*. In Codex,
mention `$citation-snowball`.

## Requirements and setup

- Python 3.8+ (standard library only) and internet access.
- **Recommended: a free [Semantic Scholar API key](https://www.semanticscholar.org/product/api#api-key-form).**
  Without one, requests go through a shared pool that is often busy. The script retries patiently, so a run takes
  5-20 minutes instead of a minute or two.
  ```sh
  echo 'export S2_API_KEY=your-key' >> ~/.zshrc
  ```
- Optional: `gh auth login` (or `GH_TOKEN`) for GitHub star counts beyond the 60-requests-an-hour anonymous limit.
- Check your setup:
  ```sh
  python3 ~/.claude/skills/citation-snowball/scripts/fbsearch.py doctor
  ```

## How it works

| Stage | Command | Output |
|---|---|---|
| Backward | `fbsearch.py backward "<seed>"` | Proposed comparison set, with each reference's role and evidence (e.g. `in Table 2 (row)`) |
| Review | `fbsearch.py core --workdir W ...` | The agent adjusts the core set: drops generic hubs and backbones, adds misses |
| Forward | `fbsearch.py forward --workdir W --query ... --keywords ...` | Candidates, verified and ranked, with code links |
| Differences | the agent writes `diffs.json` | One sentence per paper |
| Report | `fbsearch.py report --workdir W --out report.md` | The markdown report |

**Backward**

- The comparison set is read from the paper's arXiv HTML. Signals include:
  - citations or method names in results-table rows;
  - dataset-comparison tables;
  - comparison sentences;
  - the related-work section.
- Table captions and "we use X" sentences count as weak evidence, and backbones named in spanning table cells are
  treated as components, not baselines.
- References that Semantic Scholar is missing are recovered from the bibliography.

**Forward**

- Candidates come from:
  - the seed's citers;
  - the newest citers of each comparison paper;
  - a citation-sorted topic search (Semantic Scholar only returns a paper's newest citers);
  - optionally, OpenAlex's most-cited citers.
- Every candidate's reference list is checked, which gives exact co-citation counts.
- Ranking combines relevance (citing the seed, citing several comparison papers, topic match) with citation count.
- Famous "hub" papers count half, and slots are shared across lineages, so one cluster cannot fill the list.

Details: [`references/methodology.md`](references/methodology.md),
[`references/data-sources.md`](references/data-sources.md), [`references/report-format.md`](references/report-format.md).

## Data sources

- **Semantic Scholar:** citations, references, counts and abstracts.
- **arXiv:** full text and metadata.
- **Hugging Face:** paper pages, models, datasets and spaces.
- **GitHub:** repos and stars.
- **OpenAlex:** optional.

Responses are cached in `~/.cache/citation-snowball`.

**Google Scholar is never queried.** It has no API, its robots.txt disallows automated access, and Google blocks
automated traffic. Citation counts are therefore Semantic Scholar's, which are usually 1.4-1.8x lower than Google
Scholar's.

## Limitations

- Works best for arXiv papers with an HTML version. For other papers the script saves the PDF, and the agent reads
  the tables itself.
- Semantic Scholar has gaps: some papers lack reference lists, and a few records are duplicated. OpenAlex
  undercounts arXiv-heavy fields, so it is only used to find candidates, never for counts.
- Code discovery prefers repos the authors linked themselves. Weaker matches are shown as "possible code
  (unverified)".

## License

[MIT](LICENSE)
