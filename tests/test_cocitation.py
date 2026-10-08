"""Co-citation: section A2 (what the field builds on), section C (latest models) and B's same-area check."""
import unittest

from helpers import TODAY, cfg, cocite_block, core_row, fbsearch, meta_row, pool, rec, seed

stats_of = fbsearch.cocite_stats


def found(co, **over):
    return fbsearch.foundations(co, stats_of(co, TODAY), set(), cfg(**over))


def latest(co, **over):
    return fbsearch.latest_models(co, stats_of(co, TODAY), set(), cfg(**over))


def titles(rows):
    return [r["title"] for r in rows]


class Foundations(unittest.TestCase):
    def test_co_cited_and_still_cited_by_recent_work(self):
        # The InstructPart numbers: ADE20K is cited by 6 of the seed + 14 compared works and by recent papers.
        ade = meta_row("Scene Parsing through ADE20K Dataset", 4100, 108)
        co = cocite_block([ade], backward={ade["title"]: 6}, forward={ade["title"]: 6})
        rows = found(co)
        self.assertEqual(titles(rows), [ade["title"]])
        self.assertEqual((rows[0]["kind"], rows[0]["k_b"], rows[0]["n_b"]), ("data", 6, 15))

    def test_co_cited_but_no_longer_cited_is_left_out(self):
        mask = meta_row("Open-Vocabulary Panoptic Segmentation with MaskCLIP", 96 + 300, 40)
        co = cocite_block([mask], backward={mask["title"]: 5}, forward={})
        self.assertEqual(found(co), [])

    def test_without_enough_recent_area_papers_backward_co_citation_is_enough(self):
        mask = meta_row("Open-Vocabulary Panoptic Segmentation with MaskCLIP", 400, 40)
        co = cocite_block([mask], backward={mask["title"]: 5}, forward={}, area_months=[3, 4, 5])
        self.assertEqual(titles(found(co)), [mask["title"]])

    def test_needs_twenty_percent_of_the_seed_and_compared_works(self):
        lavt = meta_row("LAVT: Language-Aware Vision Transformer for Referring Image Segmentation", 564, 58)
        co = cocite_block([lavt], backward={lavt["title"]: 2}, forward={lavt["title"]: 10})
        self.assertEqual(found(co), [])  # 2 of 15 is below max(3, 20%)

    def test_field_consensus_brings_in_older_papers(self):
        pixellm = meta_row("PixelLM: Pixel Reasoning with Large Multimodal Model", 310, 34)
        co = cocite_block([pixellm], backward={}, forward={pixellm["title"]: 30})
        self.assertEqual(titles(found(co)), [pixellm["title"]])

    def test_recent_consensus_is_a_latest_model_not_a_foundation(self):
        sam2 = meta_row("SAM 2: Segment Anything in Images and Videos", 4500, 26)
        co = cocite_block([sam2], backward={}, forward={sam2["title"]: 40})
        self.assertEqual(found(co), [])
        self.assertEqual(titles(latest(co)), [sam2["title"]])

    def test_foundations_are_influential(self):
        small = meta_row("A Small Dataset of Parts", 90, 60)
        co = cocite_block([small], backward={small["title"]: 8}, forward={small["title"]: 20})
        self.assertEqual(found(co), [])

    def test_specific_papers_rank_above_ones_cited_everywhere(self):
        coco = meta_row("Microsoft COCO: Common Objects in Context", 56000, 140, abstract="We present a new dataset with "
                        "the goal of advancing the state-of-the-art in object recognition.")
        refcoco = meta_row("Modeling Context in Referring Expressions", 1900, 120, abstract="We present a new dataset "
                           "for referring expressions.")
        co = cocite_block([coco, refcoco], backward={coco["title"]: 11, refcoco["title"]: 8},
                          forward={coco["title"]: 7, refcoco["title"]: 7})
        self.assertEqual(titles(found(co)), [refcoco["title"], coco["title"]])

    def test_datasets_are_recognised_from_the_abstract(self):
        coco = meta_row("Microsoft COCO: Common Objects in Context", 56000, 140,
                        abstract="We present a new dataset with the goal of advancing object recognition.")
        clip = meta_row("Learning Transferable Visual Models From Natural Language Supervision", 57000, 68,
                        abstract="We demonstrate that a simple pre-training task is an efficient way to learn.")
        co = cocite_block([coco, clip], backward={coco["title"]: 11, clip["title"]: 7},
                          forward={coco["title"]: 7, clip["title"]: 30})
        kinds = {r["title"]: r["kind"] for r in found(co)}
        self.assertEqual(kinds, {coco["title"]: "data", clip["title"]: "model"})

    def test_journal_and_conference_twins_are_one_row(self):
        a = meta_row("Scene Parsing through ADE20K Dataset", 4100, 108)
        b = meta_row("Semantic Understanding of Scenes Through the ADE20K Dataset", 2500, 120)
        co = cocite_block([a, b], backward={a["title"]: 6, b["title"]: 4}, forward={a["title"]: 6, b["title"]: 6})
        rows = found(co)
        self.assertEqual(titles(rows), [a["title"]])
        self.assertEqual(rows[0]["twins"], [b["title"]])

    def test_model_versions_are_not_twins(self):
        q2 = meta_row("Qwen2-VL: Enhancing Vision-Language Model's Perception", 5200, 40)
        q25 = meta_row("Qwen2.5-VL Technical Report", 6100, 33)
        co = cocite_block([q2, q25], backward={}, forward={q2["title"]: 30, q25["title"]: 30})
        self.assertEqual(sorted(titles(found(co))), sorted([q2["title"], q25["title"]]))

    def test_at_most_max_foundations_of_each_kind(self):
        rows = [meta_row("Dataset %d: a benchmark" % i, 500 + i, 60) for i in range(8)]
        co = cocite_block(rows, backward={r["title"]: 5 for r in rows}, forward={r["title"]: 10 for r in rows})
        self.assertEqual(len(found(co)), 6)
        self.assertEqual(len(found(co, max_foundations=3)), 3)


