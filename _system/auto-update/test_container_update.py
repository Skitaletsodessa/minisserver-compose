#!/usr/bin/env python3
"""Task 35 C2: unit tests of the pure parts of container-update.py (version choice, ref handling, compose rewrite, quiet hours). Run: python3 test_container_update.py"""
import importlib.util
import os
import sys
import unittest
from datetime import datetime

spec = importlib.util.spec_from_file_location("cu", os.path.join(os.path.dirname(os.path.abspath(__file__)), "container-update.py"))
cu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cu)

COMPOSE = """services:
  app:
    image: ghcr.io/x/app:1.2.3@sha256:aaaa
    restart: unless-stopped
  db:
    image: docker.io/valkey/valkey:9@sha256:bbbb
"""


class Pick(unittest.TestCase):
    def test_patch_takes_the_highest_patch_of_the_same_minor(self):
        self.assertEqual(cu.pick_candidate("1.2.3", ["1.2.2", "1.2.4", "1.2.10", "1.3.0", "2.0.0"], "patch"), "1.2.10")

    def test_patch_never_crosses_the_minor(self):
        self.assertIsNone(cu.pick_candidate("1.2.3", ["1.3.0", "1.3.1"], "patch"))

    def test_minor_policy_crosses_the_minor_but_not_the_major(self):
        self.assertEqual(cu.pick_candidate("v3.3.5", ["v3.3.6", "v3.4.0", "v3.4.2", "v4.0.0"], "minor"), "v3.4.2")

    def test_prefix_and_suffix_must_match(self):
        self.assertEqual(cu.pick_candidate("v0.9.3-omnibus", ["v0.9.4", "v0.9.4-omnibus", "v0.9.5-rc1-omnibus", "0.9.9-omnibus"], "patch"), "v0.9.4-omnibus")

    def test_prereleases_are_never_chosen(self):
        self.assertIsNone(cu.pick_candidate("1.2.3", ["1.2.4-rc1", "1.2.4-beta", "1.2.4-ls1"], "patch"))

    def test_not_a_version_gives_none(self):
        self.assertIsNone(cu.pick_candidate("latest", ["1.2.3"], "patch"))
        self.assertIsNone(cu.pick_candidate("12.4", ["12.4.1"], "patch"))        # a floating major.minor tag uses the digest policy instead

    def test_nothing_newer(self):
        self.assertIsNone(cu.pick_candidate("1.2.3", ["1.2.3", "1.2.2", "1.1.9"], "patch"))

    def test_calendar_versions(self):
        self.assertEqual(cu.pick_candidate("2026.10.7", ["2026.10.8", "2026.11.0", "2026.10.9"], "patch"), "2026.10.9")


class Refs(unittest.TestCase):
    def test_split_and_build(self):
        self.assertEqual(cu.split_ref("ghcr.io/x/app:1.2.3@sha256:aaaa"), ("ghcr.io/x/app", "1.2.3", "sha256:aaaa"))
        self.assertEqual(cu.split_ref("ghcr.io/home-assistant/home-assistant@sha256:cc"), ("ghcr.io/home-assistant/home-assistant", None, "sha256:cc"))
        self.assertEqual(cu.split_ref("deluan/navidrome:0.64.2"), ("deluan/navidrome", "0.64.2", None))
        self.assertEqual(cu.split_ref("127.0.0.1:5057/test/app:1.0.0"), ("127.0.0.1:5057/test/app", "1.0.0", None))
        self.assertEqual(cu.build_ref("a/b", "1", "sha256:z"), "a/b:1@sha256:z")

    def test_rewrite_changes_exactly_the_one_line(self):
        out = cu.rewrite_compose(COMPOSE, "ghcr.io/x/app", "ghcr.io/x/app:1.2.4@sha256:dddd")
        self.assertIn("image: ghcr.io/x/app:1.2.4@sha256:dddd\n", out)
        self.assertEqual(out.replace("1.2.4@sha256:dddd", "1.2.3@sha256:aaaa"), COMPOSE)

    def test_docker_io_prefix_matches_the_bare_name(self):
        out = cu.rewrite_compose(COMPOSE, "valkey/valkey", "docker.io/valkey/valkey:9@sha256:eeee")
        self.assertIn("docker.io/valkey/valkey:9@sha256:eeee", out)

    def test_ambiguous_or_missing_lines_are_refused(self):
        with self.assertRaises(ValueError):
            cu.rewrite_compose(COMPOSE, "ghcr.io/x/other", "x")
        with self.assertRaises(ValueError):
            cu.rewrite_compose(COMPOSE + "  again:\n    image: ghcr.io/x/app:1.2.3\n", "ghcr.io/x/app", "x")


class Window(unittest.TestCase):
    def test_quiet_hours(self):
        self.assertTrue(cu.quiet_hours(datetime(2026, 11, 15, 21, 0)))
        self.assertTrue(cu.quiet_hours(datetime(2026, 11, 15, 3, 0)))
        self.assertTrue(cu.quiet_hours(datetime(2026, 11, 15, 7, 59)))
        self.assertFalse(cu.quiet_hours(datetime(2026, 11, 15, 11, 0)))
        self.assertFalse(cu.quiet_hours(datetime(2026, 11, 15, 20, 59)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
