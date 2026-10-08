"""Pieces of the backward stage that work offline: seed parsing, short names, multi-seed core merging."""
import os
import tempfile
import unittest

from helpers import core_row, fbsearch, seed


class ParseSeed(unittest.TestCase):
    def test_identifiers(self):
        cases = {
            "https://arxiv.org/abs/2306.01567v2": "ARXIV:2306.01567",
            "2402.09353": "ARXIV:2402.09353",
            "https://huggingface.co/papers/2505.18291": "ARXIV:2505.18291",
            "10.48550/arXiv.2106.09685": "ARXIV:2106.09685",
            "https://doi.org/10.1109/CVPR.2016.90": "DOI:10.1109/CVPR.2016.90",
            "https://aclanthology.org/2023.acl-long.1.pdf": "ACL:2023.acl-long.1",
            "doi: 10.1000/xyz": "DOI:10.1000/xyz",
        }
        for raw, want in cases.items():
            self.assertEqual(fbsearch.parse_seed(raw), want, raw)

    def test_titles_are_left_for_title_search(self):
        self.assertIsNone(fbsearch.parse_seed("Segment Anything"))


class Names(unittest.TestCase):
    def test_short_name(self):
        self.assertEqual(fbsearch.short_name("LoRA: Low-Rank Adaptation of Large Language Models"), "LoRA")
        self.assertEqual(fbsearch.short_name("BERT pre-training of deep bidirectional transformers"), "BERT")
        self.assertIsNone(fbsearch.short_name("Fine-Grained Part Segmentation of Things"))

    def test_same_title_matches_arxiv_and_venue_versions_only(self):
        self.assertTrue(fbsearch.same_title("Semantic-SAM: Segment and Recognize Anything",
                                            "Segment and Recognize Anything"))
        self.assertFalse(fbsearch.same_title("Video Mask Transfiner for High-Quality Video Instance Segmentation",
                                             "Mask Transfiner for High-Quality Instance Segmentation"))


class MergeCores(unittest.TestCase):
    def test_two_seeds_merge_into_one_core_set(self):
        s1, s2 = seed(1, title="InstructPart: parts"), seed(2, title="SPIN: subparts")
        lisa = core_row("LISA: Reasoning Segmentation", "related", include=False)
        paco1 = core_row("PACO: Parts and Attributes", "compared_dataset")
        paco2 = dict(paco1, role="benchmarked_model", evidence=["in S5.T1(row)"])
        lisa2 = dict(lisa, role="baseline", include=True, evidence=["in S4.T2(row)"])
        seed1_as_ref = core_row("InstructPart: parts", "related", paper_id=s1["paperId"])
        with tempfile.TemporaryDirectory() as wd:
            for s, rows in ((s1, [lisa, paco1]), (s2, [lisa2, paco2, seed1_as_ref])):
                os.makedirs(os.path.join(wd, s["dir"]))
                fbsearch.save_json(os.path.join(wd, s["dir"], "core.json"), rows)
            merged = fbsearch.merge_cores(wd, [s1, s2], max_core=30)
        by_title = {r["title"]: r for r in merged}
        self.assertNotIn("InstructPart: parts", by_title)  # a seed is never part of the comparison set
        self.assertTrue(by_title["LISA: Reasoning Segmentation"]["include"])  # included by either seed
        self.assertEqual(by_title["LISA: Reasoning Segmentation"]["role"], "baseline")  # strongest role wins
        self.assertEqual(by_title["PACO: Parts and Attributes"]["role"], "benchmarked_model")
        self.assertEqual(sorted(by_title["PACO: Parts and Attributes"]["seeds"]), sorted([s1["key"], s2["key"]]))
        self.assertTrue(any(e.startswith("[SPIN] ") for e in by_title["PACO: Parts and Attributes"]["evidence"]))


class DocsScore(unittest.TestCase):
    def test_a_complete_readme_scores_five(self):
        readme = """# FooSeg
## Installation
pip install -r requirements.txt
## Quick start / inference
python demo.py --image x.jpg
## Training
python train.py --config cfg.yaml
## Evaluation
python eval.py --checkpoint ckpt.pth
## Model zoo
Download pretrained weights from https://huggingface.co/foo/fooseg
"""
        self.assertEqual(fbsearch.docs_score(readme), (5, ["setup", "usage", "training", "evaluation", "weights"]))

    def test_a_stub_scores_low_and_no_readme_is_unknown(self):
        self.assertLessEqual(fbsearch.docs_score("# DPSeg\nCode coming soon.")[0], 1)
        self.assertEqual(fbsearch.docs_score(None), (None, []))
        self.assertEqual(fbsearch.docs_score(""), (None, []))


if __name__ == "__main__":
    unittest.main()


class FakeHttp:
    """Answers Hugging Face list calls from canned rows (no network)."""

    def __init__(self, models):
        self.models = models

    def fetch(self, url, **kw):
        return self.models if "/api/models?" in url else []