class LatestModels(unittest.TestCase):
    def test_a_new_model_used_by_a_share_of_later_papers(self):
        # SAM 3 has no reference list in Semantic Scholar, but 20 of the 89 area papers after it cite it.
        sam3 = meta_row("SAM 3: Segment Anything with Concepts", 1162, 11)
        co = cocite_block([sam3], forward={sam3["title"]: 20}, area_months=[1 + (i % 10) for i in range(89)])
        rows = latest(co)
        self.assertEqual(titles(rows), [sam3["title"]])
        self.assertEqual((rows[0]["support_f"], rows[0]["eligible"]), (20, 89))

    def test_the_window_decides_what_is_latest(self):
        sam2 = meta_row("SAM 2: Segment Anything in Images and Videos", 4500, 26)
        co = cocite_block([sam2], forward={sam2["title"]: 40})
        self.assertEqual(titles(latest(co)), [sam2["title"]])
        self.assertEqual(latest(co, latest_months=24), [])

    def test_too_few_later_papers_to_tell(self):
        brand_new = meta_row("Brand New Model", 300, 2)
        co = cocite_block([brand_new], forward={brand_new["title"]: 5})
        self.assertEqual(latest(co), [])  # only the papers 2 months old or newer could cite it

    def test_needs_fifteen_percent_of_the_later_papers(self):
        m = meta_row("Somewhat Used Model", 800, 12)  # 36 of the 100 area papers are newer than it
        self.assertEqual(len(latest(cocite_block([m], forward={m["title"]: 8}))), 1)  # 22%
        self.assertEqual(latest(cocite_block([m], forward={m["title"]: 5})), [])  # 14%


