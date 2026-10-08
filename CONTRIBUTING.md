# Contributing to citation-snowball

Thanks for helping. This is a small, dependency-free tool that AI agents run for researchers, so the bar for a
change is simple: does it make the reading map more useful to someone deciding what to read and cite?

## Ways to contribute

- **Report a bad map.** This is the most valuable contribution. Use the
  [bad result](.github/ISSUE_TEMPLATE/bad_result.md) issue template and include:
  - the seed and the commands that ran;
  - what was wrong: a paper the map should have found, an off-topic paper it kept, or a wrong code link.

  If you can, look the paper up in the run's `pool.json`:
  - absent means it was never screened;
  - present but not kept means it failed a gate or the topic match.
- **Fix a bug.** Areas include role inference from the seed's tables, code discovery, and the gates.
- **Add a data source or improve an existing one.** Keep to free, documented APIs, and follow their rate limits.
- **Improve the agent instructions in [SKILL.md](SKILL.md).** Agents follow them literally, so small wording changes
  matter.
- **Improve the docs:** this file, the README, [`references/`](references).

## Ground rules

- **Standard library only, Python 3.8+.** No third-party packages in the script or the tests: the skill must run
  wherever an agent has a Python interpreter.
- **One script.** `scripts/fbsearch.py` stays a single file that imports without side effects. All network access
  goes through `Http`, which caches, throttles per host and retries.
- **Never query Google Scholar** in any way. See [data-sources.md](references/data-sources.md#7-google-scholar-not-used-and-why).
- **Be polite to the APIs.** Keep the per-host intervals in `Http.INTERVAL`, and don't add parallel requests.
- **Match the existing style:**
  - `%` formatting;
  - docstrings and comments that say *why*, with a real example where one helps (see `GATES` or `hf_artifacts`);
  - lines up to 120 characters;
  - names that read like the surrounding code.
- **Every behavior change comes with an offline test.**

## Development setup

```sh
git clone https://github.com/<you>/citation-snowball
cd citation-snowball
python3 scripts/fbsearch.py doctor          # APIs, keys and tools
python3 -m unittest discover -s tests -v    # offline, under a second
```

To use your working copy from an agent, symlink it into the agent's skills folder (see the README's Install
section), or run the script directly as below.

