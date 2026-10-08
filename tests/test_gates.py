"""The influence gates (GATES) and the SOTA score that ranks routes B and C."""
import unittest

from helpers import TODAY, cfg, fbsearch, rec

gate = fbsearch.gate


class SeedRouteGate(unittest.TestCase):
    """Route B: papers citing the seed."""

    def test_fifty_citations_pass_at_any_age(self):
        self.assertIsNotNone(gate(rec("Old classic", cites=50, months=60), "seed", cfg(), TODAY))
        self.assertIsNone(gate(rec("Old classic", cites=49, months=60), "seed", cfg(), TODAY))

    def test_recent_bracket_needs_ten_citations(self):
        self.assertIsNotNone(gate(rec("New", cites=10, months=6), "seed", cfg(), TODAY))
        self.assertIsNone(gate(rec("New", cites=9, months=6), "seed", cfg(), TODAY))

    def test_mid_bracket_needs_twenty_five_citations(self):
        self.assertIsNotNone(gate(rec("Mid", cites=25, months=18), "seed", cfg(), TODAY))
        self.assertIsNone(gate(rec("Mid", cites=24, months=18), "seed", cfg(), TODAY))

    def test_stars_need_a_documented_readme(self):
        good = rec("Starred", cites=2, months=6, stars=100, docs=4)
        thin = rec("Starred", cites=2, months=6, stars=100, docs=3)
        self.assertIn("★100", gate(good, "seed", cfg(), TODAY))
        self.assertIsNone(gate(thin, "seed", cfg(), TODAY))

    def test_unread_readme_needs_twice_the_stars(self):
        self.assertIsNone(gate(rec("S", cites=0, months=6, stars=199, docs=None), "seed", cfg(), TODAY))
        reason = gate(rec("S", cites=0, months=6, stars=200, docs=None), "seed", cfg(), TODAY)
        self.assertIn("README not checked", reason)

    def test_mid_bracket_stars(self):
        self.assertIsNotNone(gate(rec("S", cites=3, months=20, stars=250, docs=5), "seed", cfg(), TODAY))
        self.assertIsNone(gate(rec("S", cites=3, months=20, stars=249, docs=5), "seed", cfg(), TODAY))

    def test_old_paper_can_pass_on_a_famous_repo(self):
        self.assertIsNotNone(gate(rec("Toolkit", cites=12, months=40, stars=1000, docs=4), "seed", cfg(), TODAY))
        self.assertIsNone(gate(rec("Toolkit", cites=12, months=40, stars=999, docs=5), "seed", cfg(), TODAY))

    def test_unverified_repo_does_not_count(self):
        r = rec("S", cites=0, months=6, stars=5000, docs=5)
        r["code"]["github"][0]["source"] = "github-search (verify)"
        self.assertIsNone(gate(r, "seed", cfg(), TODAY))


class ExpandRouteGate(unittest.TestCase):
    """Route C: papers citing the comparison set."""

    def test_brackets(self):
        self.assertIsNotNone(gate(rec("C", cites=20, months=10), "expand", cfg(), TODAY))
        self.assertIsNone(gate(rec("C", cites=19, months=10), "expand", cfg(), TODAY))
        self.assertIsNotNone(gate(rec("C", cites=40, months=20), "expand", cfg(), TODAY))
        self.assertIsNone(gate(rec("C", cites=39, months=20), "expand", cfg(), TODAY))

    def test_stars(self):
        self.assertIsNotNone(gate(rec("C", cites=0, months=10, stars=150, docs=4), "expand", cfg(), TODAY))
        self.assertIsNone(gate(rec("C", cites=0, months=10, stars=149, docs=5), "expand", cfg(), TODAY))
        self.assertIsNotNone(gate(rec("C", cites=0, months=20, stars=250, docs=4), "expand", cfg(), TODAY))
        self.assertIsNone(gate(rec("C", cites=0, months=20, stars=249, docs=5), "expand", cfg(), TODAY))

    def test_old_papers_are_left_out_by_default(self):
        # Route C is for recent work: in the InstructPart run an open bracket gave 4 of 10 slots to 2023 papers.
        self.assertIsNone(gate(rec("GLaMM", cites=588, months=35), "expand", cfg(), TODAY, core_score=3))

    def test_reopened_old_bracket_needs_citations_and_two_core_papers(self):
        c = cfg(gate=["expand.old=200"])
        old = rec("Classic", cites=21745, months=110)
        self.assertIsNone(gate(old, "expand", c, TODAY, core_score=1))  # DeepLab citing one compared dataset
        self.assertIsNotNone(gate(old, "expand", c, TODAY, core_score=2))
        self.assertIsNone(gate(rec("Classic", cites=199, months=40), "expand", c, TODAY, core_score=3))

    def test_old_papers_have_no_stars_route(self):
        c = cfg(gate=["expand.old=200"])
        self.assertIsNone(gate(rec("C", cites=5, months=40, stars=50000, docs=5), "expand", c, TODAY, core_score=3))


