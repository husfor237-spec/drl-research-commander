#!/usr/bin/env python3
"""Run the skill's test suite and refuse an empty or trivial run.

Guards against a CI that reports success while executing nothing: discovery
breakage after a file rename, or a wholesale skip caused by an import error in
one test module. Skips are legitimate (the reader-dependent tests skip when the
sibling `context-gauge` skill is absent), so the floor is on *executed* tests.
"""

import argparse
from pathlib import Path
import sys
import unittest


HERE = Path(__file__).resolve().parent
# Low-water marks, deliberately below the current counts (116 total, 80 executed
# without the reader, 116 executed with it). Raise them when tests are added.
MIN_TOTAL = 110
MIN_EXECUTED = 75


def build_suite():
    loader = unittest.TestLoader()
    return loader.discover(str(HERE), top_level_dir=str(HERE))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Per-test output")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError):
        pass
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    try:
        suite = build_suite()
    except Exception as exc:  # discovery can fail on an unimportable layout
        print("guarded failure: test discovery failed: " + repr(exc))
        return 1
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("\ncollected=%d executed=%d skipped=%d failures=%d errors=%d"
          % (result.testsRun, executed, skipped, len(result.failures), len(result.errors)))
    problems = []
    if not result.wasSuccessful():
        problems.append("the suite reported failures or errors")
    if result.testsRun < MIN_TOTAL:
        problems.append("collected %d tests, below the floor of %d" % (result.testsRun, MIN_TOTAL))
    if executed < MIN_EXECUTED:
        problems.append("executed %d tests, below the floor of %d" % (executed, MIN_EXECUTED))
    for problem in problems:
        print("guarded failure: " + problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
