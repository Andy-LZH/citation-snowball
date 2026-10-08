# citation-snowball

[![CI](https://github.com/Andy-LZH/citation-snowball/actions/workflows/ci.yml/badge.svg)](https://github.com/Andy-LZH/citation-snowball/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/Andy-LZH/citation-snowball)](https://github.com/Andy-LZH/citation-snowball/releases)
![python](https://img.shields.io/badge/python-3.8%2B%20·%20stdlib%20only-informational)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An [Agent Skill](https://agentskills.io) for Claude Code, Codex and GitHub Copilot. It turns a paper you like into a
short reading map, with code and BibTeX.

## Prerequisites

**Required:**

- An agent: Claude Code, Codex, GitHub Copilot (Copilot CLI needs version 1.0.11 or later) or claude.ai.
- Python 3.8 or later. The skill uses only the standard library, so there is nothing to `pip install`.
- Internet access.

**Recommended: a free Semantic Scholar API key.** The skill works without one, but requests then share a public pool
that is often busy, so a run takes 5-20 minutes instead of one or two.

1. Request a key with [Semantic Scholar's API key form](https://www.semanticscholar.org/product/api#api-key-form).
2. Add it to your shell profile, then restart your agent:

   ```sh
   echo 'export S2_API_KEY=your-key' >> ~/.zshrc    # bash: ~/.bashrc
   ```

**Optional: a GitHub login,** for accurate stars and README checks. Run `gh auth login` with the
[GitHub CLI](https://cli.github.com), or set `GH_TOKEN`. Without one, stars come from Hugging Face's cached counts.

To check your setup, ask your agent to run the skill's `doctor` command.

## Install

**Claude Code** (recommended):

```
/plugin marketplace add Andy-LZH/citation-snowball
/plugin install citation-snowball@citation-snowball
```

- On Claude Code 2.1.275 or later, one line does both:
  `/plugin install citation-snowball --marketplace Andy-LZH/citation-snowball`.
- For automatic updates, open `/plugin`, go to **Marketplaces**, choose `citation-snowball`, then
  **Enable auto-update**.

**Codex, GitHub Copilot, or Claude Code without the plugin:**

```sh
git clone https://github.com/Andy-LZH/citation-snowball ~/.agents/skills/citation-snowball
```

- Codex and Copilot read `~/.agents/skills`.
- Claude Code reads `~/.claude/skills`: clone there instead, or symlink the folder above. Don't combine this with the
  plugin, or the skill loads twice.
- Codex sandboxes network access; approve it when the skill runs.

**claude.ai:** zip the folder and upload it under Customize → Skills. It needs code execution with internet access.

## Use

Ask your agent in plain language. A paper can be an arXiv link or ID, a DOI, a title, or an OpenReview or Semantic
Scholar URL.

- *"What should I read and cite after InstructPart (arXiv 2505.18291)?"*
- *"Here are two papers I like: 2505.18291 and 2407.09686. Give me one reading list."*
- *"Only papers with at least 100 citations."* The agent passes `--min-citations 100`.
- *"Always include the recent state of the art."* The agent passes `--expand always`.

To call it by name: `/citation-snowball <paper>` in Claude Code (`/citation-snowball:citation-snowball <paper>` with
the plugin), or mention `$citation-snowball` in Codex.

The map is saved as `<name>-reading-map.md`, with `<name>-reading-map.bib` next to it, in the current directory.

## What you get

Five short sections. Each paper gets one line on why to read it, plus its code and stars when it has a repo. From the
[full example for InstructPart](examples/instructpart.md):

| Section | What it holds | In the example |
|---|---|---|
| **Read these first** | 5-8 picks from the sections below | LISA, ADE20K, PACO, SAM 3, … |
| **A1. What it compares against** | the baselines and datasets in its result tables | LISA, Shikra, PACO, PartImageNet |
| **A2. What the field builds on** | datasets and models it and the compared works commonly cite | COCO, RefCOCO, ADE20K, CLIP, SAM, LLaVA |
| **B. Influential follow-ups** | influential papers citing it, or citing what it compares against when it is new | SAM3-I, Seg-Zero, Sa2VA, UniPixel |
| **C. Latest models to keep an eye on** | what the area's newest papers build on | SAM 2, SAM 3, Qwen2.5-VL, Qwen3-VL |

Two entries from its "Read these first":

> 2. [Scene Parsing through ADE20K Dataset](https://doi.org/10.1109/CVPR.2017.544) · 2017 · 4.1k citations · [CSAILVision/semantic-segmentation-pytorch](https://github.com/CSAILVision/semantic-segmentation-pytorch) ★5.1k  
>    ↩ Foundation: cited by 6 of 15 (seed + compared works) · 14% of recent area papers. Scene parsing dataset with object and part annotations over 150 categories; the standard semantic-segmentation benchmark the compared open-vocabulary models report on, at object rather than task level.
>
> …
>
> 7. [**SAM 3**: Segment Anything with Concepts](https://arxiv.org/abs/2511.16719) · 2025 · 1.2k citations · [facebookresearch/sam3](https://github.com/facebookresearch/sam3) ★12k  
>    ↪ Latest model: cited by 24% of the area's papers since it appeared (23 of 94). Segments every instance of a concept given a short noun phrase or image exemplar; the model SAM3-I builds on, and the strongest new prompt-to-mask baseline to test on part instructions.

## How it works

A helper script does the API work, and your agent supplies the judgment.

- **The script** reads the paper's tables, counts what the paper and the works it compares against cite in common
  (co-citation), screens the papers that cite it, and finds code on GitHub and Hugging Face.
- **The agent** confirms the comparison set, chooses the topic keywords and writes the one-line reasons.

Data comes from Semantic Scholar, arXiv, Hugging Face, GitHub and OpenAlex. Google Scholar is never queried, so
citation counts are Semantic Scholar's.

More detail:

- [`SKILL.md`](SKILL.md): the workflow the agent follows.
- [`references/methodology.md`](references/methodology.md): every bar and how it was set, and the
  [known limitations](references/methodology.md#14-limitations).
- [`references/data-sources.md`](references/data-sources.md): APIs, keys and environment variables.
- [`references/report-format.md`](references/report-format.md): the files it writes.

## Updates

- **Plugin installs** update through `/plugin`, or by themselves once auto-update is on.
- **Folder installs** check for a new release once a day and say when there is one. Ask your agent to update, or run
  `python3 ~/.agents/skills/citation-snowball/scripts/fbsearch.py update`. Set `FBS_NO_UPDATE_CHECK=1` to turn the
  check off.

What changed in each version: [CHANGELOG.md](CHANGELOG.md).

## Contributing

Bug reports and pull requests are welcome, especially a paper the map should have found, with the seed you used.
[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, the offline tests and how releases work.

## License

[MIT](LICENSE)
