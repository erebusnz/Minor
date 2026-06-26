#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
#
# This file is a MODIFIED derivative work of Morris by Marc Brooker
# (https://github.com/marcbrooker/morris), licensed under the Apache License,
# Version 2.0. Changes from the original: ported from Rust to Python; retargeted
# from cargo / `cargo test` to C projects using CMake + CTest (Unity); the AWS
# Bedrock client was replaced by `claude` CLI and Anthropic API backends.
# See the LICENSE and NOTICE files for details.
"""Morris Minor - AI-Powered Mutation Testing for C firmware (Morris embedded C port).

A Python port of Marc Brooker's Morris (https://github.com/marcbrooker/morris),
adapted from Rust/cargo to embedded C projects that expose a host-side
CMake + CTest unit suite (e.g. Unity).

Like the original, Morris Minor follows a fixed, deterministic workflow. The AI (Claude)
is consulted exactly twice: once to propose strategic single-line mutations, and
once to analyse the survivors. Everything else -- file discovery, building,
running tests, applying and restoring mutations -- is plain deterministic code.

Two backends are supported for the AI calls:
  * ``cli`` -- shells out to the local ``claude`` CLI in print mode (no API key).
  * ``api`` -- uses the ``anthropic`` Python SDK and ``ANTHROPIC_API_KEY``.

The C workflow differs from Rust in one structural way: build and test are
separate commands, so a mutation that fails to compile is a BUILD ERROR, a
mutation that compiles but makes a test fail is KILLED, and one that compiles and
leaves every test passing has SURVIVED.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Mutation:
    """A single mutation proposed by the AI."""

    file_path: str
    line_number: int
    original_line: str
    mutated_line: str
    description: str

    @classmethod
    def from_dict(cls, d: dict) -> "Mutation":
        return cls(
            file_path=str(d["file_path"]),
            line_number=int(d["line_number"]),
            original_line=str(d["original_line"]),
            mutated_line=str(d["mutated_line"]),
            description=str(d.get("description", "")),
        )


class Outcome(Enum):
    """Possible outcomes of a mutation test."""

    SURVIVED = "SURVIVED"
    KILLED = "KILLED"
    TIMEOUT = "TIMEOUT"
    BUILD_ERROR = "BUILD ERROR"
    LINE_MISMATCH = "LINE MISMATCH"


@dataclass
class MutationResult:
    mutation: Mutation
    outcome: Outcome
    detail: str = ""


# --------------------------------------------------------------------------- #
# Source discovery
# --------------------------------------------------------------------------- #


def _is_under(path: Path, parent: Path) -> bool:
    """True if ``path`` is inside ``parent`` (both resolved)."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def discover_from_compile_db(
    build_dir: Path, source_root: Path, test_dir: Path
) -> list[Path]:
    """Discover mutation targets from ``compile_commands.json``.

    Returns the ``.c`` files compiled into the test build that live under
    ``source_root`` (the firmware's own code, e.g. ``Core/``) but NOT under
    ``test_dir`` -- i.e. the modules under test, excluding the test harness,
    Unity, and stubs.
    """
    db_path = build_dir / "compile_commands.json"
    if not db_path.exists():
        return []
    try:
        entries = json.loads(db_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    files: set[Path] = set()
    for entry in entries:
        raw = entry.get("file")
        if not raw:
            continue
        f = Path(raw)
        if not f.is_absolute():
            f = (Path(entry.get("directory", build_dir)) / f).resolve()
        if f.suffix != ".c":
            continue
        if not _is_under(f, source_root):
            continue
        if _is_under(f, test_dir):
            continue
        files.add(f.resolve())
    return sorted(files)


def filter_source_files(project: Path, paths: list[str]) -> list[Path]:
    """Resolve user-provided paths into a sorted list of ``.c`` files."""
    files: list[Path] = []
    for p in paths:
        abs_p = Path(p)
        if not abs_p.is_absolute():
            abs_p = project / abs_p
        abs_p = abs_p.resolve()
        if not abs_p.exists():
            raise FileNotFoundError(f"{p}: no such file or directory")
        if abs_p.is_dir():
            files.extend(sorted(abs_p.rglob("*.c")))
        elif abs_p.suffix == ".c":
            files.append(abs_p)
        else:
            raise ValueError(f"{p}: not a .c file or directory")
    # de-dup preserving sort order
    seen: set[Path] = set()
    out: list[Path] = []
    for f in sorted(files):
        rf = f.resolve()
        if rf not in seen:
            seen.add(rf)
            out.append(rf)
    return out


def read_all_sources(project: Path, source_files: list[Path]) -> str:
    """Read all source files into a single line-numbered string for the prompt."""
    chunks: list[str] = []
    for path in source_files:
        try:
            rel = path.resolve().relative_to(project.resolve())
        except ValueError:
            rel = path
        raw = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(
            f"{i + 1:>4}| {line}" for i, line in enumerate(raw.splitlines())
        )
        chunks.append(f"=== {rel} ===\n{body}\n")
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# Line matching + mutation application
# --------------------------------------------------------------------------- #


def _normalize(s: str) -> str:
    return s.strip().replace('\\"', '"').replace("\\'", "'")


def find_target_line(lines: list[str], line_number: int, expected: str) -> Optional[int]:
    """Find the 1-based target line index, with fuzzy search +/-10 lines."""
    if line_number <= 0 or line_number > len(lines):
        return None

    expected_norm = _normalize(expected)
    if _normalize(lines[line_number - 1]) == expected_norm:
        return line_number

    start = max(1, line_number - 10)
    end = min(len(lines), line_number + 10)
    for i in range(start, end + 1):
        if _normalize(lines[i - 1]) == expected_norm:
            return i
    return None


def apply_mutation_to_text(
    text: str, line_number: int, original_line: str, mutated_line: str
) -> tuple[Optional[str], Optional[str]]:
    """Return (mutated_text, None) on success or (None, mismatch_message)."""
    lines = text.splitlines()
    target = find_target_line(lines, line_number, original_line)
    if target is None:
        if 0 < line_number <= len(lines):
            actual = lines[line_number - 1]
        else:
            actual = "<out of range>"
        return None, (
            f"line {line_number}: expected '{original_line.strip()}', "
            f"found '{actual.strip()}'"
        )
    new_lines = list(lines)
    new_lines[target - 1] = mutated_line
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(new_lines) + trailing, None


# --------------------------------------------------------------------------- #
# Build / test runner
# --------------------------------------------------------------------------- #


class Runner:
    """Wraps the project's CMake configure / build / ctest commands."""

    def __init__(
        self,
        project: Path,
        test_dir: Path,
        build_dir: Path,
        generator: str,
        verbose: bool,
    ):
        self.project = project
        self.test_dir = test_dir
        self.build_dir = build_dir
        self.generator = generator
        self.verbose = verbose

    def _run(
        self, cmd: list[str], timeout: Optional[float]
    ) -> tuple[bool, str, bool]:
        """Run a command. Returns (success, combined_output, timed_out)."""
        if self.verbose:
            print(f"      $ {' '.join(cmd)}", file=sys.stderr)
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.project,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT", True
        except OSError as e:
            return False, f"failed to run {cmd[0]}: {e}", False
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out, False

    def configure(self, timeout: float) -> tuple[bool, str]:
        cmd = [
            "cmake",
            "-S",
            str(self.test_dir),
            "-B",
            str(self.build_dir),
            "-G",
            self.generator,
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        ok, out, _ = self._run(cmd, timeout)
        return ok, out

    def build(self, timeout: Optional[float]) -> tuple[bool, str, bool]:
        return self._run(["cmake", "--build", str(self.build_dir)], timeout)

    def ctest(self, timeout: Optional[float]) -> tuple[bool, str, bool]:
        return self._run(
            ["ctest", "--test-dir", str(self.build_dir), "--output-on-failure"],
            timeout,
        )

    def build_and_test(self, timeout: Optional[float]) -> tuple[Outcome, str]:
        """Build, then test. Maps the result onto a mutation Outcome."""
        ok, out, timed_out = self.build(timeout)
        if timed_out:
            return Outcome.TIMEOUT, out
        if not ok:
            return Outcome.BUILD_ERROR, out
        ok, out, timed_out = self.ctest(timeout)
        if timed_out:
            return Outcome.TIMEOUT, out
        return (Outcome.SURVIVED if ok else Outcome.KILLED), out


def backup_path_for(path: Path) -> Path:
    """Sidecar backup path used while a file is mutated."""
    return path.with_name(path.name + ".morris-backup")


def test_line_mutation(
    runner: Runner, abs_path: Path, mutation: Mutation, timeout: float
) -> tuple[Outcome, str]:
    """Apply a single-line mutation, build+test, then restore the file."""
    try:
        original = abs_path.read_text(encoding="utf-8")
    except OSError:
        return Outcome.BUILD_ERROR, "cannot read file"

    mutated, mismatch = apply_mutation_to_text(
        original, mutation.line_number, mutation.original_line, mutation.mutated_line
    )
    if mismatch is not None:
        return Outcome.LINE_MISMATCH, mismatch
    if mutated == original:
        # The AI's mutated_line matched the original verbatim — a no-op that would
        # always "survive". Flag it instead of wasting a build/test cycle.
        return Outcome.LINE_MISMATCH, "no-op: mutated line is identical to the original"

    # Crash-safe on-disk backup: if the process is hard-killed mid-build, the
    # original survives as <file>.morris-backup next to the mutated source, so it
    # can be recovered. (In-memory restore alone would lose it.)
    backup = backup_path_for(abs_path)
    try:
        backup.write_text(original, encoding="utf-8")
    except OSError:
        return Outcome.BUILD_ERROR, "cannot create backup"

    try:
        abs_path.write_text(mutated, encoding="utf-8")
        outcome, detail = runner.build_and_test(timeout)
    finally:
        # Always restore the pristine source and drop the backup.
        abs_path.write_text(original, encoding="utf-8")
        try:
            backup.unlink()
        except OSError:
            pass
    return outcome, detail


# --------------------------------------------------------------------------- #
# AI backends
# --------------------------------------------------------------------------- #


def strip_code_fences(text: str) -> str:
    """Strip a single surrounding markdown code fence if present."""
    trimmed = text.strip()
    if trimmed.startswith("```"):
        rest = trimmed[3:]
        # drop the language tag on the first line
        nl = rest.find("\n")
        if nl != -1:
            rest = rest[nl + 1 :]
        if rest.endswith("```"):
            rest = rest[:-3]
        return rest.strip()
    return trimmed


def extract_json_object(text: str) -> str:
    """Best-effort extraction of the outermost JSON object from a response."""
    s = strip_code_fences(text)
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1]
    return s


class Backend:
    """Base class for AI backends."""

    def converse(self, system: str, user: str) -> str:
        raise NotImplementedError


class CliBackend(Backend):
    """Calls the local ``claude`` CLI in print mode."""

    def __init__(self, model: str, timeout: float = 300.0):
        self.model = model  # alias like "sonnet" / "haiku"
        self.timeout = timeout
        if shutil.which("claude") is None:
            raise RuntimeError("`claude` CLI not found on PATH")

    def converse(self, system: str, user: str) -> str:
        # NOTE: no --bare. --bare forces strict ANTHROPIC_API_KEY auth and breaks
        # OAuth/keychain sessions; the plain CLI reuses the logged-in session.
        # --append-system-prompt (vs --system-prompt) keeps the default agent
        # prompt and just adds our instruction.
        cmd = [
            "claude",
            "-p",
            "--no-session-persistence",
            "--output-format",
            "text",
            "--model",
            self.model,
            "--append-system-prompt",
            system,
        ]
        proc = subprocess.run(
            cmd,
            input=user,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout


class ApiBackend(Backend):
    """Calls the Anthropic API via the ``anthropic`` SDK."""

    def __init__(
        self, model: str, max_tokens: int = 8192, temperature: float = 1.0
    ):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "the `anthropic` package is required for the api backend "
                "(pip install anthropic)"
            ) from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._anthropic = anthropic
        self.model = model  # full model id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = anthropic.Anthropic()

    def converse(self, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        if not parts:
            raise RuntimeError("no text in API response")
        return "".join(parts)


# Model selection: (backend, quick) -> model identifier.
def model_for(backend: str, quick: bool) -> str:
    if backend == "cli":
        return "haiku" if quick else "sonnet"
    return "claude-haiku-4-5-20251001" if quick else "claude-sonnet-4-6"


def make_backend(name: str, quick: bool, temperature: float = 1.0) -> Backend:
    if name == "auto":
        name = "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
    if name == "cli":
        if temperature != 1.0:
            print(
                "⚠️  --temperature only affects the api backend; the claude CLI "
                "ignores it.",
                file=sys.stderr,
            )
        return CliBackend(model_for("cli", quick))
    if name == "api":
        return ApiBackend(model_for("api", quick), temperature=temperature)
    raise ValueError(f"unknown backend: {name}")


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


def build_mutation_prompt(file_contents: str, mutation_count: str) -> str:
    return (
        f"Analyze these host-testable C firmware modules and propose "
        f"{mutation_count} single-line mutations that change observable behavior "
        "but could slip past the existing tests -- revealing real coverage gaps.\n\n"
        "This is embedded firmware, so look beyond relational operators to the "
        "bit-level and width-sensitive code where subtle bugs hide:\n"
        "- Bit-shifts and shift amounts (<<, >>), masks and bit positions "
        "(&, |, ^, ~), compound assignments (+=, |=, &=, ^=)\n"
        "- Integer width and overflow (casts that guard wraparound, literal suffixes)\n"
        "- Boundaries (>, <, >=, <=), arithmetic (+, -, *, /, %), off-by-one and loop bounds\n"
        "- Algorithmic constants and return values -- not hardware register or "
        "bit-position magic numbers\n\n"
        "Don't propose mutations no host test could kill: changes only to "
        "nondeterministic internals (e.g. a PRNG whose tests assert a range, not an "
        "exact value), to hardware values the host build stubs out (volatile, HAL_*, "
        "ISR state), or that are equivalent to the original.\n\n"
        "Only mutate the project source files shown below (the modules under test), "
        "never the test harness itself. Each mutation must still compile as valid C.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) in this exact format:\n"
        '{"mutations": [\n'
        '  {"file_path": "Core/Music/arp.c", "line_number": 42, '
        '"original_line": "    if (x > 0) {", '
        '"mutated_line": "    if (x >= 0) {", '
        '"description": "Change > to >= to test boundary"}\n'
        "]}\n\n"
        "IMPORTANT:\n"
        "- Use paths relative to the project root, exactly as shown in the '=== path ===' headers\n"
        '- Line numbers are shown as "  N| code" -- use the number before the pipe\n'
        '- Copy original_line EXACTLY as it appears AFTER the "| " prefix (including indentation)\n'
        "- Each mutation must be a single-line change that still compiles\n"
        "- The mutated_line must keep the same indentation as original_line\n\n"
        f"Source files (with line numbers):\n{file_contents}"
    )


def format_results_summary(results: list[MutationResult]) -> str:
    out = []
    for r in results:
        out.append(
            f"- {r.mutation.file_path}:{r.mutation.line_number} "
            f"[{r.outcome.value}] {r.mutation.description} | "
            f"{r.mutation.original_line.strip()} -> {r.mutation.mutated_line.strip()}"
        )
    return "\n".join(out) + ("\n" if out else "")


def build_analysis_prompt(
    auto_mode: bool, results_summary: str, file_contents: str, test_contents: str
) -> str:
    if auto_mode:
        return (
            f"Mutation testing results:\n{results_summary}\n\n"
            f"Module source code:\n{file_contents}\n\n"
            f"Existing Unity test files:\n{test_contents}\n\n"
            "Write new Unity test functions that would KILL each SURVIVED mutation.\n"
            "These tests will be inserted into the existing test files, which already\n"
            "include unity.h and the module header and define setUp/tearDown.\n\n"
            "Respond with ONLY a JSON object (no markdown fences) in this exact format:\n"
            '{"additions": [\n'
            '  {"test_file": "test/test_arp.c",\n'
            '   "functions": ["static void test_new_case(void)\\n{\\n    ...\\n}"],\n'
            '   "runners": ["test_new_case"]}\n'
            "]}\n\n"
            "IMPORTANT:\n"
            "- test_file must match one of the existing test file paths shown above\n"
            "- Each entry in `functions` is a complete static C function definition\n"
            "- Each entry in `runners` is the bare function name (it will be wrapped in RUN_TEST(...))\n"
            "- functions[i] corresponds to runners[i]; keep them aligned and 1:1\n"
            "- Use only the public API exercised by the existing tests; the code must compile"
        )
    return (
        "These single-line mutations were tested against the project's host unit "
        "tests (CMake + Unity + CTest).\n\n"
        f"Results:\n{results_summary}\n\n"
        f"Module source code:\n{file_contents}\n\n"
        "For each SURVIVED mutation, explain:\n"
        "1. Why the current tests don't catch it\n"
        "2. A specific Unity test that would catch it (show the C code)\n\n"
        "Be concise and actionable."
    )


# --------------------------------------------------------------------------- #
# Auto-apply (insert new Unity tests)
# --------------------------------------------------------------------------- #


def insert_unity_tests(
    source: str, functions: list[str], runner_names: list[str]
) -> Optional[str]:
    """Insert test functions before main() and register them after UNITY_BEGIN().

    Returns the patched source, or None if the anchors could not be found.
    """
    lines = source.splitlines()

    # Find the line that opens main() and the UNITY_BEGIN() call.
    main_idx = next(
        (i for i, ln in enumerate(lines) if ln.lstrip().startswith("int main(")),
        None,
    )
    begin_idx = next(
        (i for i, ln in enumerate(lines) if "UNITY_BEGIN()" in ln),
        None,
    )
    if main_idx is None or begin_idx is None:
        return None

    fn_block = "\n".join(functions)
    runner_block = "\n".join(f"    RUN_TEST({name});" for name in runner_names)

    out: list[str] = []
    for i, ln in enumerate(lines):
        if i == main_idx:
            out.append(fn_block)
            out.append("")
        out.append(ln)
        if i == begin_idx:
            out.append(runner_block)

    trailing = "\n" if source.endswith("\n") else ""
    return "\n".join(out) + trailing


def _verify_kills(
    runner: Runner, project: Path, survivors: list[Mutation], timeout: float
) -> list[Mutation]:
    """Return the survivors the current test tree now KILLS (the mutant is the oracle).

    The candidate test is already written into its file and the clean tree passes.
    For each survivor, re-apply its mutation and check the suite now fails -- which,
    since a survivor leaves the baseline green, means the new test caught it.
    The source is always restored afterwards.
    """
    killed: list[Mutation] = []
    for s in survivors:
        sp = (project / s.file_path).resolve()
        try:
            sorig = sp.read_text(encoding="utf-8")
        except OSError:
            continue
        mtext, mismatch = apply_mutation_to_text(
            sorig, s.line_number, s.original_line, s.mutated_line
        )
        if mismatch is not None:
            continue
        try:
            sp.write_text(mtext, encoding="utf-8")
            if runner.build_and_test(timeout)[0] == Outcome.KILLED:
                killed.append(s)
        finally:
            sp.write_text(sorig, encoding="utf-8")
    return killed


def auto_apply(
    project: Path,
    analysis: str,
    runner: Runner,
    timeout: float,
    survivors: list[Mutation],
) -> None:
    print("\n🔧 Auto-applying test improvements...", file=sys.stderr)
    try:
        data = json.loads(extract_json_object(analysis))
    except json.JSONDecodeError as e:
        print(f"   ⚠️  Could not parse AI additions JSON: {e}", file=sys.stderr)
        return

    additions = data.get("additions", [])
    if not additions:
        print("   ⚠️  No test additions in AI response", file=sys.stderr)
        return

    # Flatten to individual candidate tests so each can be verified in isolation.
    candidates: list[tuple[str, str, str]] = []  # (test_file, function, runner_name)
    for entry in additions:
        test_file = entry.get("test_file")
        functions = entry.get("functions", [])
        runners = entry.get("runners", [])
        if not test_file or not functions:
            continue
        for i, fn in enumerate(functions):
            name = runners[i] if i < len(runners) else None
            if name:
                candidates.append((test_file, fn, name))

    backups: dict[Path, str] = {}
    accepted = 0
    remaining = list(survivors)  # survivors not yet covered by an accepted test
    try:
        for test_file, fn, name in candidates:
            path = (project / test_file).resolve()
            if not path.exists():
                print(f"   ⚠️  {test_file}: no such test file, skipping", file=sys.stderr)
                continue
            if path not in backups:
                backups[path] = path.read_text(encoding="utf-8")

            # Insert on top of whatever has already been accepted in this file.
            current = path.read_text(encoding="utf-8")
            patched = insert_unity_tests(current, [fn], [name])
            if patched is None:
                print(
                    f"   ⚠️  {test_file}: could not find main()/UNITY_BEGIN anchors",
                    file=sys.stderr,
                )
                continue

            # Gate 1: the new test must compile and pass on clean source.
            path.write_text(patched, encoding="utf-8")
            outcome = runner.build_and_test(timeout)[0]
            if outcome != Outcome.SURVIVED:
                why = (
                    "won't compile (e.g. duplicate name)"
                    if outcome == Outcome.BUILD_ERROR
                    else "fails on clean source"
                )
                print(f"   ✗ {name}: rejected ({why})", file=sys.stderr)
                path.write_text(current, encoding="utf-8")
                continue

            # Gate 2: it must actually KILL an as-yet-uncovered survivor.
            newly = _verify_kills(runner, project, remaining, timeout)
            if not newly:
                print(
                    f"   ✗ {name}: rejected (kills no uncovered survivor)",
                    file=sys.stderr,
                )
                path.write_text(current, encoding="utf-8")
                continue

            for s in newly:
                remaining.remove(s)
            accepted += 1
            covered = ", ".join(f"{s.file_path}:{s.line_number}" for s in newly)
            print(f"   ✓ {name}: kills {covered}", file=sys.stderr)

        if accepted == 0:
            for path, original in backups.items():
                path.write_text(original, encoding="utf-8")
            print(
                "   ⚠️  No generated test verifiably killed a survivor; nothing kept.",
                file=sys.stderr,
            )
            return

        # Belt-and-suspenders: the accepted set must still pass all together.
        if runner.build_and_test(timeout)[0] != Outcome.SURVIVED:
            for path, original in backups.items():
                path.write_text(original, encoding="utf-8")
            print(
                "   ❌ Accepted tests failed when combined; reverting all.",
                file=sys.stderr,
            )
            return

        killed_n = len(survivors) - len(remaining)
        print(
            f"   ✅ Added {accepted} verified test(s), killing {killed_n} of "
            f"{len(survivors)} survivor(s).",
            file=sys.stderr,
        )
        for s in remaining:
            print(
                f"   ℹ️  still unkilled: {s.file_path}:{s.line_number} {s.description}",
                file=sys.stderr,
            )
    except Exception:
        # On any error, restore everything we touched.
        for path, original in backups.items():
            try:
                path.write_text(original, encoding="utf-8")
            except OSError:
                pass
        raise


# --------------------------------------------------------------------------- #
# CLI / main workflow
# --------------------------------------------------------------------------- #


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="morris",
        description="AI-powered mutation testing for C firmware (CMake + CTest).",
    )
    p.add_argument(
        "paths",
        nargs="*",
        help="Source files/dirs to mutate (default: discover from the test build).",
    )
    p.add_argument(
        "--project",
        default=".",
        help="Firmware project root containing the test dir (default: cwd).",
    )
    p.add_argument(
        "--test-dir",
        default="test",
        help="CMake test source dir, relative to project (default: test).",
    )
    p.add_argument(
        "--build-dir",
        default=None,
        help="CMake build dir, relative to project (default: <test-dir>/build).",
    )
    p.add_argument(
        "--source-root",
        default="Core",
        help="Subtree holding the modules under test, relative to project "
        "(default: Core).",
    )
    p.add_argument(
        "--generator", default="Ninja", help="CMake generator (default: Ninja)."
    )
    p.add_argument(
        "--backend",
        choices=["auto", "cli", "api"],
        default="auto",
        help="AI backend: claude CLI, Anthropic API, or auto-detect (default: auto).",
    )
    p.add_argument(
        "--auto", dest="auto_mode", action="store_true",
        help="Automatically write & verify new Unity tests for survivors.",
    )
    p.add_argument(
        "--quick", dest="quick_mode", action="store_true",
        help="Use the faster Haiku model.",
    )
    p.add_argument(
        "-n", "--mutations", type=int, default=None,
        help="Number of mutations to request (default: 5-8).",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Sampling temperature for the AI calls, 0.0-1.0 (default: 1.0). "
        "Lower values make mutation selection more repeatable but less varied "
        "across re-runs; only the api backend honors it.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if not 0.0 <= args.temperature <= 1.0:
        print("❌ --temperature must be between 0.0 and 1.0", file=sys.stderr)
        return 1

    # Emit UTF-8 regardless of the console's default code page (Windows cp1252
    # would otherwise mangle or refuse the status emoji).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    print(
        "🧬 Morris Minor (Morris embedded C port) - AI-Powered Mutation Testing\n",
        file=sys.stderr,
    )

    project = Path(args.project).resolve()
    test_dir = (project / args.test_dir).resolve()
    build_dir = (
        (project / args.build_dir).resolve()
        if args.build_dir
        else (test_dir / "build")
    )
    source_root = (project / args.source_root).resolve()

    if not (test_dir / "CMakeLists.txt").exists():
        print(
            f"❌ No CMakeLists.txt in test dir: {test_dir}\n"
            "   Point --project at the firmware root that contains the test dir.",
            file=sys.stderr,
        )
        return 1

    runner = Runner(project, test_dir, build_dir, args.generator, args.verbose)

    # Build the AI backend early so we fail fast on missing creds/tools.
    try:
        backend = make_backend(args.backend, args.quick_mode, args.temperature)
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    # Step 1: Configure (also emits compile_commands.json for discovery) + baseline.
    print("⏱️  Configuring + running baseline tests...", file=sys.stderr)
    ok, out = runner.configure(timeout=300.0)
    if not ok:
        print(f"❌ CMake configure failed:\n{out}", file=sys.stderr)
        return 1

    start = time.monotonic()
    outcome, detail = runner.build_and_test(timeout=600.0)
    baseline_duration = time.monotonic() - start
    if outcome != Outcome.SURVIVED:
        print(
            f"❌ Baseline build/tests did not pass ({outcome.value}). "
            f"Fix them first.\n{detail}",
            file=sys.stderr,
        )
        return 1
    test_timeout = max(30.0, baseline_duration * 3.0)
    print(
        f"   ✅ Baseline passed in {baseline_duration:.1f}s "
        f"(mutation timeout: {test_timeout:.1f}s)",
        file=sys.stderr,
    )

    # Step 2: Discover source files.
    print("\n📁 Discovering source files...", file=sys.stderr)
    if args.paths:
        try:
            source_files = filter_source_files(project, args.paths)
        except (FileNotFoundError, ValueError) as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
    else:
        source_files = discover_from_compile_db(build_dir, source_root, test_dir)
    if not source_files:
        print(
            "❌ No C source files found to mutate. "
            "Pass explicit paths or check --source-root.",
            file=sys.stderr,
        )
        return 1
    for f in source_files:
        try:
            print(f"   {f.relative_to(project)}", file=sys.stderr)
        except ValueError:
            print(f"   {f}", file=sys.stderr)

    file_contents = read_all_sources(project, source_files)

    # Step 3: Ask AI for a mutation plan.
    mutation_count = (
        f"exactly {args.mutations}" if args.mutations is not None else "5-8"
    )
    print("\n🧬 Asking AI for mutation plan...", file=sys.stderr)
    plan_text = backend.converse(
        "You are a mutation testing expert for C. Respond only with valid JSON.",
        build_mutation_prompt(file_contents, mutation_count),
    )
    try:
        plan = json.loads(extract_json_object(plan_text))
        mutations = [Mutation.from_dict(m) for m in plan["mutations"]]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(
            f"❌ Failed to parse mutation plan: {e}\nRaw response:\n{plan_text}",
            file=sys.stderr,
        )
        return 1
    print(f"   Got {len(mutations)} mutations", file=sys.stderr)
    if not mutations:
        print("   The AI proposed no mutations; nothing to test.", file=sys.stderr)
        return 0

    # Warn about leftover backups from a previously interrupted run.
    stale = [f for f in source_files if backup_path_for(f).exists()]
    if stale:
        print(
            "\n⚠️  Found leftover .morris-backup files (a previous run may have "
            "been killed). Check these sources are not still mutated:",
            file=sys.stderr,
        )
        for f in stale:
            print(f"     {backup_path_for(f)}", file=sys.stderr)

    # Step 4: Test each mutation.
    print("\n🧪 Testing mutations...\n", file=sys.stderr)
    results: list[MutationResult] = []
    icons = {
        Outcome.SURVIVED: "❌ SURVIVED",
        Outcome.KILLED: "✅ KILLED",
        Outcome.TIMEOUT: "⏱️  TIMEOUT",
        Outcome.BUILD_ERROR: "🔧 BUILD ERROR",
        Outcome.LINE_MISMATCH: "⚠️  LINE MISMATCH",
    }
    for i, mut in enumerate(mutations):
        abs_path = (project / mut.file_path).resolve()
        sys.stderr.write(
            f"   [{i + 1}/{len(mutations)}] {mut.file_path}:{mut.line_number} "
            f"- {mut.description}... "
        )
        sys.stderr.flush()
        if not abs_path.exists():
            outcome, detail = Outcome.LINE_MISMATCH, "file not found"
        else:
            outcome, detail = test_line_mutation(runner, abs_path, mut, test_timeout)
        print(icons[outcome], file=sys.stderr)
        results.append(MutationResult(mut, outcome, detail))

    # Step 5: Summary.
    survived = [r for r in results if r.outcome == Outcome.SURVIVED]
    killed = [r for r in results if r.outcome == Outcome.KILLED]
    testable = [
        r
        for r in results
        if r.outcome not in (Outcome.BUILD_ERROR, Outcome.LINE_MISMATCH)
    ]
    print(
        f"\n📊 Results: {len(killed)} killed, {len(survived)} survived "
        f"out of {len(testable)} testable mutations",
        file=sys.stderr,
    )

    if not survived:
        print("\n🎉 All mutations were killed! Your tests look solid.", file=sys.stderr)
        return 0

    # Step 6: Analysis (+ optional auto-apply).
    print("\n💡 Analyzing surviving mutations...\n", file=sys.stderr)
    test_contents = ""
    if args.auto_mode:
        test_files = sorted(test_dir.glob("test_*.c"))
        test_contents = read_all_sources(project, test_files)

    system_prompt = (
        "Output only the requested JSON object. No markdown, no code fences, "
        "no explanation."
        if args.auto_mode
        else "You are a C testing expert. Help improve test coverage based on "
        "mutation testing results."
    )
    analysis = backend.converse(
        system_prompt,
        build_analysis_prompt(
            args.auto_mode, format_results_summary(results), file_contents, test_contents
        ),
    )

    if args.auto_mode:
        auto_apply(
            project, analysis, runner, test_timeout, [r.mutation for r in survived]
        )
    else:
        print(analysis)

    return 0


if __name__ == "__main__":
    sys.exit(main())
