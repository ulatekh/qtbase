#!/usr/bin/env python3


#############################################################################
##
## Copyright (C) 2021 The Qt Company Ltd.
## Contact: https://www.qt.io/licensing/
##
## This file is part of the release tools of the Qt Toolkit.
##
## $QT_BEGIN_LICENSE:GPL-EXCEPT$
## Commercial License Usage
## Licensees holding valid commercial Qt licenses may use this file in
## accordance with the commercial license agreement provided with the
## Software or, alternatively, in accordance with the terms contained in
## a written agreement between you and The Qt Company. For licensing terms
## and conditions see https://www.qt.io/terms-conditions. For further
## information use the contact form at https://www.qt.io/contact-us.
##
## GNU General Public License Usage
## Alternatively, this file may be used under the terms of the GNU
## General Public License version 3 as published by the Free Software
## Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
## included in the packaging of this file. Please review the following
## information to ensure the GNU General Public License requirements will
## be met: https://www.gnu.org/licenses/gpl-3.0.html.
##
## $QT_END_LICENSE$
##
#############################################################################


# !!!IMPORTANT!!!  If you change anything to this script, run the testsuite
#    manually and make sure it still passes, as it doesn't run automatically.
#    Just execute the command line as such:
#
#      ./util/testrunner/tests/tst_testrunner.py -v [--debug]
#
# This script wraps the execution of a Qt test executable, for example
# tst_whatever, and tries to iron out unpredictable test failures.
# In particular:
#
# + Appends output argument to it: "-o tst_whatever.xml,xml"
# + Checks the exit code. If it is zero, the script exits with zero,
#   otherwise proceeds.
# + Reads the XML test log and Understands exactly which function
#   of the test failed.
#   + If no XML file is found or was invalid, the test executable
#     probably CRASHed, so we *re-run the full test once again*.
# + If some testcases failed it executes only those individually
#   until they pass, or until max-repeats times is reached.
#
# The regular way to use is to set the environment variable TESTRUNNER to
# point to this script before invoking ctest.
#
# NOTE: this script is crafted specifically for use with Qt tests and for
#       using it in Qt's CI. For example it detects and acts specially if test
#       executable is "tst_selftests" or "androidtestrunner".  It also detects
#       env var "COIN_CTEST_RESULTSDIR" and uses it as log-dir.
#
# TODO implement --dry-run.

# Exit codes of this script:
#   0: PASS. Either no test failed, or failed initially but passed
#      in the re-runs (FLAKY PASS).
#   1: Some unexpected error of this script.
#   2: FAIL! for at least one test, even after the re-runs.
#   3: CRASH! for the test executable even after re-running it once.



import sys
if sys.version_info < (3, 6):
    sys.stderr.write(
        "Error: this test wrapper script requires Python version 3.6 at least\n")
    sys.exit(1)

import argparse
import subprocess
import os
import traceback
import timeit
import xml.etree.ElementTree as ET
import logging as L

from pprint import pprint
from typing import NamedTuple, Tuple, List, Optional

# Define a custom type for returning a fail incident
class WhatFailed(NamedTuple):
    func: str
    tag: Optional[str] = None


# In the last test re-run, we add special verbosity arguments, in an attempt
# to log more information about the failure
VERBOSE_ARGS = ["-v2", "-maxwarnings", "0"]
VERBOSE_ENV = {
    "QT_LOGGING_RULES": "*=true",
    "QT_MESSAGE_PATTERN": "[%{time process} %{if-debug}D%{endif}%{if-warning}W%{endif}%{if-critical}C%{endif}%{if-fatal}F%{endif}] %{category} %{file}:%{line} %{function}()  -  %{message}",
}


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Wrap Qt test execution. This is intended to be invoked via the TESTRUNNER
environment variable before running ctest in the CI environment. The purpose
of the script is to repeat failed tests in order to iron out transient errors
caused by unpredictable factors. Individual test functions that failed are
retried up to max-repeats times until the test passes.
        """,
        epilog="""
