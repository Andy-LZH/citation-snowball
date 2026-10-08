---
name: Bad result
about: The reading map missed a paper, kept an off-topic one, or linked the wrong code
labels: result-quality
---

**Seed(s):** <!-- arXiv ids, DOIs or titles -->

**Commands that ran** (or the agent's summary):

```
fbsearch.py backward ...
fbsearch.py forward --workdir ... --query '...' --keywords '...'
```

**What was wrong**

- [ ] A paper is missing: <!-- title / arXiv id, and which section it belongs in -->
- [ ] An off-topic paper was kept: <!-- title, section -->
- [ ] Wrong or missing code link: <!-- paper, expected repo -->
- [ ] Other:

**What pool.json says** (optional, very helpful): search the workdir's `pool.json` for the paper's title.

- Absent: it was never screened.
- Present but not kept: paste its record (`citationCount`, `publicationDate`, `cited`, `sources`, `code`).

**Version:** <!-- python3 scripts/fbsearch.py --version -->