class HuggingFaceArtifacts(unittest.TestCase):
    def test_artifacts_that_merely_cite_the_paper_are_skipped(self):
        # Real tags from 2026-10: Microsoft's Magma-8B cites Set-of-Mark (2310.11441) but is not its model.
        rows = [{"id": "microsoft/Magma-8B", "likes": 418, "tags": ["arxiv:2502.13130", "arxiv:2310.11441"]}]
        found = fbsearch.hf_artifacts(FakeHttp(rows), "2310.11441",
                                      "Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding in GPT-4V",
                                      ["microsoft/SoM"])
        self.assertEqual(found, [])

    def test_named_artifact_of_the_repo_owner_is_verified(self):
        rows = [{"id": "ByteDance/Sa2VA-4B", "likes": 99, "tags": ["arxiv:2501.04001"]}]
        found = fbsearch.hf_artifacts(FakeHttp(rows), "2501.04001", "Sa2VA: Marrying SAM2 With MLLM", ["bytedance/Sa2VA"])
        self.assertEqual([(a["id"], a["verified"]) for a in found], [("ByteDance/Sa2VA-4B", True)])

    def test_single_paper_artifact_by_another_owner_is_unverified(self):
        rows = [{"id": "facebook/sam-vit-huge", "likes": 199, "tags": ["arxiv:2304.02643"]}]
        found = fbsearch.hf_artifacts(FakeHttp(rows), "2304.02643", "Segment Anything",
                                      ["facebookresearch/segment-anything"])
        self.assertEqual([(a["id"], a["verified"]) for a in found], [("facebook/sam-vit-huge", False)])


class Venues(unittest.TestCase):
    def test_short_venue(self):
        self.assertEqual(fbsearch.short_venue("Annual Meeting of the Association for Computational Linguistics"), "ACL")
        self.assertEqual(fbsearch.short_venue("IEEE/CVF International Conference on Computer Vision"), "ICCV")
        self.assertEqual(fbsearch.short_venue("arXiv.org"), "arXiv")
        self.assertEqual(fbsearch.short_venue("A Workshop Nobody Abbreviates"), "A Workshop Nobody Abbreviates")


class SearchHttp:
    """Answers GitHub repository searches from canned items: name searches, then README searches."""

    def __init__(self, name_items, readme_items):
        self.name_items, self.readme_items = name_items, readme_items

    def fetch(self, url, **kw):
        return {"items": self.readme_items if "in%3Areadme" in url else self.name_items}


def repo(full, description, stars=100):
    return {"full_name": full, "name": full.split("/")[1], "html_url": "https://github.com/" + full,
            "stargazers_count": stars, "description": description}


class GitHubSearch(unittest.TestCase):
    def test_official_implementation_of_the_paper_is_not_a_paper_list(self):
        seem = repo("UX-Decoder/Segment-Everything-Everywhere-All-At-Once",
                    '[NeurIPS 2023] Official implementation of the paper "Segment Everything Everywhere All at Once"', 4793)
        hit = fbsearch.github_search(SearchHttp([], [seem]), "Segment Everything Everywhere All at Once", "token")
        self.assertEqual((hit["repo"], hit["source"]),
                         ("UX-Decoder/Segment-Everything-Everywhere-All-At-Once", "github-search"))

    def test_paper_lists_are_still_skipped(self):
        lst = repo("someone/Awesome-Segmentation", "A curated list of papers on Segment Everything Everywhere All at Once")
        self.assertIsNone(fbsearch.github_search(SearchHttp([], [lst]), "Segment Everything Everywhere All at Once", "token"))

    def test_a_repo_named_exactly_like_the_paper_is_its_own(self):
        sam2 = repo("facebookresearch/sam2", "The repository provides code for running inference with the Meta Segment "
                    "Anything Model 2 (SAM 2), links for downloading the trained model checkpoints", 19958)
        hit = fbsearch.github_search(SearchHttp([sam2], []), "SAM 2: Segment Anything in Images and Videos", "token")
        self.assertEqual((hit["repo"], hit["source"]), ("facebookresearch/sam2", "github-search"))

    def test_a_longer_repo_name_still_needs_verifying(self):
        fork = repo("someone/sam2-finetune", "Fine-tune SAM 2 to segment anything", 50)
        hit = fbsearch.github_search(SearchHttp([fork], []), "SAM 2: Segment Anything in Images and Videos", "token")
        self.assertEqual(hit["source"], "github-search (verify)")

    def test_short_name_in_the_description_names_the_repo(self):
        mini = repo("Vision-CAIR/MiniGPT-4", "Open-sourced codes for MiniGPT-4 and MiniGPT-v2 (https://minigpt-4.github.io)", 25601)
        title = "MiniGPT-v2: large language model as a unified interface for vision-language multi-task learning"
        hit = fbsearch.github_search(SearchHttp([mini], [mini]), title, "token")
        self.assertEqual((hit["repo"], hit["source"]), ("Vision-CAIR/MiniGPT-4", "github-search"))
