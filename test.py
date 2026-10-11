"""Single-command full test runner for the Lost and Found system.

Run from this project folder with: python test.py
The process exits with code 0 only when every discovered test passes.
"""
import sys
import unittest

import tests


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"\nTEST SUMMARY: {result.testsRun} run | {len(result.failures)} failed | {len(result.errors)} errors")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
