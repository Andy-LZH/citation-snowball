"""--focus: what the user is looking for among the results. Matching papers rank first; the gates do not move."""
import contextlib
import io
import os
import tempfile
import unittest

from helpers import TODAY, cfg, core_row, fbsearch, pool, rec, seed

select = fbsearch.select_papers


class PhraseHits(unittest.TestCase):
    def test_whole_words_any_case_plurals_and_hyphenated_variants(self):
        text = "We scale DiTs and keep a ViT-L/14 encoder; the edit model is frozen."
        self.assertEqual(fbsearch.phrase_hits(text, ["DiT", "ViT", "diffusion transformer", "edit"]), ["DiT", "ViT", "edit"])
        self.assertEqual(fbsearch.phrase_hits("A credit model.", ["DiT"]), [])  # "credit" is not "DiT"

    def test_multi_word_phrases_match_across_case_and_plural(self):
        self.assertEqual(fbsearch.phrase_hits("Diffusion Transformers (DiT) replace the U-Net.", ["diffusion transformer", "u-net"]),
                         ["diffusion transformer", "u-net"])

    def test_no_text_or_empty_phrase_gives_no_hits(self):
        self.assertEqual(fbsearch.phrase_hits(None, ["DiT"]), [])
        self.assertEqual(fbsearch.phrase_hits("a DiT", ["", " "]), [])


HTML = """<html><body><article class="ltx_document">
<section class="ltx_section" id="S1"><h2 class="ltx_title ltx_title_section">1 Introduction</h2>
<div class="ltx_para"><p class="ltx_p">Prior work uses a DiT backbone.</p></div></section>
<section class="ltx_section" id="S2"><h2 class="ltx_title ltx_title_section">2 Related Work</h2>
<div class="ltx_para"><p class="ltx_p">DiT [1] and U-ViT [2] are diffusion transformers.</p></div></section>
<section class="ltx_section" id="S3"><h2 class="ltx_title ltx_title_section">3 FooNet</h2>
<section class="ltx_subsection" id="S3.SS1"><h3 class="ltx_title ltx_title_subsection">3.1 Architecture</h3>
<div class="ltx_para"><p class="ltx_p">We build on a ViT-L/14 encoder.</p></div></section>
<section class="ltx_subsection" id="S3.SS2"><h3 class="ltx_title ltx_title_subsection">3.2 Training</h3>
<div class="ltx_para"><p class="ltx_p">We train with Adam.</p></div></section></section>
<section class="ltx_section" id="S4"><h2 class="ltx_title ltx_title_section">4 Experiments</h2>
<div class="ltx_para"><p class="ltx_p">We compare against DiT-XL/2.</p></div></section>
</article></body></html>"""


class FakeHttp:
    """Serves the arXiv HTML pages it was given; every other request (Semantic Scholar batches, ar5iv) returns None."""

    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url, **kw):
        return self.pages.get(url)


class MethodText(unittest.TestCase):
    def test_takes_method_headed_sections_and_subsections_only(self):
        text = fbsearch.method_text(FakeHttp({"https://arxiv.org/html/2501.00001": HTML}), "2501.00001")
        self.assertIn("ViT-L/14", text)  # 3.1 Architecture, under a section named after the model
        self.assertNotIn("DiT", text)  # introduction, related work and experiments name prior work, not the method
        self.assertNotIn("Adam", text)  # 3.2 Training is not a method heading

    def test_empty_without_a_method_heading_and_none_without_html(self):
        page = HTML.replace("3.1 Architecture", "3.1 Overview")
        self.assertEqual(fbsearch.method_text(FakeHttp({"https://arxiv.org/html/2501.00001": page}), "2501.00001"), "")
        self.assertIsNone(fbsearch.method_text(FakeHttp({}), "2501.00002"))


class FocusHits(unittest.TestCase):
    def test_abstract_and_method_are_searched_and_html_is_counted(self):
        a = rec("A: a video model", cites=10)
        a["abstract"] = "A diffusion transformer for video."
        b = rec("B: unrelated", cites=10)
        b["abstract"] = "A convolutional network."
        b["externalIds"] = {}  # no arXiv id: nothing to read a method from
        http = FakeHttp({"https://arxiv.org/html/2501.00001": HTML})
        hits, checked, with_html = fbsearch.focus_hits(http, ["diffusion transformer", "ViT"], [a, b])
        self.assertEqual(hits, {a["paperId"]: {"abstract": ["diffusion transformer"], "method": ["ViT"]}})
        self.assertEqual((checked, with_html), (2, 1))


class Config(unittest.TestCase):
    def test_focus_flag_is_split_inherited_and_cleared(self):
        self.assertEqual(cfg(focus="DiT, diffusion transformer,,ViT")["focus"], ["DiT", "diffusion transformer", "ViT"])
        self.assertEqual(cfg()["focus"], [])

        class Inherit:
            focus = None

        class Off:
            focus = ""
        self.assertEqual(fbsearch.selection_config(Inherit(), base={"focus": ["DiT"]})["focus"], ["DiT"])
        self.assertEqual(fbsearch.selection_config(Off(), base={"focus": ["DiT"]})["focus"], [])


