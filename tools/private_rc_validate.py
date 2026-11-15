#!/usr/bin/env python3
"""Record isolated existing unittest suites against exact tracked source bytes.

Run in Cloud. No application, RF or device startup. Logs must be outside repo.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args]).decode().strip()


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in git('ls-files').splitlines() if (ROOT / name).is_file()}


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = {}

    def startTest(self, test):
        self.cases[test.id()] = dict(status='RUNNING')
        super().startTest(test)

    def addSuccess(self, test):
        self.cases[test.id()] = dict(status='PASS')
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.cases[test.id()] = dict(status='FAIL')
        super().addFailure(test, err)

    def addError(self, test, err):
        self.cases[test.id()] = dict(status='ERROR')
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.cases[test.id()] = dict(status='SKIP', reason=reason)
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.cases[test.id()] = dict(status='FAIL' if issubclass(err[0], test.failureException) else 'ERROR')
        super().addSubTest(test, subtest, err)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', choices=['main', 'receiver', 'piglet'])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    if out == ROOT or ROOT in out.parents:
        parser.error('Output must be outside the checkout')
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    before = hashes()
    loader = unittest.TestLoader()
    if args.suite == 'main':
        suite = loader.discover(str(ROOT / 'tests'))
    elif args.suite == 'receiver':
        suite = loader.loadTestsFromNames(['test_meshtastic_rx_probe', 'test_spi_mutation_regression'])
    else:
        suite = loader.discover(str(ROOT / 'firmware/piglet-collection/tests'))
    start = time.monotonic()
    with (out / (args.suite + '.log')).open('w') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Result).run(suite)
    counts = {name: sum(v['status'] == name for v in result.cases.values())
              for name in ['PASS', 'FAIL', 'ERROR', 'SKIP', 'RUNNING']}
    unchanged = before == hashes()
    summary = dict(suite=args.suite, source_commit=git('rev-parse', 'HEAD'),
                   source_tree=git('rev-parse', 'HEAD^{tree}'),
                   system=platform.platform(), machine=platform.machine(), python=sys.version,
                   uid=os.getuid() if hasattr(os, 'getuid') else None,
                   command=sys.argv, elapsed_seconds=time.monotonic()-start,
                   tests_run=result.testsRun, counts=counts, cases=result.cases,
                   source_unchanged=unchanged, tracked_sha256=before)
    (out / (args.suite + '.json')).write_text(json.dumps(summary, indent=2))
    success = result.wasSuccessful() and unchanged and not counts['RUNNING']
    (out / (args.suite + '.exit')).write_text(str(0 if success else 1)+'\n')
    print(json.dumps({k: v for k, v in summary.items() if k not in ['cases', 'tracked_sha256']}, indent=2))
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
