---
name: morris-minor
description: >-
  AI-guided mutation testing for embedded C firmware that has a host-side CMake +
  CTest + Unity test suite. It mutates the code under test one line at a time,
  reruns the suite, and reports which mutations SURVIVED (real coverage gaps) —
  then optionally writes new Unity tests and verifies each one actually kills its
  mutant before keeping it. Use this whenever the user wants to find gaps or weak
  spots in their C unit tests, judge how good their tests really are ("are my
  tests any good?", "what would my tests miss?"), run mutation testing, find bugs
  hiding in a test suite, harden a Unity/CTest suite, or auto-generate tests that
  close coverage gaps — especially for STM32 / embedded C projects. Trigger on
  mentions of mutation testing, test-coverage quality, surviving mutants, weak or
  missing tests, or "make my C tests stronger," even if the user never says
  "Morris Minor."
---

# Morris Minor — AI mutation testing for embedded C firmware

Morris Minor finds the bugs your test suite would miss. It applies small,
single-line mutations to the modules under test (flip `>` to `>=`, change a shift
amount, drop a mask bit, etc.), rebuilds, and reruns the host unit tests:

- compiles but a test fails → **KILLED** (the suite caught it — good)
- compiles and all tests pass → **SURVIVED** (a real coverage gap)
- fails to compile → **BUILD ERROR** (not counted)
- the quoted line no longer matches → **LINE MISMATCH** (not counted)
- build/tests exceed the timeout → **TIMEOUT**

The AI is consulted only twice — once to propose strategic mutations, once to
analyze the survivors. Everything else (discovery, build, apply, restore) is
deterministic. This is a fast "where are my test gaps?" pass, not an exhaustive
audit like Mull or Dextool.

## When to use it

The user has a C/embedded project with a **host-side** unit suite (Unity, driven
by CMake + CTest) and wants to know whether those tests actually catch bugs, or
wants help closing the gaps. It only mutates the `.c` files that the host test
build compiles — typically the portable logic factored out of an STM32CubeMX
project (DSP, ring buffers, parsers, music/quantizer logic). HAL, `main.c`,
peripheral drivers, and ISRs aren't in the host build, so they're skipped.

## Prerequisites — check these first

- **CMake**, a generator (**Ninja** by default), and **CTest** on `PATH`.
- A **native host C compiler** (`gcc`/`clang`/MSVC) — NOT the ARM cross-compiler.
  The host suite builds for this machine.
- The baseline suite must **build and pass** before mutation testing can run; the
  tool aborts otherwise. If it's red, fix or rebuild it first.
- An AI backend, auto-detected (prefers Anthropic key, then OpenAI key, else CLI):
  - `cli` (default, no key): the `claude` CLI on `PATH`, signed in.
  - `api`: `pip install anthropic` and `ANTHROPIC_API_KEY` set (Anthropic).
  - `openai`: `pip install openai` and `OPENAI_API_KEY` set (OpenAI).

If a prerequisite is missing, say so plainly rather than running and failing.

## How to run

This skill ships alongside `morris-minor.py` at the repository root — run that
single file (stdlib-only for the default `cli` backend, nothing to build). Point
`--project` at the firmware root that contains the test dir:

```bash
python morris-minor.py --project path/to/firmware
```

Mutation testing temporarily edits source files and runs builds, so confirm the
working tree is clean (or the user is fine with it) before a run. The tool backs
up and restores every file it touches; a clean run leaves the tree unchanged.

### Useful flags

| Flag | Purpose |
|------|---------|
| `paths...` | Specific `.c` files/dirs to mutate (default: auto-discover from the test build). |
| `--project DIR` | Firmware root containing the test dir (default: cwd). |
| `--test-dir DIR` | CMake test source dir, relative to project (default: `test`). |
| `--build-dir DIR` | CMake build dir (default: `<test-dir>/build`). |
| `--source-root DIR` | Subtree holding the modules under test (default: `Core`). |
| `--generator NAME` | CMake generator (default: `Ninja`). |
| `--backend {auto,cli,api,openai}` | AI backend (default: `auto`; `api`=Anthropic, `openai`=OpenAI). |
| `--auto` | Write & verify new Unity tests that kill the survivors. |
| `--quick` | Use the faster/cheaper model variant (Haiku / gpt-5-mini). |
| `-n, --mutations N` | Request exactly N mutations (default: 5–8). |
| `--temperature T` | Sampling temperature 0.0–1.0 (default 1.0); lower = more repeatable, less varied across re-runs (honored by the `api` and `openai` backends). |
| `-v, --verbose` | Print the CMake/CTest commands as they run. |

Progress and results print to **stderr**; in `--explain` (default) mode the AI's
written analysis prints to **stdout**.

## Interpreting the output

Focus the user on **SURVIVED** mutations — each is a behavior change no test
noticed, i.e. a gap. Without `--auto`, the tool prints, per survivor, why the
tests miss it and a concrete Unity test that would catch it. KILLED results are
reassurance the suite has teeth. BUILD ERROR / LINE MISMATCH are non-results;
don't present them as findings.

Some survivors are genuinely **not killable** by a host test (equivalent mutants,
or behavior only observable on hardware / in nondeterministic code such as a
PRNG). Don't push the user to chase those — call them out as such.

## `--auto`: verified test generation

With `--auto`, the tool asks the AI for new Unity tests, then **verifies each one
before keeping it**: a test is accepted only if it (1) compiles and passes on
clean source and (2) *fails* when the survivor's mutation is re-applied — proving
it actually kills that bug. Tests are verified individually, so a useless or
non-compiling one is dropped while the good ones are kept and registered with
`RUN_TEST(...)`. If the combined suite then fails, everything is reverted. The
result: only tests proven to close a real gap land in the suite.

This permanently edits the project's `test_*.c` files (that's the point), so make
sure the user wants that, and remind them to review/commit the additions.

## Project layout it expects

```
<project>/                 # --project
├── Core/                  # modules under test  (--source-root)
│   └── dsp/filter.c …
└── test/                  # host CMake test project  (--test-dir)
    ├── CMakeLists.txt     #   enable_testing(); add_test(...)
    ├── unity/             #   vendored Unity
    ├── test_filter.c      #   main() with UNITY_BEGIN()/RUN_TEST/UNITY_END()
    └── build/             #   generated  (--build-dir)
```

`--auto` relies on each `test_*.c` having the standard Unity `main()` shape
(`UNITY_BEGIN()` … `RUN_TEST(...)` … `return UNITY_END();`).

## Suggested workflow

1. Confirm prerequisites and that the host suite builds + passes (`ctest`).
2. Run with sensible defaults; if discovery finds nothing, the logic may not be
   in `--source-root`, or no modules are compiled into the host build — say so.
3. Summarize survivors as actionable coverage gaps, separating the killable ones
   from the inherently-unkillable.
4. Offer `--auto` to write verified tests, or hand-write tests from the analysis.
5. Re-run to confirm the new tests kill what they should.
