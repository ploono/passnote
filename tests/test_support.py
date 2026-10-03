import os
import subprocess
import unittest

from support import HomeCase


class LeakCheckTest(unittest.TestCase):
    def test_home_case_fails_a_test_that_leaks_a_file(self):
        # -W error alone doesn't fail a test on a leaked file: CPython reports the
        # ResourceWarning raised in __del__ as "Exception ignored" and the run still passes.
        class Leaky(HomeCase):
            def test_leak(self):
                open(__file__, "rb").read()  # deliberately never closed

        result = unittest.TestResult()
        Leaky("test_leak").run(result)
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn("ResourceWarning", result.failures[0][1])

    def test_home_case_passes_a_test_that_closes_its_files(self):
        class Tidy(HomeCase):
            def test_tidy(self):
                with open(__file__, "rb") as fh:
                    fh.read()

        result = unittest.TestResult()
        Tidy("test_tidy").run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)


class GitIsolationTest(HomeCase):
    def test_git_ignores_user_and_system_config(self):
        self.assertEqual(os.environ.get("GIT_CONFIG_GLOBAL"), os.devnull)
        self.assertEqual(os.environ.get("GIT_CONFIG_NOSYSTEM"), "1")
        # git < 2.32 ignores GIT_CONFIG_GLOBAL and reads $HOME/.gitconfig and $XDG_CONFIG_HOME/git/config.
        for var in ("HOME", "XDG_CONFIG_HOME"):
            self.assertTrue(os.environ[var].startswith(self.tmp + os.sep), var)
        out = subprocess.run(["git", "config", "--global", "--list"], capture_output=True, text=True)
        self.assertEqual(out.stdout, "")


if __name__ == "__main__":
    unittest.main()
