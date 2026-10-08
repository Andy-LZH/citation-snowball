"""render_report / write_bib: the reading map the user actually reads."""
import os
import re
import tempfile
import unittest

from helpers import fbsearch, pid, seed


def paper(section, title, cites=100, year=2025, stars=None, docs=None, key=None, **extra):
    p = pid()
    out = {"key": key or p[:8], "paperId": p, "title": title, "year": year, "citationCount": cites,
           "link": "https://arxiv.org/abs/2501.00001", "section": section, "route": extra.pop("route", None),
           "role": extra.pop("role", None), "seeds": extra.pop("seeds", []), "gate": extra.pop("gate", None),
           "score": extra.pop("score", 1.0), "builds_on": extra.pop("builds_on", []), "authors": ["Ada Lovelace"],
           "bibtex": extra.pop("bibtex", None), "externalIds": {"ArXiv": "2501.00001"},
           "code": {"github": [], "hf": []}}
    if stars is not None:
        out["code"]["github"].append({"repo": "lab/" + title.split(":")[0].replace(" ", ""), "url": "https://github.com/lab/x",
                                      "stars": stars, "source": "hf-paper", "docs": docs})
    out.update(extra)
    return out


def candidates(papers, expand_shown=True):
    gates = {k: list(v) for k, v in fbsearch.GATES.items()}
    selection = dict(fbsearch.SELECT_DEFAULTS, gates=gates)
    meta = {"pool": 5692, "seed_citers": 19, "verified": 372, "generated": "2026-10-08", "selected": "2026-10-08",
            "selection": selection, "expand_shown": expand_shown, "expand_why": "only 2 influential papers cite the seed",
            "query": '"part segmentation"', "since": "2024-10-08", "version": fbsearch.VERSION,
            "keywords": ["part segmentation", "reasoning segmentation"],
            "cocitation": {"backward_set": 15, "backward_have": 15, "area_size": 263, "coupling_set": 30}}
    return {"meta": meta, "papers": papers}


def world():
    a1 = paper("compared", "LISA: Reasoning Segmentation via Large Language Model", cites=1068, year=2023, stars=2684,
               docs=5, role="benchmarked_model", score=None)
    a2 = paper("compared", "PACO: Parts and Attributes of Common Objects", cites=195, year=2023, stars=302, docs=5,
               role="compared_dataset", score=None)
    a3 = paper("compared", "Shikra: Unleashing Multimodal LLM's Referential Dialogue Magic", cites=1043, year=2023,
               role="benchmarked_model", score=None)
    rel = paper("related", "Going Denser with Open-Vocabulary Part Segmentation", cites=99, year=2023, role="related")
    f1 = paper("foundation", "Scene Parsing through ADE20K Dataset", cites=4140, year=2017, kind="data", k_b=6, n_b=15,
               share_f=0.14, score=5.9, twins=["Semantic Understanding of Scenes Through the ADE20K Dataset"])
    f2 = paper("foundation", "Microsoft COCO: Common Objects in Context", cites=56061, year=2014, kind="data", k_b=11,
               n_b=15, share_f=0.21, score=7.8)
    f3 = paper("foundation", "Learning Transferable Visual Models From Natural Language Supervision", cites=57185,
               year=2021, kind="model", k_b=7, n_b=15, share_f=0.49, score=9.6, stars=34000, docs=5)
    b1 = paper("follow_up", "SAM3-I: Segment Anything with Instructions", cites=12, stars=185, docs=5, route="seed",
               gate="12 citations in 10 months", score=1.9)
    b2 = paper("follow_up", "Affordance-R1: Reinforcement Learning for Affordance Reasoning", cites=26, stars=80, docs=5,
               route="seed", gate="26 citations in 14 months", score=1.4)
    b3 = paper("follow_up", "Seg-Zero: Reasoning-Chain Guided Segmentation", cites=241, stars=641, docs=5, route="expand",
               gate="241 citations in 19 months", builds_on=["LISA"], score=2.6, share_f=0.29)
    c1 = paper("latest", "SAM 3: Segment Anything with Concepts", cites=1162, stars=12000, docs=5, share_f=0.24,
               support_f=23, eligible=94, score=0.24)
    c2 = paper("latest", "Qwen2.5-VL Technical Report", cites=6110, share_f=0.23, support_f=43, eligible=188, score=0.23)
    also = paper("also", "Another Qualified Paper", cites=30, route="expand", gate="30 citations in 8 months",
                 builds_on=["PACO"], score=0.9)
    return [a1, a2, a3, rel, f1, f2, f3, b1, b2, b3, c1, c2, also]


def fbsearch_diffs(raw):
    """diffs.json content -> what load_diffs returns."""
    with tempfile.TemporaryDirectory() as wd:
        fbsearch.save_json(os.path.join(wd, "diffs.json"), raw)
        return fbsearch.load_diffs(wd)


