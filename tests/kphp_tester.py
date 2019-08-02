#!/usr/bin/python3
import argparse
import copy
import glob
import math
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
from functools import partial
from multiprocessing.dummy import Pool as ThreadPool


def red(text):
    return "\033[31m{}\033[0m".format(text)


def green(text):
    return "\033[32m{}\033[0m".format(text)


def yellow(text):
    return "\033[33m{}\033[0m".format(text)


def blue(text):
    return "\033[1;34m{}\033[0m".format(text)


class TestFile:
    def __init__(self, file_path, test_tmp_dir, tags, out_regex):
        self.test_tmp_dir = test_tmp_dir
        self.file_path = file_path
        self.tags = tags
        self.out_regex = out_regex

    def is_ok(self):
        return "ok" in self.tags

    def is_kphp_should_fail(self):
        return "kphp_should_fail" in self.tags

    def is_php5(self):
        return "php5" in self.tags


class TestArtifacts:
    class Artifact:
        def __init__(self):
            self.file = None
            self.priority = 0

    def __init__(self):
        self.php_stderr = TestArtifacts.Artifact()
        self.kphp_build_stderr = TestArtifacts.Artifact()
        self.kphp_build_asan_log = TestArtifacts.Artifact()
        self.kphp_runtime_stderr = TestArtifacts.Artifact()
        self.kphp_runtime_asan_log = TestArtifacts.Artifact()
        self.php_and_kphp_stdout_diff = TestArtifacts.Artifact()

    def get_all(self):
        result = []
        if self.php_stderr.file:
            result.append(("php stderr", self.php_stderr))
        if self.kphp_build_stderr.file:
            result.append(("kphp build stderr", self.kphp_build_stderr))
        if self.kphp_build_asan_log.file:
            result.append(("kphp build asan log", self.kphp_build_asan_log))
        if self.kphp_runtime_stderr.file:
            result.append(("kphp runtime stderr", self.kphp_runtime_stderr))
        if self.kphp_runtime_asan_log.file:
            result.append(("kphp runtime asan log", self.kphp_runtime_asan_log))
        if self.php_and_kphp_stdout_diff.file:
            result.append(("php and kphp stdout diff", self.php_and_kphp_stdout_diff))

        return sorted(result, key=lambda x: x[1].priority, reverse=True)

    def empty(self):
        return self.php_stderr.file is None and self.kphp_build_stderr.file is None and \
               self.kphp_build_asan_log.file is None and self.kphp_runtime_stderr.file is None and \
               self.kphp_runtime_asan_log.file is None and self.php_and_kphp_stdout_diff.file is None