class SameAreaCheck(unittest.TestCase):
    def setUp(self):
        self.s = seed()
        self.lisa = core_row("LISA: Reasoning Segmentation via Large Language Model", "benchmarked_model", cites=1068)
        self.refcoco = meta_row("Modeling Context in Referring Expressions", 1900, 120)
        self.co = cocite_block([self.refcoco], backward={self.refcoco["title"]: 8}, forward={self.refcoco["title"]: 7})

    def run_select(self, papers, **over):
        p = pool(papers)
        p["cocitation"] = self.co
        return fbsearch.select_papers(p, [self.s], [self.lisa], cfg(**over), TODAY)

    def test_a_citer_from_another_area_is_left_out(self):
        S, R = self.s["paperId"], self.refcoco["paperId"]
        crisp = rec("CRISP: Contact-Guided Real2Sim", cites=0, months=10, cited=[S], stars=183, docs=5)
        same = rec("Affordance-R1: affordance reasoning", cites=26, months=14, cited=[S, R])
        unknown = rec("No Reference List Yet", cites=30, months=14, cited=[S], verified=False)
        kept = titles(self.run_select([crisp, same, unknown])["follow_ups"])
        self.assertEqual(sorted(kept), sorted([same["title"], unknown["title"]]))

    def test_min_coupling_zero_turns_it_off(self):
        crisp = rec("CRISP: Contact-Guided Real2Sim", cites=0, months=10, cited=[self.s["paperId"]], stars=183, docs=5)
        self.assertEqual(titles(self.run_select([crisp], min_coupling=0)["follow_ups"]), [crisp["title"]])

    def test_widening_rows_need_the_topic_and_the_coupling(self):
        L, R = self.lisa["paperId"], self.refcoco["paperId"]
        on_topic = rec("SegNet: part segmentation with reasoning", cites=60, months=10, cited=[L, R], sources=["core-citers"])
        uncoupled = rec("ArtNet: part segmentation of 3D meshes", cites=60, months=10, cited=[L], sources=["core-citers"])
        kept = titles(self.run_select([on_topic, uncoupled])["follow_ups"])
        self.assertEqual(kept, [on_topic["title"]])


class SelectIntegration(unittest.TestCase):
    def test_a_hub_excluded_from_the_core_can_still_be_a_foundation(self):
        s = seed()
        sam = core_row("Segment Anything", "related", cites=16000, include=False, year=2023)
        sam["excluded"] = True
        lisa = core_row("LISA: Reasoning Segmentation via Large Language Model", "benchmarked_model", cites=1068)
        m = meta_row("Segment Anything", 16000, 42, paper_id=sam["paperId"])
        p = pool([])
        p["cocitation"] = cocite_block([m], backward={m["title"]: 4}, forward={m["title"]: 41})
        out = fbsearch.select_papers(p, [s], [sam, lisa], cfg(), TODAY)
        self.assertEqual(titles(out["foundations"]), ["Segment Anything"])

    def test_sections_do_not_repeat_papers(self):
        s = seed()
        lisa = core_row("LISA: Reasoning Segmentation via Large Language Model", "benchmarked_model", cites=1068)
        rel = core_row("Visual Instruction Tuning", "related", cites=11000)
        segzero = rec("Seg-Zero: reasoning-chain guided part segmentation", cites=241, months=19,
                      cited=[s["paperId"]])
        llava = meta_row("Visual Instruction Tuning", 11000, 42, paper_id=rel["paperId"])
        lisa_meta = meta_row(lisa["title"], 1068, 38, paper_id=lisa["paperId"])
        seg_meta = meta_row(segzero["title"], 241, 19, paper_id=segzero["paperId"])
        p = pool([segzero])
        p["cocitation"] = cocite_block([llava, lisa_meta, seg_meta],
                                       backward={llava["title"]: 5, lisa_meta["title"]: 9},
                                       forward={llava["title"]: 30, lisa_meta["title"]: 50, seg_meta["title"]: 29})
        out = fbsearch.select_papers(p, [s], [lisa, rel], cfg(min_coupling=0), TODAY)
        self.assertEqual(titles(out["foundations"]), ["Visual Instruction Tuning"])
        self.assertEqual(out["related"], [])  # shown in A2 instead of the related-work line
        self.assertNotIn(lisa["title"], titles(out["foundations"] + out["latest"]))  # already in A1
        self.assertEqual(titles(out["follow_ups"]), [segzero["title"]])
        self.assertEqual(out["latest"], [])  # Seg-Zero is a follow-up, tagged instead of repeated
        self.assertGreater(out["follow_ups"][0]["share_f"], 0.15)


if __name__ == "__main__":
    unittest.main()