class Ranking(unittest.TestCase):
    def setUp(self):
        self.s = seed()
        self.core = [core_row("LISA: Reasoning Segmentation via Large Language Model", "benchmarked_model", cites=1068)]
        S = self.s["paperId"]
        self.strong = rec("Strong: no match", cites=300, months=10, cited=[S])
        self.match = rec("Match: uses a DiT", cites=30, months=10, cited=[S])
        self.too_new = rec("TooNew: a DiT too, but 2 citations", cites=2, months=3, cited=[S])  # fails the gate
        self.pool = pool([self.strong, self.match, self.too_new])
        self.focus = {self.match["paperId"]: {"abstract": ["DiT"]}, self.too_new["paperId"]: {"method": ["DiT"]}}

    def titles(self, rows):
        return [r["title"] for r in rows]

    def test_matches_rank_first_but_a_match_never_passes_a_gate(self):
        out = select(self.pool, [self.s], self.core, cfg(), TODAY, focus=self.focus)
        self.assertEqual(self.titles(out["follow_ups"]), [self.match["title"], self.strong["title"]])
        self.assertEqual(out["follow_ups"][0]["focus"], {"abstract": ["DiT"]})
        self.assertIsNone(out["follow_ups"][1]["focus"])
        self.assertNotIn(self.too_new["title"], self.titles(out["follow_ups"] + out["also"]))

    def test_without_focus_the_order_is_by_score(self):
        out = select(self.pool, [self.s], self.core, cfg(), TODAY)
        self.assertEqual(self.titles(out["follow_ups"]), [self.strong["title"], self.match["title"]])

    def test_matches_take_the_cap_first_and_the_overflow_lists_matches_first(self):
        out = select(self.pool, [self.s], self.core, cfg(max_follow_ups=1), TODAY, focus=self.focus)
        self.assertEqual(self.titles(out["follow_ups"]), [self.match["title"]])
        self.assertEqual(self.titles(out["also"]), [self.strong["title"]])
        other = rec("Other: a DiT with fewer citations", cites=20, months=10, cited=[self.s["paperId"]])
        focus = dict(self.focus, **{other["paperId"]: {"abstract": ["DiT"]}})
        out = select(pool([self.strong, self.match, other]), [self.s], self.core, cfg(max_follow_ups=1), TODAY, focus=focus)
        self.assertEqual(self.titles(out["also"]), [other["title"], self.strong["title"]])

    def test_compared_papers_carry_the_mark_without_moving(self):
        lisa = self.core[0]
        focus = {lisa["paperId"]: {"method": ["DiT"]}}
        out = select(self.pool, [self.s], self.core, cfg(), TODAY, focus=focus)
        self.assertEqual(out["compared"][0]["focus"], {"method": ["DiT"]})
        self.assertEqual(fbsearch.focus_note(out["compared"][0]), "DiT (method)")
        self.assertEqual(fbsearch.focus_note(out["follow_ups"][0]), "")


class BriefsAndSummary(unittest.TestCase):
    """What the agent reads after `select`: briefs.md and the printed summary both say where the phrases appear."""

    def papers(self):
        hit = {"key": "aaaaaaaa", "paperId": "a" * 40, "title": "Match: uses a DiT", "year": 2025, "citationCount": 30,
               "section": "follow_up", "route": "seed", "gate": "30 citations in 10 months", "score": 1.1,
               "focus": {"abstract": ["DiT"]}, "tldr": "A DiT for parts.", "code": {"github": [], "hf": []}}
        miss = dict(hit, key="bbbbbbbb", paperId="b" * 40, title="Strong: no match", focus=None, tldr="A CNN.")
        return [hit, miss]

    def test_briefs_carry_the_note_and_the_instruction_only_when_something_matched(self):
        with tempfile.TemporaryDirectory() as wd:
            fbsearch.write_briefs(wd, [seed()], self.papers())
            with open(os.path.join(wd, "briefs.md"), encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("| no code found | focus: DiT (abstract)\n", text)
        self.assertEqual(text.count("focus: DiT"), 1)
        self.assertIn("A `focus:` note says where the user's focus phrases appear", text)
        with tempfile.TemporaryDirectory() as wd:
            fbsearch.write_briefs(wd, [seed()], self.papers()[1:])
            with open(os.path.join(wd, "briefs.md"), encoding="utf-8") as fh:
                self.assertNotIn("focus", fh.read())

    def test_summary_prints_a_focus_line_and_marks_the_rows(self):
        meta = {"pool": 10, "seed_citers": 5, "topic_hits": 0, "verified": 3, "expand_shown": False, "github_token": True,
                "expand_why": "not needed: 2 influential papers cite the seed", "selection": {"focus": ["DiT", "ViT"]},
                "focus_checked": 7, "focus_read": 4}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fbsearch.print_select_summary("wd", self.papers(), meta, {})
        self.assertIn("FOCUS   DiT, ViT: 1 of 2 kept papers mention it (B: 1 of 2); method sections read for 4 of 7 papers "
                      "checked", out.getvalue())
        self.assertIn("focus: DiT (abstract) | cites the seed; 30 citations in 10 months", out.getvalue())
        meta["selection"]["focus"] = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fbsearch.print_select_summary("wd", self.papers()[1:], meta, {})
        self.assertNotIn("FOCUS", out.getvalue())


if __name__ == "__main__":
    unittest.main()