class Overrides(unittest.TestCase):
    def test_gate_flag(self):
        c = cfg(gate=["seed.recent=3,50"])
        self.assertEqual(c["gates"]["seed.recent"], (3, 50))
        self.assertIsNotNone(gate(rec("N", cites=3, months=6), "seed", c, TODAY))

    def test_gate_flag_keeps_stars_when_omitted_and_dash_turns_them_off(self):
        self.assertEqual(cfg(gate=["seed.mid=30"])["gates"]["seed.mid"], (30, 250))
        self.assertEqual(cfg(gate=["seed.mid=30,-"])["gates"]["seed.mid"], (30, None))

    def test_dash_closes_a_bracket(self):
        c = cfg(gate=["seed.recent=-"])
        self.assertEqual(c["gates"]["seed.recent"][0], None)
        self.assertIsNone(gate(rec("N", cites=49, months=6, stars=999, docs=5), "seed", c, TODAY))
        self.assertIsNotNone(gate(rec("N", cites=50, months=6), "seed", c, TODAY))  # seed.any still applies

    def test_unknown_gate_is_an_error(self):
        with self.assertRaises(SystemExit):
            cfg(gate=["seed.ancient=1"])

    def test_min_citations_is_a_hard_floor_without_stars(self):
        c = cfg(min_citations=50)
        self.assertIsNone(gate(rec("N", cites=30, months=6), "seed", c, TODAY))
        self.assertIsNone(gate(rec("N", cites=1, months=6, stars=50000, docs=5), "seed", c, TODAY))
        self.assertIsNotNone(gate(rec("N", cites=50, months=6), "seed", c, TODAY))

    def test_brackets_follow_the_month_flags(self):
        c = cfg(recent_months=6)
        self.assertIsNone(gate(rec("N", cites=10, months=9), "seed", c, TODAY))  # now "mid": needs 25

    def test_deprecated_recent_min_citations(self):
        self.assertEqual(cfg(recent_min_citations=7)["gates"]["seed.recent"], (7, 100))


class SotaScore(unittest.TestCase):
    def test_velocity_beats_raw_counts(self):
        fast = rec("Fast", cites=60, months=6)
        slow = rec("Slow", cites=200, months=48)
        self.assertGreater(fbsearch.sota_score(fast, TODAY, cfg()), fbsearch.sota_score(slow, TODAY, cfg()))

    def test_stars_per_month_count(self):
        plain = rec("Plain", cites=20, months=10)
        starred = rec("Starred", cites=20, months=10, stars=600, docs=5)
        self.assertGreater(fbsearch.sota_score(starred, TODAY, cfg()), fbsearch.sota_score(plain, TODAY, cfg()))

    def test_surveys_rank_lower(self):
        method = rec("FooNet: segmentation", cites=100, months=10)
        survey = rec("A survey of segmentation", cites=100, months=10)
        self.assertGreater(fbsearch.sota_score(method, TODAY, cfg()), fbsearch.sota_score(survey, TODAY, cfg()))


if __name__ == "__main__":
    unittest.main()