class Render(unittest.TestCase):
    def setUp(self):
        self.papers = world()
        self.seeds = [seed(title="InstructPart: Task-Oriented Part Segmentation with Instruction Reasoning",
                           ptype="benchmark")]

    def render(self, diffs=None, **kw):
        lines, kept, bib, todo = fbsearch.render_report(candidates(self.papers, **kw), self.seeds, diffs or {})
        return "\n".join(lines), kept, bib, todo

    def by_title(self, start):
        return next(p for p in self.papers if p["title"].startswith(start))

    def test_title_uses_the_short_name_and_venue_is_abbreviated(self):
        self.seeds[0]["venue"] = "Annual Meeting of the Association for Computational Linguistics"
        text, _, _, _ = self.render()
        self.assertTrue(text.startswith("# Reading map: InstructPart\n"))
        self.assertIn(" · ACL 2025 · ", text)

    def test_structure(self):
        text, kept, _, todo = self.render()
        for heading in ("## Read these first", "## A. Foundations", "### A1. What the seed compares against",
                        "### A2. What the field builds on", "## B. Influential follow-ups",
                        "## C. Latest models to keep an eye on"):
            self.assertIn(heading, text)
        self.assertEqual(len(kept), 11)  # A1 3 + A2 3 + B 3 + C 2; related and also are not counted
        self.assertEqual(todo, 11)
        self.assertIn("**Closest related work**", text)
        self.assertIn("Also qualified, beyond the size caps (1, not reviewed)", text)
        self.assertIn("**11 papers to read**", text)

    def test_glance_table_links_to_every_section(self):
        text, _, _, _ = self.render()
        for anchor in ("(#a1-what-the-seed-compares-against)", "(#a2-what-the-field-builds-on)",
                       "(#b-influential-follow-ups)", "(#c-latest-models-to-keep-an-eye-on)"):
            self.assertIn(anchor, text)

    def test_foundations_split_datasets_from_models_and_say_who_cites_them(self):
        text, _, _, _ = self.render()
        a2 = text.split("### A2.", 1)[1].split("## B.", 1)[0]
        self.assertLess(a2.index("**Datasets and benchmarks (2)**"), a2.index("**Models and methods (1)**"))
        self.assertIn("| 6 of 15<br>14% of recent work |", a2)
        self.assertIn("also published as Semantic Understanding of Scenes Through the ADE20K Dataset", a2)

    def test_follow_ups_say_why_they_are_there(self):
        text, _, _, _ = self.render()
        b = text.split("## B.", 1)[1].split("## C.", 1)[0]
        self.assertIn("cites the seed<br>_12 citations in 10 months_", b)
        self.assertIn("builds on LISA<br>_241 citations in 19 months_<br>used by 29% of recent work", b)
        self.assertIn("Because only 2 influential papers cite the seed, this section also has 1 recent papers", b)

    def test_latest_models_say_how_much_of_the_area_uses_them(self):
        text, _, _, _ = self.render()
        c = text.split("## C.", 1)[1].split("\n---\n", 1)[0]
        self.assertIn("| 24% of later area papers<br>_23 of 94_ |", c)
        self.assertIn("no reference list", c)

    def test_empty_sections_say_why(self):
        self.papers = [p for p in self.papers if p["section"] not in ("latest", "foundation")]
        text, _, _, _ = self.render()
        self.assertIn("_No foundations:", text)
        self.assertIn("_None: too few recent papers in the area to tell", text)

    def test_compared_papers_are_grouped_by_role(self):
        text, _, _, _ = self.render()
        self.assertIn("**Benchmarked models (2)**", text)
        self.assertIn("**Compared datasets (1)**", text)

    def test_auto_picks_take_two_of_each_kind(self):
        text, _, _, _ = self.render()
        block = text.split("## Read these first", 1)[1].split("## A.", 1)[0]
        items = re.findall(r"^\d+\. ", block, re.M)
        self.assertEqual(len(items), 8)
        order = [block.index(t) for t in ("Scene Parsing", "LISA", "Seg-Zero", "SAM 3")]
        self.assertEqual(order, sorted(order))  # foundations, compared, follow-ups, latest
        self.assertIn("↩ Foundation: cited by 6 of 15 (seed + compared works) · 14% of recent area papers.", block)
        self.assertIn("↩ Compared in the seed (benchmarked model).", block)
        self.assertIn("↪ Builds on LISA · 241 citations in 19 months.", block)
        self.assertIn("↪ Latest model: cited by 24% of the area's papers since it appeared (23 of 94).", block)

    def test_agent_picks_replace_auto_picks_in_their_order(self):
        segzero, paco = self.by_title("Seg-Zero"), self.by_title("PACO")
        diffs = {segzero["key"]: {"diff": "Best new method.", "pick": 1}, paco["key"]: {"diff": "The part dataset.", "pick": 2}}
        text, _, _, _ = self.render(fbsearch_diffs(diffs))
        block = text.split("## Read these first", 1)[1].split("## A.", 1)[0]
        self.assertEqual(len(re.findall(r"^\d+\. ", block, re.M)), 2)
        self.assertLess(block.index("Seg-Zero"), block.index("PACO"))
        self.assertIn("Best new method.", block)

    def test_diffs_fill_the_why_column_and_excludes_leave_the_tables(self):
        lisa, shikra = self.by_title("LISA"), self.by_title("Shikra")
        diffs = {lisa["key"]: "Defines reasoning segmentation; the seed extends it to parts.",
                 shikra["key"]: "EXCLUDE: box-only model, not a segmenter"}
        text, kept, _, todo = self.render(fbsearch_diffs(diffs))
        self.assertIn("Defines reasoning segmentation", text)
        self.assertIn("Excluded after review (1)", text)
        self.assertIn("box-only model, not a segmenter", text)
        self.assertEqual(len(kept), 10)
        self.assertEqual(todo, 9)

    def test_code_cells_show_stars_and_docs(self):
        text, _, _, _ = self.render()
        self.assertIn("★2.7k · docs 5/5", text)
        self.assertIn("| — |", text)  # Shikra: no code found

    def test_counts_link_to_google_scholar_and_titles_bold_the_short_name(self):
        text, _, _, _ = self.render()
        self.assertIn("[**LISA**: Reasoning Segmentation via Large Language Model](", text)
        self.assertIn("](https://scholar.google.com/scholar?q=", text)

    def test_footnote_states_every_bar(self):
        text, _, _, _ = self.render()
        foot = text.split("**How this map was made.**", 1)[1]
        self.assertIn("≥50 citations or ★≥1000 at any age", foot)
        self.assertIn("≥20 citations or ★≥150 within 12 months", foot)
        self.assertIn("≥40 citations or ★≥250 within 24 months", foot)
        self.assertIn("older papers are left out", foot)
        self.assertIn("cited by at least 3 of the 15 reference lists", foot)
        self.assertIn("one of the 30 strongest co-citations", foot)
        self.assertIn("cited by at least 15% (and 4)", foot)
        self.assertIn("263 had reference lists", foot)


