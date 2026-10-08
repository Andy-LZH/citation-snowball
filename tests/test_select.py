"""select_papers: how the screened pool becomes sections A (compared), B (cites the seed) and C (expansion)."""
import unittest

from helpers import TODAY, cfg, core_row, fbsearch, pool, rec, seed

select = fbsearch.select_papers


def seed_rows(out):
    """Section B rows that cite a seed."""
    return [r for r in out["follow_ups"] if r["route"] == "seed"]


def expand_rows(out):
    """Section B rows added by widening (they cite a compared work)."""
    return [r for r in out["follow_ups"] if r["route"] == "expand"]


def world():
    s = seed()
    lisa = core_row("LISA: Reasoning Segmentation via Large Language Model", "benchmarked_model", cites=1068)
    paco = core_row("PACO: Parts and Attributes of Common Objects", "compared_dataset", cites=195)
    tiny = core_row("TinyBench: a small benchmark", "compared_benchmark", cites=3)
    sam = core_row("Segment Anything", "benchmarked_model", cites=16057)  # a hub: counts half, citers not searched
    rel = core_row("Going Denser with Open-Vocabulary Part Segmentation", "related", cites=99)
    background = core_row("Adam: A Method for Stochastic Optimization", "background", cites=90000, include=False)
    return s, [lisa, paco, tiny, sam, rel, background]


