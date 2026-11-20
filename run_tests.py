#!/usr/bin/env python3
"""Run deterministic unit/API tests; browser fixtures are explicitly optional."""
import argparse
import sys
import unittest
from pathlib import Path


def individual_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from individual_tests(test)
        else:
            yield test


def main():
    parser = argparse.ArgumentParser(description="Uji StockPilot tanpa menghubungi merchant.")
    parser.add_argument("--browser-fixtures", action="store_true",
                        help="Sertakan tes Chromium pada situs fixture localhost; tidak menghubungi iBox.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    all_tests = list(individual_tests(unittest.defaultTestLoader.discover(str(root / "tests"))))
    selected = [test for test in all_tests if args.browser_fixtures or type(test).__name__ != "BrowserFixtureTests"]
    omitted = len(all_tests) - len(selected)
    if omitted:
        print(f"{omitted} tes browser localhost tidak dijalankan; gunakan --browser-fixtures untuk menyertakannya.", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