Set `S2_API_KEY` (a free [Semantic Scholar key](https://www.semanticscholar.org/product/api#api-key-form)) and log in
with `gh auth login`. Without them, real runs take many minutes and the stars route is less reliable.

## Tests

- `python3 -m unittest discover -s tests` runs everything offline. CI runs it on Python 3.8 and 3.13.
- `tests/helpers.py` builds the fixtures:

  | Helper | Builds |
  |---|---|
  | `seed()` | a `seeds.json` entry |
  | `core_row()` | a `core.json` row |
  | `rec()` | a `pool.json` record, optionally with stars and a README docs score |
  | `pool()` | a pool |
  | `cfg()` | a selection config built the same way the CLI builds it |

- Functions that touch the network take an `http` object. Tests pass a stand-in that returns canned responses; see
  `FakeHttp` and `SearchHttp` in `tests/test_backward.py`.
- `tests/test_version.py` fails when the version numbers drift apart or the SKILL.md description states stale bars.

| File | Covers |
|---|---|
| `test_gates.py` | the influence gates, `--gate` overrides, the SOTA score |
| `test_select.py` | sections A1 and B, widening's trigger and topic rule, exclusion backfill, caps, survey cap, multi-seed |
| `test_cocitation.py` | sections A2 and C: co-citation paths, specificity, dataset labels, twins, the latest-model window, the same-area check |
| `test_report.py` | the rendered reading map, read-first picks, the BibTeX file |
| `test_backward.py` | seed parsing, multi-seed core merging, README docs score, code discovery |
| `test_version.py` | version consistency |

## Trying a change on real papers

Run the pipeline into a scratch folder. HTTP responses are cached in `~/.cache/citation-snowball`, so repeated runs
take seconds.

```sh
F=scripts/fbsearch.py
python3 $F backward 2505.18291 --workdir /tmp/ip
python3 $F core --workdir /tmp/ip                     # review; edit with --include / --exclude / --role
python3 $F forward --workdir /tmp/ip --query '"part segmentation" | "reasoning segmentation"' \
                   --keywords 'part segmentation,reasoning segmentation,part grounding,affordance'
python3 $F report --workdir /tmp/ip --out /tmp/ip.md
```

- **Selection changes** don't need a new search: `select` re-cuts the saved `pool.json`, e.g.
  `python3 $F select --workdir /tmp/ip --gate expand.mid=30,200`. That makes before/after comparisons fast.
- **Fresh data:** `--refresh` ignores the cache, and `FBS_CACHE_DIR=/tmp/cache` uses a separate one.

## Proposing a change to the gates or ranking

The defaults decide what every user reads. A pull request that changes any of these needs evidence:

- `GATES`, `COCITE` or `SELECT_DEFAULTS`;
- `sota_score`;
- the relevance rules in `select_papers`, `foundations` or `latest_models`.

Include:

1. **Two seeds of different kinds:**
   - a recent seed with few citers, which exercises widening and sections A2 and C;
   - a well-cited seed, which exercises section B's cap.

   The 1.3.0 calibration used InstructPart (`2505.18291`) and InstructPart + SPIN (`2407.09686`).
2. **For each seed:** the section counts before and after, and the papers gained and lost, with a sentence on why
   the new list is better. The CHANGELOG entry for 1.3.0 shows the expected level of detail.
3. **Updated docs:** the tests, the gate tables in SKILL.md, README and CHANGELOG, and the methodology reference.

## Changing SKILL.md

- The `description` decides when agents load the skill. Keep it at most 1024 characters and make it state the
  current bars; a test checks both.
- Keep instructions imperative and short.
- Check the result with an agent on the prompts in [`evals/evals.json`](evals/evals.json).

## Versioning

citation-snowball uses [Semantic Versioning](https://semver.org), adapted for a young tool that keeps moving. Feature
releases are minor versions, even when they change the report or the workdir, as long as nobody's commands break
without warning.

| Bump | When |
|---|---|
| **Minor** (1.x.0) | Feature releases that move the tool forward: new sections, data sources, flags or defaults. Report layout and workdir format changes count too, provided the changelog has a **Migration** note and renamed flags keep working, with a deprecation notice. 1.3.0 was one. |
| **Patch** (1.x.y) | Bug fixes and documentation that leave a typical seed's selection unchanged. |
| **Major** (2.0.0) | Removing the deprecated flags, adding a required dependency, or changing the agent workflow in SKILL.md so that old instructions no longer work. |

The version lives in three places, and the tests check that they match:

- `VERSION` in `scripts/fbsearch.py`, which is also sent in the User-Agent and printed in each report's footnote;
- `metadata.version` in `SKILL.md`;
- the top `## [x.y.z] - YYYY-MM-DD` entry in `CHANGELOG.md`.

Deprecated flags keep working, with a notice, until the next major version.

## Release checklist (maintainers)

1. Move the `[Unreleased]` entries into a new `## [x.y.z] - YYYY-MM-DD` section.
2. Bump `VERSION` and `metadata.version`.
3. Run the tests on Python 3.8 and a current Python, and do one real run.
4. Merge to `main`.
5. Tag `vX.Y.Z` and push the tag.
6. Create a GitHub release whose notes are the CHANGELOG section.

## Pull request checklist

The [pull request template](.github/pull_request_template.md) repeats this list:

- [ ] `python3 -m unittest discover -s tests` passes, with new tests for new behavior.
- [ ] Standard library only, and Python 3.8 compatible: no runtime `X | Y` types, no `match`, no
      `str.removeprefix`.
- [ ] A CHANGELOG entry under `[Unreleased]`.
- [ ] SKILL.md, README and `references/` updated if behavior changed.
- [ ] Selection changes: before/after on two seeds.

## Conduct

Be kind and assume good faith. This project follows the
[Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/); report problems to the
maintainer through a GitHub issue or the email on their profile.