class Sections(unittest.TestCase):
    def setUp(self):
        self.s, self.core = world()
        self.lisa, self.paco, self.tiny, self.sam, self.rel, self.bg = self.core
        S, L, P, H = self.s["paperId"], self.lisa["paperId"], self.paco["paperId"], self.sam["paperId"]
        self.b_new = rec("NewSeg: builds on the seed", cites=30, months=10, cited=[S])
        self.b_weak = rec("WeakSeg: too new to judge", cites=5, months=3, cited=[S])
        self.c_two = rec("TwoCore: part segmentation by reasoning", cites=25, months=8, cited=[L, P], sources=["core-citers"])
        self.c_paco = rec("PacoNet: part segmentation of objects", cites=40, months=8, cited=[P], sources=["core-citers"])
        self.c_generic = rec("InternVL: a generic multimodal LLM", cites=3450, months=20, cited=[L, P], sources=["core-citers"])
        self.c_abstract = rec("Seg-Zero: reasoning-chain guided masks", cites=60, months=10, cited=[L], sources=["core-citers"])
        self.c_abstract["abstract"] = "We tackle reasoning and part segmentation with a decoupled model."
        self.c_topic = rec("PartNet++: part segmentation with LLMs", cites=30, months=6, cited=[L], sources=["core-citers"])
        self.c_offtopic = rec("ChartQA reasoning with MLLMs", cites=100, months=6, cited=[L], sources=["core-citers"])
        self.c_hubby = rec("Robot grasping with foundation models", cites=80, months=6, cited=[L, H], sources=["core-citers"])
        self.c_classic = rec("DeepLab: part segmentation classic", cites=21745, months=110, cited=[P], sources=["core-citers"])
        self.c_ref = rec("A paper the seed itself cites", cites=500, months=20, cited=[L, P], paper_id=self.bg["paperId"])
        self.pool = pool([self.b_new, self.b_weak, self.c_two, self.c_paco, self.c_generic, self.c_abstract, self.c_topic,
                          self.c_offtopic, self.c_hubby, self.c_classic, self.c_ref])

    def run_select(self, **over):
        return select(self.pool, [self.s], self.core, cfg(**over), TODAY)

    def titles(self, rows):
        return [r["title"] for r in rows]

    def test_section_a1_lists_every_compared_paper_without_a_citation_bar(self):
        out = self.run_select()
        self.assertEqual(sorted(self.titles(out["compared"])),
                         sorted(r["title"] for r in (self.lisa, self.paco, self.tiny, self.sam)))
        self.assertEqual(self.titles(out["related"]), [self.rel["title"]])

    def test_follow_ups_citing_the_seed_pass_the_gate(self):
        out = self.run_select()
        self.assertEqual(self.titles(seed_rows(out)), [self.b_new["title"]])

    def test_widening_runs_when_b_is_short_and_keeps_relevant_recent_papers(self):
        out = self.run_select()
        self.assertTrue(out["expanded"])
        self.assertIn("only 1 influential paper", out["expand_why"])
        self.assertEqual(sorted(self.titles(expand_rows(out))),
                         sorted(r["title"] for r in (self.c_two, self.c_paco, self.c_abstract, self.c_topic)))

    def test_widening_requires_the_seeds_topic_even_with_co_citations(self):
        # A generic multimodal LLM co-citing two compared papers is not new work on the seed's topic.
        self.assertNotIn(self.c_generic["title"], self.titles(expand_rows(self.run_select())))

    def test_multi_word_keywords_match_abstracts(self):
        self.assertIn(self.c_abstract["title"], self.titles(expand_rows(self.run_select())))

    def test_widening_drops_off_topic_co_citation_hubs_and_old_single_link_classics(self):
        kept = self.titles(expand_rows(self.run_select()))
        for r in (self.c_offtopic, self.c_hubby, self.c_classic):
            self.assertNotIn(r["title"], kept)

    def test_the_seeds_own_references_are_never_forward_papers(self):
        out = self.run_select()
        self.assertNotIn(self.c_ref["title"], self.titles(out["follow_ups"] + out["also"]))

    def test_no_widening_when_b_is_long_enough(self):
        out = self.run_select(min_forward=1)
        self.assertFalse(out["expanded"])
        self.assertEqual(expand_rows(out), [])
        self.assertIn("not needed", out["expand_why"])

    def test_expand_never_and_always(self):
        self.assertEqual(expand_rows(self.run_select(expand="never")), [])
        self.assertTrue(self.run_select(expand="always", min_forward=1)["expanded"])

    def test_widening_needs_forward_to_have_searched_it(self):
        self.pool["meta"]["expanded"] = False
        out = self.run_select()
        self.assertEqual(expand_rows(out), [])
        self.assertIn("re-run `forward`", out["expand_why"])

    def test_agent_exclusions_backfill(self):
        out = select(self.pool, [self.s], self.core, cfg(), TODAY, skip_keys={self.c_two["paperId"][:8]})
        self.assertNotIn(self.c_two["title"], self.titles(expand_rows(out)))
        self.assertIn(self.c_topic["title"], self.titles(expand_rows(out)))

    def test_excluding_a_compared_paper_removes_it_from_a_and_from_relevance(self):
        out = select(self.pool, [self.s], self.core, cfg(), TODAY, skip_keys={self.paco["paperId"][:8]})
        self.assertNotIn(self.paco["title"], self.titles(out["compared"]))
        self.assertNotIn(self.c_paco["title"], self.titles(expand_rows(out)))  # its only link was PACO

    def test_builds_on_names_the_compared_papers(self):
        out = self.run_select()
        two = next(r for r in expand_rows(out) if r["title"] == self.c_two["title"])
        self.assertEqual(sorted(two["builds_on"]), ["LISA", "PACO"])
        self.assertEqual(two["builds_on"][0], "PACO")  # most specific (least cited) first


