"""The version number lives in four places; a release must bump all of them (see CONTRIBUTING.md)."""
import os
import re
import unittest

from helpers import fbsearch

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class VersionConsistency(unittest.TestCase):
    def test_skill_metadata_matches_the_script(self):
        m = re.search(r'^\s+version:\s*"([^"]+)"', read("SKILL.md"), re.M)
        self.assertIsNotNone(m, "SKILL.md needs metadata.version")
        self.assertEqual(m.group(1), fbsearch.VERSION)

    def test_changelog_top_entry_matches_the_script(self):
        m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", read("CHANGELOG.md"), re.M)
        self.assertIsNotNone(m, "CHANGELOG.md needs a '## [x.y.z] - date' entry")
        self.assertEqual(m.group(1), fbsearch.VERSION)

    def test_plugin_marketplace_version_matches_the_script(self):
        # Claude Code only updates plugin installs when this string changes; a forgotten bump strands those users.
        import json
        plugins = json.loads(read(".claude-plugin/marketplace.json"))["plugins"]
        self.assertEqual([p["version"] for p in plugins if p["name"] == "citation-snowball"], [fbsearch.VERSION])

    def test_user_agent_carries_the_version(self):
        self.assertIn("citation-snowball/%s" % fbsearch.VERSION, fbsearch.UA)

    def test_readme_excerpt_matches_the_example_map(self):
        """The README quotes examples/instructpart.md; a re-render must update both (citation links may be dropped)."""
        example = {re.sub(r"\| \[([\d.k]+)\]\(https://scholar\.google\.com/[^)]*\) \|", r"| \1 |", line)
                   for line in read("examples/instructpart.md").split("\n")}
        quoted = [line[2:] for line in read("README.md").split("\n")
                  if re.match(r"> (\| |\d+\. |   [↩↪])", line)]
        self.assertGreaterEqual(len(quoted), 4)
        for line in quoted:
            self.assertIn(line, example, "README quotes a line the example map no longer has: %s" % line[:80])

    def test_skill_description_states_no_stale_thresholds(self):
        desc = re.search(r'^description:\s*"(.+)"\s*$', read("SKILL.md"), re.M).group(1)
        for cites, stars in [fbsearch.GATES["seed.any"]]:
            self.assertIn(str(cites), desc, "the description should state the current citation bar")
        self.assertNotIn("at least 30 citations", desc)
        self.assertLessEqual(len(desc), 1024, "agentskills.io caps descriptions at 1024 characters")


if __name__ == "__main__":
    unittest.main()