Default flags: --max-repeats 5 --passes-needed 1
        """
    )
    parser.add_argument("testargs", metavar="TESTARGS", nargs="+",
                        help="Test executable and arguments")
    parser.add_argument("--log-dir", metavar="DIR",
                        help="Where to write the XML log files with the test results of the primary test run;"
                        " by default write to CWD")
    parser.add_argument("--max-repeats", type=int, default=5, metavar='N',
                        help="In case the test FAILs, repeat the failed cases this many times")
    parser.add_argument("--passes-needed", type=int, default=1, metavar='M',
                        help="Number of repeats that need to succeed in order to return an overall PASS")
    parser.add_argument("--parse-xml-testlog", metavar="file.xml",
                        help="Do not run the full test the first time, but parse this XML test log;"
                        " if the test log contains failures, then re-run the failed cases normally,"
                        " as indicated by the other flags")
    parser.add_argument("--dry-run", action="store_true",
                        help="(TODO - not implemented yet) Do not run anything, just describe what would happen")
    parser.add_argument("--timeout", metavar="T",
                        help="Timeout for each test execution in seconds")
    parser.add_argument("--no-extra-args", action="store_true",
                        help="Do not append any extra arguments to the test command line, like"
                        " -o log_file.xml -v2 -vs. This will disable some functionality like the"
                        " failed test repetition and the verbose output on failure. This is"
                        " activated by default when TESTARGS is tst_selftests.")
    args = parser.parse_args()
    args.self_name = os.path.basename(sys.argv[0])
    args.specific_extra_args = []

    logging_format = args.self_name + " %(levelname)8s: %(message)s"
    L.basicConfig(format=logging_format, level=L.DEBUG)

    if args.log_dir is None:
        if "COIN_CTEST_RESULTSDIR" in os.environ:
            args.log_dir = os.environ["COIN_CTEST_RESULTSDIR"]
            L.info("Will write XML test logs to directory"
                   " COIN_CTEST_RESULTSDIR=%s", args.log_dir)
        else:
            args.log_dir = "."

    args.test_basename = os.path.basename(args.testargs[0])
    if args.test_basename.endswith(".exe"):
        args.test_basename = args.test_basename[:-4]

    # On Android emulated platforms, "androidtestrunner" is invoked by CMake
    # to wrap the tests.  We have to append the test arguments to it after
    # "--". Besides that we have to detect the basename to avoid saving the
    # XML log as "androidtestrunner.xml" for all tests.
    if args.test_basename == "androidtestrunner":
        args.specific_extra_args = [ "--" ]
        apk_arg = False
        for a in args.testargs[1:]:
            if a == "--apk":
                apk_arg = True
            elif apk_arg:
                apk_arg = False
                if a.endswith(".apk"):
                    args.test_basename = os.path.basename(a)[:-4]
                    break
        L.info("Detected androidtestrunner, test will be handled specially. Detected test basename: %s",
               args.test_basename)

    # The qtestlib selftests are implemented using an external test library
    # (Catch), and they don't support the same command-line options.
    if args.test_basename == "tst_selftests":
        L.info("Detected special test not able to generate XML log! Will not repeat individual testcases.")
        args.no_extra_args = True
        args.max_repeats = 0

    return args


def parse_log(results_file) -> List[WhatFailed]:
    """Parse the XML test log file. Return the failed testcases, if any.

    Failures are considered the "fail" and "xpass" incidents.
    A testcase is a function with an optional data tag."""
    start_timer = timeit.default_timer()

    try:
        tree = ET.parse(results_file)
    except FileNotFoundError:
        L.error("XML log file not found: %s", results_file)
        raise
    except ET.ParseError:
        L.error("Failed to parse the XML log file: %s", results_file)
        with open(results_file, "rb") as f:
            L.error("File Contents:\n%s\n\n", f.read().decode("utf-8", "ignore"))
        raise

    root = tree.getroot()
    if root.tag != "TestCase":
        raise AssertionError(
            f"The XML test log must have <TestCase> as root tag, but has: <{root.tag}>")

    failures = []
    n_passes = 0
    for e1 in root:
        if e1.tag == "TestFunction":
            for e2 in e1:                          # every <TestFunction> can have many <Incident>
                if e2.tag == "Incident":
                    if e2.attrib["type"] in ("fail", "xpass"):
                        func = e1.attrib["name"]
                        e3 = e2.find("DataTag")    # every <Incident> might have a <DataTag>
                        if e3 is not None:
                            failures.append(WhatFailed(func, tag=e3.text))
                        else:
                            failures.append(WhatFailed(func))
                    else:
                        n_passes += 1

    end_timer = timeit.default_timer()
    t = end_timer - start_timer
    L.info(f"Parsed XML file {results_file} in {t:.3f} seconds")
    L.info(f"Found {n_passes} passes and {len(failures)} failures")

    return failures


def run_test(arg_list: List[str], **kwargs):
    L.debug("Running test command line: %s", arg_list)
    proc = subprocess.run(arg_list, **kwargs)
    L.info("Test process exited with code: %d", proc.returncode)

    return proc

# Returns tuple: (exit_code, xml_logfile)
def run_full_test(test_basename, testargs: List[str], output_dir: str,
                  no_extra_args=False, dryrun=False,
                  timeout=None, specific_extra_args=[])  \
        -> Tuple[int, Optional[str]]:

    results_files = []
    output_testargs = []

    # Append arguments to write log to qtestlib XML file,
    # and text to stdout.
    if not no_extra_args:
        results_files.append(os.path.join(output_dir, test_basename + ".xml"))
        output_testargs.extend(["-o", results_files[0] + ",xml"])
        output_testargs.extend(["-o", "-,txt"])

    proc = run_test(testargs + specific_extra_args + output_testargs,
                    timeout=timeout)

    return (proc.returncode, results_files[0] if results_files else None)


def rerun_failed_testcase(testargs: List[str], what_failed: WhatFailed,
                          max_repeats, passes_needed,
                          dryrun=False, timeout=None) -> bool:
    """Run a specific function:tag of a test, until it passes enough times, or
    until max_repeats is reached.

    Return True if it passes eventually, False if it fails.
    """
    assert passes_needed <= max_repeats
    failed_arg = what_failed.func
    if what_failed.tag:
        failed_arg += ":" + what_failed.tag

    n_passes = 0
    for i in range(max_repeats):
        L.info("Re-running testcase: %s", failed_arg)
        if i < max_repeats - 1:
            proc = run_test(testargs + [failed_arg], timeout=timeout)
        else:                                                   # last re-run
            proc = run_test(testargs + VERBOSE_ARGS + [failed_arg],
                            timeout=timeout,
                            env={**os.environ, **VERBOSE_ENV})
        if proc.returncode == 0:
            n_passes += 1
        if n_passes == passes_needed:
            L.info("Test has PASSed as FLAKY after re-runs:%d, passes:%d, failures:%d",
                   i+1, n_passes, i+1-n_passes)
            return True

    assert n_passes <  passes_needed
    assert n_passes <= max_repeats
    n_failures = max_repeats - n_passes
    L.info("Test has FAILed despite all repetitions! re-runs:%d failures:%d",
           max_repeats, n_failures)
    return False


def main():
    args = parse_args()
    n_full_runs = 1 if args.parse_xml_testlog else 2

    for i in range(n_full_runs):
        try:
            if i != 0:
                L.info("Re-running the full test!")
            if args.parse_xml_testlog:
                retcode = 1    # pretend the test returned error
                results_file = args.parse_xml_testlog
            else:
                (retcode, results_file) = \
                    run_full_test(args.test_basename, args.testargs, args.log_dir,
                                  args.no_extra_args, args.dry_run, args.timeout,
                                  args.specific_extra_args)
                if retcode == 0:
                    sys.exit(0)    # PASS

            failed_functions = parse_log(results_file)

            if not args.parse_xml_testlog:
                assert len(failed_functions) > 0, \
                    "The XML test log should contain at least one failure!" \
                    " Did the test CRASH right after all its testcases PASSed?"

            break    # go to re-running individual failed testcases

        except Exception as e:
            L.exception("The test executable CRASHed uncontrollably!"
                        " Details about where we caught the problem:",
                        exc_info=e)
            if i < n_full_runs - 1:
                L.info("Will re-run the full test executable")
            else:    # Failed on the final run
                L.error("Full test run failed repeatedly, aborting!")
                sys.exit(3)

    if args.max_repeats == 0:
        sys.exit(2)          # Some tests failed but no re-runs were asked

    L.info("Some tests failed, will re-run at most %d times.\n",
           args.max_repeats)

    for what_failed in failed_functions:
        try:
            ret = rerun_failed_testcase(args.testargs, what_failed,
                                        args.max_repeats, args.passes_needed,
                                        dryrun=args.dry_run, timeout=args.timeout)
        except Exception as e:
            L.exception("The test executable CRASHed uncontrollably!"
                        " Details about where we caught the problem:",
                        exc_info=e)
            L.error("Test re-run exited unxpectedly, aborting!")
            sys.exit(3)                                    # Test re-run CRASH

        if not ret:
            sys.exit(2)                                    # Test re-run FAIL

    sys.exit(0)                                  # All testcase re-runs PASSed


if __name__ == "__main__":
    main()