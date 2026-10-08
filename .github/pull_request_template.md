## What and why

<!-- What does this change, and what problem in the reading map or the workflow does it fix? Link the issue if there is one. -->

## How it was checked

<!-- Tests added or updated. For changes to the gates, ranking, relevance or caps: the section counts before and
after on two seeds (one recent with few citers, one well cited), and the papers gained and lost. -->

## Checklist

- [ ] `python3 -m unittest discover -s tests` passes, with new tests for new behavior
- [ ] Standard library only, Python 3.8 compatible (no runtime `X | Y` types, `match`, `str.removeprefix`)
- [ ] CHANGELOG entry under `[Unreleased]`
- [ ] SKILL.md, README and `references/` updated if behavior changed
- [ ] Selection changes: before/after on two seeds (see CONTRIBUTING.md)
