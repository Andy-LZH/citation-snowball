# Data sources

What `fbsearch.py` calls, the limits that matter, and the environment variables it reads.

Contents:

1. Semantic Scholar (primary)
2. arXiv
3. Hugging Face
4. GitHub
5. OpenAlex (optional)
6. Crossref (fallback)
7. Google Scholar: not used, and why
8. Caching and environment variables

## 1. Semantic Scholar (primary)

The Graph API (`https://api.semanticscholar.org/graph/v1`) supplies:

- the seed and its references, with citation contexts and intents;
- citers and citation counts;
- abstracts and TL;DRs;
- BibTeX for the kept papers.

- **Keyless access** shares one rate-limited pool with every other keyless user. At busy times most requests get
  HTTP 429; one check on 2026-10-06 saw 1 of 12 requests succeed. The script retries each request with 1-3 s jitter
  for up to `FBS_S2_PATIENCE` seconds (default 900), so keyless runs work but can take 5-20 minutes.
- **With a key** (`S2_API_KEY`, free from https://www.semanticscholar.org/product/api#api-key-form), requests are
  rate limited to about 1 per second for that key alone. The script paces itself at 1.1 s. An occasional 429 still
  happens with a key; the script backs off and retries it ("HTTP 429 ... retrying in 2s" is harmless).
- **Citers are returned newest first**, and `offset + limit` must stay below 10,000. Widening wants exactly the
  newest citers, and the topic search and OpenAlex cover older influential ones (methodology section 9).
- **Some big papers have no reference list.** SAM 3 (1,162 citations), Qwen2-VL and Qwen2.5-VL (6,110) have
  `referenceCount: 0` (checked 2026-10-08), so they never appear as citers of anything. Section C finds them from the
  citing side, by counting what recent papers in the area cite (methodology section 8).
- Endpoints used:
  - `paper/{id}` (seed)
  - `paper/{id}/references` (contexts, intents, isInfluential): the seed's references, and the reference lists
    of the seeds and compared works with their citation sentences, for section A2's counts and its dataset labels
  - `paper/{id}/citations`
  - `paper/batch`, used three ways:
    - up to 500 ids at a time;
    - with `references.paperId` for the full reference lists used in verification and in the recent area's
      co-citation counts (up to 1,000 papers);
    - with `abstract` to check widening's topic match and to tell datasets from models, and with `citationStyles`
      for the BibTeX of kept papers.
  - `paper/search/bulk`: widening's topic search, with a boolean query, `sort=citationCount:desc`,
    `minCitationCount` and `publicationDateOrYear=<30 months ago>:`.
  - `paper/search/match` (title match)
  - `paper/search` (seed disambiguation)
- Identifier prefixes accepted for seeds: `ARXIV:`, `DOI:`, `CorpusId:`, `ACL:`, `PMID:`, `PMCID:`, `MAG:`, `URL:`,
  or a 40-character paperId. OpenReview URLs are resolved through the OpenReview API to a title.
- Known gaps: occasional duplicate records (same paper twice with different counts), and missing reference lists for
  some papers. The script recovers those from the seed's bibliography.

## 2. arXiv

- `https://arxiv.org/html/{id}` (native HTML, papers since late 2023) or `https://ar5iv.labs.arxiv.org/html/{id}`
  (older papers). This is the seed's full text, with table and section structure, used to infer roles.
- `https://export.arxiv.org/api/query`: metadata, including the comment field that often has the code link, and the
  title search used to recover bibliography entries. arXiv asks for one request per 3 seconds; the script waits 3.1 s.
- The PDF (`arxiv.org/pdf/{id}`, or Semantic Scholar's open-access link) is downloaded when there is no HTML. If
  `pdftotext` is installed it is converted to `fulltext.txt`; otherwise the agent reads `paper.pdf`.

## 3. Hugging Face

- `https://huggingface.co/api/papers/{arxiv_id}`: the paper page. `githubRepo` and `githubStars` give the
  repository, linked by the authors or detected automatically. This is the most reliable code link and replaces
  Papers with Code, which shut down in 2025. It is also the cheap first step of the **stars probe** that runs before
  selection.
- `https://huggingface.co/api/{models|datasets|spaces}?filter=arxiv:{id}&sort=likes`: artifacts tagged with the paper.
  - Any model card can cite a paper: Microsoft's Magma-8B is tagged with Set-of-Mark's arXiv id. So an artifact
    counts only if its id contains the paper's short name or its repo's name, or its tags cite no other arXiv paper.
  - It is verified when its owner also owns the paper's verified GitHub repo, and unverified otherwise.
  - The most-liked artifact of each kind is kept. Likes are shown as ♥.
- No key needed; limits are generous.

## 4. GitHub

- **Stars.** With a token, star counts come from GraphQL, 40 repos per query, together with `createdAt` (for stars
  per month), `pushedAt` and the description. The token is `GH_TOKEN`, then `GITHUB_TOKEN`, then `gh auth token`.
  Without one, REST allows 60 requests per hour: the script queries up to 55 repos, and the stars probe uses
  Hugging Face's `githubStars` only.
- **READMEs** for the docs score come from `repos/{repo}/readme` (raw) with a token, and from
  `raw.githubusercontent.com/{repo}/HEAD/README.md` (no quota) without one. The score gives one point each for setup,
  usage, training, evaluation and released weights. A check counts when a README heading matches, or the text matches
  twice. It was calibrated on 72 repos: most maintained ML repos score 4-5, and a stub README scores 0-1.
- Repo-link priority:
  1. a repo the authors linked on the paper's Hugging Face page;
  2. links in the arXiv comment or abstract (an abstract can also link a dependency, hence the lower rank);
  3. a repo Hugging Face auto-detected, kept only if its name or description matches the title. Auto-detection
     sometimes picks a repo that merely cites the paper.
- Fallback for papers with no linked repo (at most 40 searches per run, token required):
  1. a repo whose name contains the paper's short name (e.g. "LoRA");
  2. a repo whose README contains the exact title and whose name or description matches it. A description that
     names the paper's short name also counts: MiniGPT-v2 lives in Vision-CAIR/MiniGPT-4, "Open-sourced codes for
     MiniGPT-4 and MiniGPT-v2". Awesome lists, paper lists ("papers", not "the paper"), surveys and course notes are
     skipped.

  This finds repos that neither Hugging Face nor arXiv link, for example:
  - facebookresearch/segment-anything;
  - KaiyangZhou/CoOp;
  - UX-Decoder/Segment-Everything-Everywhere-All-At-Once.
- **Verified vs unverified.** Repos from the paper or its Hugging Face page count as verified, and so do strong
  search matches. So do Hugging Face artifacts owned by the same account as the verified repo. Weak search matches,
  and Hugging Face artifacts not owned by the repo's owner (often community ports), are **unverified**. They appear as
  "maybe ... (unverified)" in the code column when nothing verified exists, and never count toward the gate's stars
  route.

## 5. OpenAlex (optional)

Used for one job: the most-cited citers of a paper whose citers were only partly fetched
(`filter=cites:W...,cited_by_count:>N&sort=cited_by_count:desc`, 100 per call). There are two cases:

- route B: a seed with more citers than `--seed-cap`;
- widening: a compared work with more citers than `--cap`. Here the filter adds `from_publication_date:<30 months ago>`,
  so the hits are recent.

- Pricing in 2026: singleton GETs are free, list/filter calls cost 1 credit, searches 10. Keyless callers get 1,000
  credits per day, shared per IP address; a free key (`OPENALEX_API_KEY`) gives 10,000. The script spends at most
  `--openalex-budget` (40) billable calls per run and sends the key as a header.
- Caveats: OpenAlex badly undercounts arXiv-heavy fields (HQ-SAM had 109 citations there vs 708 on Semantic Scholar),
  and some records are mis-merged (SAM's arXiv record carried an unrelated title). Every hit is therefore re-resolved
  in Semantic Scholar and kept only if the titles match. OpenAlex counts and titles are never reported.

## 6. Crossref (fallback)

When neither Semantic Scholar nor the bibliography yields references and the seed has a publisher DOI, the script
reads Crossref's deposited reference DOIs and resolves them in Semantic Scholar. Crossref has no arXiv records.

## 7. Google Scholar: not used, and why

Google Scholar has no API. Its robots.txt disallows `/scholar` (searches, "Cited by" lists, versions) for all
automated clients, and Google's Terms of Service forbid automated access that violates robots.txt or bypasses its
protective measures. Automated traffic can also get the user's whole network CAPTCHA-blocked. So:

- Never query it from the agent: no curl/fetch tools, no `scholarly` package, no SerpApi or proxy services, no
  browser automation on its search pages.
- Instead, each report row has a **GS** link (`allintitle:"<title>"`) for the reader to click.
- Google Scholar counts are usually 1.4-1.8x Semantic Scholar's (spot checks on 2026-10-06: LoRA 41.6k vs 23.9k,
  BERT 185k vs 122k, ResNet 344k vs 241k). Thresholds always apply to Semantic Scholar counts.

## 8. Caching and environment variables

| Variable | Effect |
|---|---|
| `S2_API_KEY` (or `SEMANTIC_SCHOLAR_API_KEY`) | Semantic Scholar key: fast, reliable runs. |
| `GH_TOKEN` / `GITHUB_TOKEN` | GitHub token for star counts (otherwise `gh auth token` is tried). |
| `OPENALEX_API_KEY` | OpenAlex key (10x daily budget). |
| `FBS_CACHE_DIR` | HTTP cache location (default `~/.cache/citation-snowball`; falls back to the system temp dir if not writable). |
| `FBS_S2_PATIENCE` | Seconds to keep retrying one keyless Semantic Scholar request (default 900). |

Cached responses live 7-60 days depending on the endpoint; `--refresh` ignores the cache. Delete the cache folder to
reclaim space.
