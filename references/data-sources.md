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

The Graph API (`https://api.semanticscholar.org/graph/v1`) supplies the seed, its references (with citation contexts
and intents), citers, citation counts, abstracts and TL;DRs.

- **Keyless access** shares one rate-limited pool with every other keyless user. At busy times most requests get
  HTTP 429; one check on 2026-10-06 saw 1 of 12 requests succeed. The script retries each request with 1-3 s jitter
  for up to `FBS_S2_PATIENCE` seconds (default 900), so keyless runs work but can take 5-20 minutes.
- **With a key** (`S2_API_KEY`, free from https://www.semanticscholar.org/product/api#api-key-form), requests are
  rate limited to about 1 per second for that key alone. The script paces itself at 1.1 s.
- **Citers are returned newest first**, and `offset + limit` must stay below 10,000. See methodology section 4 for how
  the script compensates.
- Endpoints used:
  - `paper/{id}` (seed)
  - `paper/{id}/references` (contexts, intents, isInfluential)
  - `paper/{id}/citations`
  - `paper/batch` (up to 500 ids; also used with `references.paperId` to fetch full reference lists for
    verification)
  - `paper/search/bulk` (boolean query, `sort=citationCount:desc`, `minCitationCount`)
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
  Papers with Code, which shut down in 2025.
- `https://huggingface.co/api/{models|datasets|spaces}?filter=arxiv:{id}&sort=likes`: artifacts tagged with the
  paper. The most-liked artifact of each kind is kept if its owner matches the paper's GitHub owner (verified) or its
  name contains the paper's short name (unverified). Likes are shown as ♥.
- No key needed; limits are generous.

## 4. GitHub

- Star counts come from GraphQL (40 repos per query) when a token is available: `GH_TOKEN`, then `GITHUB_TOKEN`,
  then `gh auth token`. Without a token, REST allows 60 requests per hour: the script queries up to 55 repos and
  otherwise uses Hugging Face's `githubStars`.
- Repo-link priority:
  1. a repo the authors linked on the paper's Hugging Face page;
  2. links in the arXiv comment or abstract (an abstract can also link a dependency, hence the lower rank);
  3. a repo Hugging Face auto-detected, kept only if its name or description matches the title. Auto-detection
     sometimes picks a repo that merely cites the paper.
- Fallback for papers with no linked repo (at most 40 searches per run, token required):
  1. a repo whose name contains the paper's short name (e.g. "LoRA");
  2. a repo whose README contains the exact title and whose name or description matches it. Awesome lists, paper
     lists, surveys and course notes are skipped.
  This finds e.g. facebookresearch/segment-anything and KaiyangZhou/CoOp, which neither Hugging Face nor arXiv link.
- **Verified vs unverified.** Repos from the paper or its Hugging Face page count as verified, and so do strong
  search matches. So do Hugging Face artifacts owned by the same account as the verified repo. Weak search matches,
  and Hugging Face artifacts matched only by name (often community ports), are **unverified**. They appear in a
  "Possible code (unverified)" column and do not put a paper in the "with code" section.

## 5. OpenAlex (optional)

Used for one job: the most-cited citers of a core paper whose citers were only partly fetched
(`filter=cites:W...,cited_by_count:>N&sort=cited_by_count:desc`, 100 per call).

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