class CapsAndDedupe(unittest.TestCase):
    def test_caps_send_the_rest_to_also(self):
        s, core = world()
        papers = [rec("Paper %d" % i, cites=100 + i, months=10, cited=[s["paperId"]]) for i in range(5)]
        out = select(pool(papers), [s], core, cfg(max_follow_ups=2), TODAY)
        self.assertEqual(len(seed_rows(out)), 2)
        self.assertEqual(len(out["also"]), 3)
        self.assertEqual(seed_rows(out)[0]["title"], "Paper 4")  # best score first

    def test_duplicate_records_of_one_paper_are_merged(self):
        s, core = world()
        a = rec("Semantic-SAM: Segment and Recognize Anything at Any Granularity", cites=300, months=20, cited=[s["paperId"]])
        b = rec("Segment and Recognize Anything at Any Granularity", cites=290, months=20, cited=[s["paperId"]])
        out = select(pool([a, b]), [s], core, cfg(), TODAY)
        self.assertEqual(len(seed_rows(out)), 1)

    def test_widening_is_balanced_across_the_papers_it_builds_on(self):
        s, core = world()
        lisa, paco = core[0], core[1]
        lisa_kids = [rec("LISA follow-up %d: part segmentation" % i, cites=200 + i, months=8, cited=[lisa["paperId"]],
                         sources=["core-citers"]) for i in range(8)]
        paco_kids = [rec("PACO follow-up %d: part segmentation" % i, cites=30 + i, months=8, cited=[paco["paperId"]],
                         sources=["core-citers"]) for i in range(2)]
        out = select(pool(lisa_kids + paco_kids), [s], core, cfg(max_follow_ups=4), TODAY)
        self.assertEqual(len(expand_rows(out)), 4)
        self.assertTrue(any("PACO follow-up" in r["title"] for r in expand_rows(out)))


class Surveys(unittest.TestCase):
    def test_at_most_one_survey_per_section(self):
        s, core = world()
        papers = [rec("Survey %d: a survey of part segmentation" % i, cites=300 - i, months=10, cited=[s["paperId"]])
                  for i in range(3)] + [rec("Method %d" % i, cites=20 + i, months=10, cited=[s["paperId"]]) for i in range(3)]
        out = select(pool(papers), [s], core, cfg(), TODAY)
        surveys = [r for r in seed_rows(out) if "survey" in r["title"].lower()]
        self.assertEqual([r["title"] for r in surveys], ["Survey 0: a survey of part segmentation"])
        self.assertEqual(len(seed_rows(out)), 4)
        self.assertEqual(sum("survey" in r["title"].lower() for r in out["also"]), 2)


class MultiSeed(unittest.TestCase):
    def test_a_reference_of_one_seed_that_cites_the_other_is_a_follow_up(self):
        lora = seed(1, cites=24000, title="LoRA: Low-Rank Adaptation of Large Language Models")
        dora = seed(2, cites=1000, title="DoRA: Weight-Decomposed Low-Rank Adaptation")
        qlora = core_row("QLoRA: Efficient Finetuning of Quantized LLMs", "background", cites=3000, include=False,
                         seeds=[dora["key"]])
        adam = core_row("Adam: A Method for Stochastic Optimization", "background", cites=90000, include=False)
        papers = [rec(qlora["title"], cites=3000, months=40, cited=[lora["paperId"]], paper_id=qlora["paperId"]),
                  rec(adam["title"], cites=90000, months=100, cited=[], paper_id=adam["paperId"])]
        out = select(pool(papers), [lora, dora], [qlora, adam], cfg(), TODAY)
        self.assertEqual([r["title"] for r in seed_rows(out)], [qlora["title"]])

    def test_a_compared_paper_that_cites_a_seed_stays_in_section_a_only(self):
        s, core = world()
        lisa = core[0]
        papers = [rec(lisa["title"], cites=1068, months=30, cited=[s["paperId"]], paper_id=lisa["paperId"])]
        out = select(pool(papers), [s], core, cfg(), TODAY)
        self.assertIn(lisa["title"], [r["title"] for r in out["compared"]])
        self.assertEqual(seed_rows(out), [])

    def test_papers_citing_either_seed_are_follow_ups_and_record_which(self):
        s1, core = world()
        s2 = seed(2, title="SecondSeed: Another Paper")
        a = rec("Cites one", cites=60, months=30, cited=[s1["paperId"]])
        b = rec("Cites both", cites=60, months=30, cited=[s1["paperId"], s2["paperId"]])
        out = select(pool([a, b]), [s1, s2], core, cfg(), TODAY)
        by_title = {r["title"]: r for r in seed_rows(out)}
        self.assertEqual(by_title["Cites one"]["cites_seeds"], [s1["key"]])
        self.assertEqual(sorted(by_title["Cites both"]["cites_seeds"]), sorted([s1["key"], s2["key"]]))


if __name__ == "__main__":
    unittest.main()
