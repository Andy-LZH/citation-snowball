"""The update check: compare versions, ask GitHub at most once a day, never break a run."""
import json
import os
import tempfile
import time
import unittest

from helpers import fbsearch


class FakeGitHub:
    """Stands in for GitHub's releases/latest endpoint and counts the calls."""

    def __init__(self, tag="v9.0.0", fail=False):
        self.tag, self.fail, self.calls = tag, fail, 0

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise OSError("offline")
        return {"tag_name": self.tag, "html_url": "https://github.com/x/y/releases/tag/" + self.tag}


class Versions(unittest.TestCase):
    def test_numeric_order(self):
        vt = fbsearch.version_tuple
        self.assertGreater(vt("v1.10.0"), vt("1.9.3"))
        self.assertGreater(vt("1.4.0"), vt("1.3.0"))
        self.assertEqual(vt("v1.3.0"), vt("1.3.0"))
        self.assertEqual(vt("not a version"), ())


class LatestRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_asks_github_at_most_once_a_day(self):
        gh = FakeGitHub("v9.0.0")
        first = fbsearch.latest_release(self.cache, fetch=gh)
        second = fbsearch.latest_release(self.cache, fetch=gh)
        self.assertEqual(first, {"version": "9.0.0", "url": "https://github.com/x/y/releases/tag/v9.0.0"})
        self.assertEqual(second, first)
        self.assertEqual(gh.calls, 1)

    def test_asks_again_when_the_answer_is_old(self):
        gh = FakeGitHub("v9.0.0")
        fbsearch.latest_release(self.cache, fetch=gh)
        path = os.path.join(self.cache, "update-check.json")
        data = json.load(open(path))
        data["checked"] = time.time() - 25 * 3600
        json.dump(data, open(path, "w"))
        fbsearch.latest_release(self.cache, fetch=gh)
        self.assertEqual(gh.calls, 2)

    def test_a_failure_is_unknown_and_not_retried_all_day(self):
        gh = FakeGitHub(fail=True)
        self.assertIsNone(fbsearch.latest_release(self.cache, fetch=gh))
        self.assertIsNone(fbsearch.latest_release(self.cache, fetch=gh))
        self.assertEqual(gh.calls, 1)

    def test_a_corrupt_cache_file_is_ignored(self):
        with open(os.path.join(self.cache, "update-check.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(fbsearch.latest_release(self.cache, fetch=FakeGitHub("v2.0.0"))["version"], "2.0.0")

    def test_no_cache_dir_still_works(self):
        self.assertEqual(fbsearch.latest_release(None, fetch=FakeGitHub("v2.0.0"))["version"], "2.0.0")


class Notice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("FBS_NO_UPDATE_CHECK", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("FBS_NO_UPDATE_CHECK", None)

    def test_a_newer_release_gives_a_notice(self):
        notice = fbsearch.update_notice(self.tmp.name, fetch=FakeGitHub("v99.0.0"))
        self.assertIn("citation-snowball 99.0.0 is out (this copy is %s)" % fbsearch.VERSION, notice)
        self.assertIn("Ask the user before updating", notice)
        self.assertIn(" update", notice)

    def test_the_same_or_an_older_release_gives_none(self):
        self.assertIsNone(fbsearch.update_notice(self.tmp.name, fetch=FakeGitHub("v" + fbsearch.VERSION)))
        with tempfile.TemporaryDirectory() as other:
            self.assertIsNone(fbsearch.update_notice(other, fetch=FakeGitHub("v0.1.0")))

    def test_opt_out(self):
        os.environ["FBS_NO_UPDATE_CHECK"] = "1"
        gh = FakeGitHub("v99.0.0")
        self.assertIsNone(fbsearch.update_notice(self.tmp.name, fetch=gh))
        self.assertEqual(gh.calls, 0)



class UpdateBlocker(unittest.TestCase):
    """When `update` refuses to touch a copy. Uses throwaway local git repos; no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name

        def git(cwd, *argv):
            import subprocess
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(argv), cwd=cwd, check=True,
                           capture_output=True)
        self.git = git
        git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
        git(root, "clone", "-q", "origin.git", "work")
        work = os.path.join(root, "work")
        open(os.path.join(work, "SKILL.md"), "w").write("skill\n")
        git(work, "add", "SKILL.md")
        git(work, "commit", "-q", "-m", "first")
        git(work, "push", "-q", "origin", "HEAD:main")
        git(root, "clone", "-q", "origin.git", "copy")  # an installed copy, on main like a fresh clone
        self.copy = os.path.join(root, "copy")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_clean_clone_on_main_can_update(self):
        self.assertIsNone(fbsearch.update_blocker(self.copy))

    def test_not_a_git_clone(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertIn("not a git clone", fbsearch.update_blocker(plain))

    def test_local_changes(self):
        open(os.path.join(self.copy, "SKILL.md"), "a").write("edited\n")
        self.assertIn("local changes", fbsearch.update_blocker(self.copy))

    def test_a_feature_branch(self):
        self.git(self.copy, "checkout", "-q", "-b", "release-1.4")
        blocker = fbsearch.update_blocker(self.copy)
        self.assertIn("on branch release-1.4, but releases land on main", blocker)
        self.assertIn("checkout main", blocker)


class PluginInstalls(unittest.TestCase):
    """A copy installed through Claude Code's /plugin is Claude Code's to update, not git's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = os.path.join(self.tmp.name, ".claude", "plugins", "cache", "citation-snowball", "1.4.0")
        os.makedirs(os.path.join(self.plugin, ".git"))  # the cache copy may itself be a git clone
        os.environ.pop("FBS_NO_UPDATE_CHECK", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detection(self):
        self.assertTrue(fbsearch.installed_as_plugin(self.plugin))
        self.assertFalse(fbsearch.installed_as_plugin(os.path.join(self.tmp.name, ".agents", "skills", "x")))

    def test_update_points_to_plugin_instead_of_git(self):
        blocker = fbsearch.update_blocker(self.plugin)
        self.assertIn("installed as a Claude Code plugin", blocker)
        self.assertIn("/plugin", blocker)

    def test_the_notice_says_how_plugin_users_update(self):
        script = os.path.join(self.plugin, "scripts", "fbsearch.py")
        notice = fbsearch.update_notice(self.tmp.name, fetch=FakeGitHub("v99.0.0"), script=script)
        self.assertIn("/plugin", notice)
        self.assertNotIn("fbsearch.py update", notice)


if __name__ == "__main__":
    unittest.main()