class MultiSeedRender(unittest.TestCase):
    def test_two_seeds(self):
        papers = world()
        s1 = seed(1, title="InstructPart: Task-Oriented Part Segmentation")
        s2 = seed(2, title="SPIN: Hierarchical Segmentation with Subpart Granularity")
        papers[0]["seeds"] = [s1["key"], s2["key"]]
        papers[7]["seeds"] = [s2["key"]]  # SAM3-I cites SPIN
        lines, _, _, _ = fbsearch.render_report(candidates(papers), [s1, s2], {})
        text = "\n".join(lines)
        self.assertTrue(text.startswith("# Reading map: InstructPart + SPIN\n"))
        self.assertEqual(text.count("**Seed:**"), 2)
        self.assertIn("### A1. What the seeds compare against", text)
        self.assertIn("| Compared by | Why read it |", text)
        self.assertIn("| InstructPart, SPIN |", text)
        self.assertIn("cites SPIN<br>_12 citations in 10 months_", text)


class Bib(unittest.TestCase):
    def test_semantic_scholar_bibtex_gets_a_url_and_unique_keys(self):
        bib = "@Article{Lai2023LISARS,\n author = {Xin Lai},\n title = {LISA},\n year = {2023}\n}"
        a = paper("compared", "LISA", bibtex=bib)
        b = paper("follow_up", "LISA again", bibtex=bib)
        with tempfile.TemporaryDirectory() as wd:
            path = os.path.join(wd, "r.bib")
            self.assertEqual(fbsearch.write_bib(path, [a, b]), 2)
            text = open(path, encoding="utf-8").read()
        self.assertIn("@Article{Lai2023LISARS,", text)
        self.assertIn("@Article{Lai2023LISARSa,", text)
        self.assertIn(" year = {2023},\n url = {https://arxiv.org/abs/2501.00001}\n}", text)

    def test_fallback_entry_from_metadata(self):
        bib = fbsearch.bibtex_for(paper("follow_up", "Seg-Zero: Reasoning-Chain Guided Segmentation", year=2025))
        self.assertTrue(bib.startswith("@article{lovelace2025segzero,") or bib.startswith("@article{lovelace2025seg,"))
        self.assertIn("title = {Seg-Zero: Reasoning-Chain Guided Segmentation}", bib)
        self.assertIn("arXiv preprint arXiv:2501.00001", bib)


if __name__ == "__main__":
    unittest.main()