class TestRunner:
    def __init__(self, test_file, kphp_path, tests_dir):
        self._test_file = test_file

        self.artifacts = TestArtifacts()
        self._artifacts_dir = os.path.join(self._test_file.test_tmp_dir, "artifacts")
        self._php_stdout = None
        self._kphp_server_stdout = None

        self._kphp_path = os.path.abspath(kphp_path)
        self._test_file_path = os.path.abspath(self._test_file.file_path)
        self._working_dir = os.path.abspath(os.path.join(self._test_file.test_tmp_dir, "working_dir"))
        self._include_dirs = (os.path.abspath(tests_dir), os.path.dirname(self._test_file_path))
        self._php_tmp_dir = os.path.join(self._working_dir, "php")
        self._kphp_build_tmp_dir = os.path.join(self._working_dir, "kphp_build")
        self._kphp_runtime_tmp_dir = os.path.join(self._working_dir, "kphp_runtime")
        self._kphp_runtime_bin = os.path.join(self._kphp_build_tmp_dir, "server")

    def _create_artifacts_dir(self):
        os.makedirs(self._artifacts_dir, exist_ok=True)

    def _move_to_artifacts(self, artifact_name, proc, content=None, file=None):
        self._create_artifacts_dir()
        artifact = getattr(self.artifacts, artifact_name)
        artifact.file = os.path.join(self._artifacts_dir, artifact_name)
        artifact.priority = proc.returncode
        if content:
            with open(artifact.file, 'wb') as f:
                f.write(content)
        if file:
            shutil.move(file, artifact.file)

    @staticmethod
    def _wait_proc(proc, timeout=300):
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None, b"Zombie detected?! Proc can't be killed due timeout!"

            stderr = (stderr or b"") + b"\n\nKilled due timeout\n"
        return stdout, stderr

    @staticmethod
    def _clear_working_dir(dir_path):
        if os.path.exists(dir_path):
            bad_paths = []
            shutil.rmtree(dir_path, onerror=lambda f, path, e: bad_paths.append(path))

            # some php tests changes permissions
            for bad_path in reversed(bad_paths):
                os.chmod(bad_path, 0o777)
            if bad_paths:
                shutil.rmtree(dir_path)
        os.makedirs(dir_path)

    def remove_artifacts_dir(self):
        shutil.rmtree(self._artifacts_dir, ignore_errors=True)

    def remove_kphp_runtime_bin(self):
        os.remove(self._kphp_runtime_bin)

    def run_with_php(self):
        self._clear_working_dir(self._php_tmp_dir)

        if self._test_file.is_php5():
            php_bin = shutil.which("php5.6") or shutil.which("php5")
        else:
            php_bin = shutil.which("php7.2")

        if php_bin is None:
            raise RuntimeError("Can't find php executable")

        options = [
            ("display_errors", 0),
            ("log_errors", 1),
            ("error_log", "/proc/self/fd/2"),
            ("extension", "json.so"),
            ("extension", "bcmath.so"),
            ("extension", "iconv.so"),
            ("extension", "mbstring.so"),
            ("extension", "vkext.so"),
            ("memory_limit", "3072M"),
            ("xdebug.var_display_max_depth", -1),
            ("xdebug.var_display_max_children", -1),
            ("xdebug.var_display_max_data", -1),
            ("include_path", "{}:{}".format(*self._include_dirs))
        ]

        cmd = [php_bin, "-n"]
        for k, v in options:
            cmd.append("-d {}='{}'".format(k, v))
        cmd.append(self._test_file_path)
        php_proc = subprocess.Popen(cmd, cwd=self._php_tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._php_stdout, php_stderr = self._wait_proc(php_proc)

        if php_stderr:
            self._move_to_artifacts("php_stderr", php_proc, content=php_stderr)

        if not os.listdir(self._php_tmp_dir):
            shutil.rmtree(self._php_tmp_dir, ignore_errors=True)

        return php_proc.returncode == 0

    @staticmethod
    def _can_be_ignored(ignore_patterns, binary_text):
        if not binary_text:
            return True

        for line in binary_text.split(b'\n'):
            if not line:
                continue
            is_line_ok = False
            for pattern in ignore_patterns:
                if re.fullmatch(pattern, line.decode()):
                    is_line_ok = True
                    break

            if not is_line_ok:
                return False
        return True

    @staticmethod
    def _prepare_asan_env(working_directory, asan_log_name):
        tmp_asan_file = os.path.join(working_directory, asan_log_name)
        asan_glob_mask = "{}.*".format(tmp_asan_file)
        for old_asan_file in glob.glob(asan_glob_mask):
            os.remove(old_asan_file)

        env = copy.copy(os.environ)
        env["ASAN_OPTIONS"] = "detect_leaks=0:log_path={}".format(tmp_asan_file)
        return env, asan_glob_mask

    @staticmethod
    def _can_ignore_kphp_asan_log(asan_log_file):
        with open(asan_log_file, 'rb') as f:
            ignore_asan = TestRunner._can_be_ignored(
                ignore_patterns=[
                    "^==\\d+==WARNING: ASan doesn't fully support makecontext/swapcontext functions and may produce false positives in some cases\\!$",
                    "^==\\d+==WARNING: ASan is ignoring requested __asan_handle_no_return: stack top.+$",
                    "^False positive error reports may follow$",
                    "^For details see .+$"
                ],
                binary_text=f.read())

        if ignore_asan:
            os.remove(asan_log_file)

        return ignore_asan

    def _move_asan_logs_to_artifacts(self, asan_glob_mask, proc, asan_log_name):
        for asan_log in glob.glob(asan_glob_mask):
            if not self._can_ignore_kphp_asan_log(asan_log):
                self._move_to_artifacts(asan_log_name, proc, file=asan_log)
                return

    def compile_with_kphp(self):
        os.makedirs(self._kphp_build_tmp_dir, exist_ok=True)

        asan_log_name = "kphp_build_asan_log"
        env, asan_glob_mask = self._prepare_asan_env(self._kphp_build_tmp_dir, asan_log_name)
        env["KPHP_JOBS_COUNT"] = "2"
        env["KPHP_THREADS_COUNT"] = "3"

        include = " ".join("-I {}".format(include_dir) for include_dir in self._include_dirs)
        cmd = [self._kphp_path, include, "-d", os.path.abspath(self._kphp_build_tmp_dir), self._test_file_path]
        # TODO kphp writes error into stdout and info into stderr
        kphp_compilation_proc = subprocess.Popen(cmd, cwd=self._kphp_build_tmp_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        kphp_build_stderr, fake_stderr = self._wait_proc(kphp_compilation_proc, timeout=600)
        if fake_stderr:
            kphp_build_stderr = kphp_build_stderr + fake_stderr

        self._move_asan_logs_to_artifacts(asan_glob_mask, kphp_compilation_proc, asan_log_name)
        ignore_stderr = self._can_be_ignored(
            ignore_patterns=[
                "^Starting php to cpp transpiling\\.\\.\\.$",
                "^Starting make\\.\\.\\.$",
                "^objs cnt = \\d+$",
                "^\\s*\\d+\\% \\[total jobs \\d+\\] \\[left jobs \\d+\\] \\[running jobs \\d+\\] \\[waiting jobs \\d+\\]$"
            ],
            binary_text=kphp_build_stderr)
        if not ignore_stderr:
            self._move_to_artifacts("kphp_build_stderr", kphp_compilation_proc, content=kphp_build_stderr)

        return kphp_compilation_proc.returncode == 0

    def run_with_kphp(self):
        self._clear_working_dir(self._kphp_runtime_tmp_dir)

        asan_log_name = "kphp_runtime_asan_log"
        env, asan_glob_mask = self._prepare_asan_env(self._kphp_runtime_tmp_dir, asan_log_name)

        cmd = [self._kphp_runtime_bin, "-o"]
        if not os.getuid():
            cmd += ["-u", "root", "-g", "root"]
        kphp_server_proc = subprocess.Popen(cmd,
                                            cwd=self._kphp_runtime_tmp_dir,
                                            env=env,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
        self._kphp_server_stdout, kphp_runtime_stderr = self._wait_proc(kphp_server_proc)

        self._move_asan_logs_to_artifacts(asan_glob_mask, kphp_server_proc, asan_log_name)
        ignore_stderr = self._can_be_ignored(
            ignore_patterns=[
                "^\\[\\d{4}\\-\\d{2}\\-\\d{2} \\d{2}:\\d{2}:\\d{2}\\.\\d+ PHP/php\\-runner\\.cpp\\s+\\d+\\].+$"
            ],
            binary_text=kphp_runtime_stderr)

        if not ignore_stderr:
            self._move_to_artifacts("kphp_runtime_stderr", kphp_server_proc, content=kphp_runtime_stderr)

        if not os.listdir(self._kphp_runtime_tmp_dir):
            shutil.rmtree(self._kphp_runtime_tmp_dir, ignore_errors=True)

        return kphp_server_proc.returncode == 0

    def compare_php_and_kphp_stdout(self):
        if self._kphp_server_stdout == self._php_stdout:
            return True

        self._create_artifacts_dir()
        php_stdout_file = os.path.join(self._artifacts_dir, "php_stdout")
        with open(php_stdout_file, 'wb') as f:
            f.write(self._php_stdout)
        kphp_server_stdout_file = os.path.join(self._artifacts_dir, "kphp_server_stdout")
        with open(kphp_server_stdout_file, 'wb') as f:
            f.write(self._kphp_server_stdout)
        self.artifacts.php_and_kphp_stdout_diff.file = os.path.join(self._artifacts_dir, "php_vs_kphp.diff")

        with open(self.artifacts.php_and_kphp_stdout_diff.file, 'wb') as f:
            subprocess.call(["diff", "--text", "-ud", php_stdout_file, kphp_server_stdout_file], stdout=f)
        self.artifacts.php_and_kphp_stdout_diff.priority = 1

        return False


def make_test_file(file_path, test_tmp_dir, test_tags):
    # if file doesn't exist it will fail late
    if file_path.endswith(".phpt") or not os.path.exists(file_path):
        test_acceptable = True
        for test_tag in test_tags:
            test_acceptable = file_path.find(test_tag) != -1
            if test_acceptable:
                break
        if not test_acceptable:
            return None

        return TestFile(file_path, test_tmp_dir, ["ok"], None)

    with open(file_path, 'rb') as f:
        first_line = f.readline().decode('utf-8')
        if not first_line.startswith("@"):
            return None

        tags = first_line[1:].split()
        test_acceptable = True
        for test_tag in test_tags:
            test_acceptable = (test_tag in tags) or (file_path.find(test_tag) != -1)
            if test_acceptable:
                break

        if not test_acceptable:
            return None
        out_regex = None
        second_line = f.readline().decode('utf-8').strip()
        if len(second_line) > 1 and second_line.startswith("/") and second_line.endswith("/"):
            out_regex = re.compile(second_line[1:-1])

        return TestFile(file_path, test_tmp_dir, tags, out_regex)


def test_files_from_dir(tests_dir):
    for root, _, files in os.walk(tests_dir):
        for file in files:
            yield root, file


def test_files_from_list(tests_dir, test_list):
    with open(test_list) as f:
        for line in f.readlines():
            yield tests_dir, line.strip()


def collect_tests(tests_dir, test_tags, test_list):
    tests = []
    tmp_dir = "{}_tmp".format(__file__[:-3])
    file_it = test_files_from_list(tests_dir, test_list) if test_list else test_files_from_dir(tests_dir)
    for root, file in file_it:
        if file.endswith(".php") or file.endswith(".phpt"):
            test_file_path = os.path.join(root, file)
            test_tmp_dir = os.path.join(tmp_dir, os.path.relpath(test_file_path, os.path.dirname(tests_dir)))
            test_tmp_dir = test_tmp_dir[:-4] if test_tmp_dir.endswith(".php") else test_tmp_dir[:-5]
            test_file = make_test_file(test_file_path, test_tmp_dir, test_tags)
            if test_file:
                tests.append(test_file)
    return tests


class TestResult:
    @staticmethod
    def failed(test_file, artifacts, failed_stage):
        return TestResult(red("failed "), test_file, artifacts, failed_stage)

    @staticmethod
    def passed(test_file, artifacts):
        return TestResult(green("passed "), test_file, artifacts, None)

    @staticmethod
    def skipped(test_file):
        return TestResult(yellow("skipped"), test_file, None, None)

    def __init__(self, status, test_file, artifacts, failed_stage):
        self.status = status
        self.test_file_path = test_file.file_path
        self.artifacts = artifacts
        self.failed_stage_msg = None
        if failed_stage:
            self.failed_stage_msg = red("({})".format(failed_stage))

    def _print_artifacts(self):
        if self.artifacts:
            for file_type, artifact in self.artifacts.get_all():
                file_type_colored = red(file_type) if artifact.priority else yellow(file_type)
                print("  {} - {}".format(blue(artifact.file), file_type_colored), flush=True)

    def print_short_report(self, total_tests, test_number):
        width = 1 + int(math.log10(total_tests))
        completed_str = "{0: >{width}}".format(test_number, width=width)
        additional_info = ""
        if self.failed_stage_msg:
            additional_info = self.failed_stage_msg
        elif self.artifacts:
            stderr_names = ", ".join(file_type for file_type, _ in self.artifacts.get_all())
            if stderr_names:
                additional_info = yellow("(got {})".format(stderr_names))

        print("[{test_number}/{total_tests}] {status} {test_file} {additional_info}".format(
            test_number=completed_str,
            total_tests=total_tests,
            status=self.status,
            test_file=self.test_file_path,
            additional_info=additional_info), flush=True)

        self._print_artifacts()

    def print_fail_report(self):
        if self.failed_stage_msg:
            print("{} {}".format(self.test_file_path, self.failed_stage_msg), flush=True)
            self._print_artifacts()

    def is_skipped(self):
        return self.artifacts is None

    def is_failed(self):
        return self.failed_stage_msg is not None


def run_test(kphp_path, tests_dir, test):
    if not os.path.exists(test.file_path):
        return TestResult.failed(test, None, "can't find test file")

    runner = TestRunner(test, kphp_path, tests_dir)
    runner.remove_artifacts_dir()

    if test.is_kphp_should_fail():
        if runner.compile_with_kphp():
            return TestResult.failed(test, runner.artifacts, "kphp build is ok, but it expected to fail")

        if test.out_regex:
            if not runner.artifacts.kphp_build_stderr.file:
                return TestResult.failed(test, runner.artifacts, "kphp build failed without stderr")

            with open(runner.artifacts.kphp_build_stderr.file) as f:
                if not test.out_regex.search(f.read()):
                    return TestResult.failed(test, runner.artifacts, "unexpected kphp build fail")

        runner.artifacts.kphp_build_stderr.file = None
        return TestResult.passed(test, runner.artifacts)

    if test.is_ok():
        if not runner.run_with_php():
            return TestResult.failed(test, runner.artifacts, "got php error")
        if not runner.compile_with_kphp():
            return TestResult.failed(test, runner.artifacts, "got kphp build error")
        if not runner.run_with_kphp():
            return TestResult.failed(test, runner.artifacts, "got kphp runtime error")
        if not runner.compare_php_and_kphp_stdout():
            return TestResult.failed(test, runner.artifacts, "got php and kphp diff")

        if runner.artifacts.empty():
            runner.remove_kphp_runtime_bin()

        return TestResult.passed(test, runner.artifacts)

    return TestResult.skipped(test)


def main(tests_dir, kphp_path, jobs, test_tags, no_report, passed_list, test_list):
    hack_reference_exit = []
    signal.signal(signal.SIGINT, lambda sig, frame: hack_reference_exit.append(1))

    tests = collect_tests(tests_dir, test_tags, test_list)
    if not tests:
        print("Can't find any tests with [{}] {}".format(
            ", ".join(test_tags),
            "tag" if len(test_tags) == 1 else "tags"))
        sys.exit(1)

    results = []
    with ThreadPool(jobs) as pool:
        tests_completed = 0
        for test_result in pool.imap_unordered(partial(run_test, kphp_path, tests_dir), tests):
            if hack_reference_exit:
                print(yellow("Testing process was interrupted"), flush=True)
                break
            tests_completed = tests_completed + 1
            test_result.print_short_report(len(tests), tests_completed)
            results.append(test_result)

    print("\nTesting results:", flush=True)

    skipped = len(tests) - len(results)
    failed = 0
    passed = []
    for test_result in results:
        if test_result.is_skipped():
            skipped = skipped + 1
        elif test_result.is_failed():
            failed = failed + 1
        else:
            passed.append(os.path.relpath(test_result.test_file_path, tests_dir))

    if passed:
        print("  {}{}".format(green("passed:  "), len(passed)))
        if passed_list:
            with open(passed_list, "w") as f:
                passed.sort()
                f.writelines("{}\n".format(l) for l in passed)
    if skipped:
        print("  {}{}".format(yellow("skipped: "), skipped))
    if failed:
        print("  {}{}\n".format(red("failed:  "), failed))
        if not no_report:
            for test_result in results:
                test_result.print_fail_report()

    sys.exit(1 if failed else len(hack_reference_exit))


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "-j",
        type=int,
        dest="jobs",
        default=(multiprocessing.cpu_count() // 2) or 1,
        help="number of parallel jobs")

    this_dir = os.path.dirname(__file__)
    parser.add_argument(
        "-d",
        type=str,
        dest="tests_dir",
        default=os.path.join(this_dir, "phpt"),
        help="tests dir")

    parser.add_argument(
        "--kphp",
        type=str,
        dest="kphp_path",
        default=os.path.normpath(os.path.join(this_dir, os.path.pardir, "kphp.sh")),
        help="path to kphp")

    parser.add_argument(
        'test_tags',
        metavar='TAG',
        type=str,
        nargs='*',
        help='test tag or directory or file')

    parser.add_argument(
        "--no-report",
        action='store_true',
        dest="no_report",
        default=False,
        help="do not show full report")

    parser.add_argument(
        '--save-passed',
        metavar='FILE',
        type=str,
        dest="passed_list",
        default=None,
        help='save passed tests in separate file')

    parser.add_argument(
        '--from-list',
        metavar='FILE',
        type=str,
        dest="test_list",
        default=None,
        help='run tests from list')

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not os.path.exists(args.tests_dir):
        print("Can't find tests dir '{}'".format(args.test_list))
        sys.exit(1)

    if not os.path.exists(args.kphp_path):
        print("Can't find kphp '{}'".format(args.kphp_path))
        sys.exit(1)

    if args.test_list and not os.path.exists(args.test_list):
        print("Can't find test list file '{}'".format(args.test_list))
        sys.exit(1)

    main(tests_dir=os.path.normpath(args.tests_dir),
         kphp_path=os.path.normpath(args.kphp_path),
         jobs=args.jobs,
         test_tags=args.test_tags,
         no_report=args.no_report,
         passed_list=args.passed_list,
         test_list=args.test_list)