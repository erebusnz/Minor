# 🧬 Morris Minor (Morris embedded C port)

### AI-Powered Mutation Testing for C firmware

*Find the bugs hiding in your test suite*

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Claude](https://img.shields.io/badge/Claude-Sonnet%20%2F%20Haiku-8A2BE2.svg)](https://www.anthropic.com/claude)

A Python port of [Marc Brooker's Morris](https://github.com/marcbrooker/morris),
adapted from Rust/`cargo` to **embedded C projects with a host-side CMake + CTest
unit suite** (e.g. [Unity](https://github.com/ThrowTheSwitch/Unity)). It works
with any project laid out that way — see *Project layout*.

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│  Your C     │ ───> │ Morris Minor │ ───> │  Test Gaps  │
│  + Unity    │      │ (Fixed Flow) │      │  + Fixes    │
│   tests     │      └──────────────┘      └─────────────┘
└─────────────┘
```

Like [the original](https://github.com/marcbrooker/morris), Morris Minor follows a
**fixed, deterministic workflow**. The AI
(Claude) is consulted exactly **twice** — once to propose mutations, once to
analyse the survivors. All file discovery, building, test execution, and
mutation application is plain deterministic code.

---

## How it differs from the Rust original

The original drives `cargo test`, where build and test are one step. C splits
them, which actually makes the outcomes *cleaner*:

| After applying the mutant | Outcome |
|---|---|
| Source fails to compile | 🔧 **BUILD ERROR** (not counted) |
| Compiles, a test fails | ✅ **KILLED** (good — the suite caught it) |
| Compiles, all tests pass | ❌ **SURVIVED** (a coverage gap) |
| Build/tests exceed the timeout (e.g. a mutated loop bound now spins forever) | ⏱️ **TIMEOUT** |
| The target line no longer matches what the AI quoted | ⚠️ **LINE MISMATCH** (not counted) |

Everything else mirrors Morris: `±10`-line fuzzy matching to locate the line to
mutate, automatic backup/restore, a `3×`-baseline timeout (min 30 s), and an
optional `--auto` mode that writes and verifies new tests.

---

## Requirements

- **Python 3.10+** (standard library only for the default backend).
- A project with a host-side CMake test directory (see *Project layout*).
- **CMake**, a generator (**Ninja** by default), and **CTest** on `PATH`.
- A **native C compiler** for the host tests (`gcc`/`clang`/MSVC). Note this is
  *not* the firmware's cross-compiler — the host suite builds for your machine.
- An AI backend (pick one):
  - **`cli`** *(default, no API key)* — the [`claude`](https://docs.claude.com/en/docs/claude-code)
    CLI on `PATH`, signed in. Morris Minor calls `claude -p`.
  - **`api`** — `pip install anthropic` and set `ANTHROPIC_API_KEY`.
  - **`openai`** — `pip install openai` and set `OPENAI_API_KEY`.

---

## Quick start

Morris Minor is a single file — there's nothing to build or install. Copy
`morris-minor.py` wherever you like and run it with Python 3.10+ (the default
`cli` backend needs no extra packages; the `api` backend wants
`pip install anthropic`). Point it at your firmware project root (the directory
containing the test dir):

```bash
python morris-minor.py --project path/to/firmware
```

That's it. Morris Minor configures the test build, runs the baseline, asks Claude for
5–8 strategic mutations, tries each one, and reports what survived.

### Example run

```text
🧬 Morris Minor (Morris embedded C port) - AI-Powered Mutation Testing

⏱️  Configuring + running baseline tests...
   ✅ Baseline passed in 0.1s (mutation timeout: 30.0s)
📁 Discovering source files...
   Core/dsp/filter.c
   Core/util/ringbuf.c
🧬 Asking AI for mutation plan...
   Got 4 mutations
🧪 Testing mutations...
   [1/4] Core/util/ringbuf.c:42 - Change >= to > in the full check...   ✅ KILLED
   [2/4] Core/dsp/filter.c:88 - Change + to - in the accumulator...     ✅ KILLED
   [3/4] Core/util/ringbuf.c:57 - Off-by-one: head+1 -> head on wrap... ✅ KILLED
   [4/4] Core/dsp/filter.c:31 - Change <= to < on the tap loop...       ✅ KILLED

📊 Results: 4 killed, 0 survived out of 4 testable mutations

🎉 All mutations were killed! Your tests look solid.
```

---

## Command-line options

| Flag | Description |
|------|-------------|
| `paths...` | Specific `.c` files/dirs to mutate, relative to project (default: auto-discover from the test build). |
| `--project DIR` | Firmware project root containing the test dir (default: cwd). |
| `--test-dir DIR` | CMake test source dir, relative to project (default: `test`). |
| `--build-dir DIR` | CMake build dir, relative to project (default: `<test-dir>/build`). |
| `--source-root DIR` | Subtree holding the modules under test, relative to project (default: `Core`). |
| `--generator NAME` | CMake generator (default: `Ninja`). |
| `--backend {auto,cli,api,openai}` | AI backend (default: `auto`). `api` = Anthropic, `openai` = OpenAI. |
| `--auto` | Write & verify new Unity tests for survivors. |
| `--quick` | Use the faster Haiku model. |
| `-n, --mutations N` | Request exactly N mutations (default: 5–8). |
| `--temperature T` | Sampling temperature for the AI calls, `0.0`–`1.0` (default: `1.0`). Lower = more repeatable but less varied mutation selection across re-runs. Honored by the `api` and `openai` backends. |
| `-v, --verbose` | Print the CMake/CTest commands as they run. |

`--backend auto` uses `api` (Anthropic) if `ANTHROPIC_API_KEY` is set, then
`openai` if `OPENAI_API_KEY` is set, otherwise the `claude` CLI.

### Examples

```bash
# Standard analysis
python morris-minor.py --project path/to/firmware

# Only mutate one module, quick model
python morris-minor.py --project path/to/firmware --quick Core/dsp/filter.c

# Hands-free: auto-write tests that kill the survivors, then verify
python morris-minor.py --project path/to/firmware --auto

# Force the Anthropic API backend
ANTHROPIC_API_KEY=sk-... python morris-minor.py --project path/to/firmware --backend api

# Use the OpenAI API backend
OPENAI_API_KEY=sk-... python morris-minor.py --project path/to/firmware --backend openai
```

---

## Use it as a Claude Code skill

This repo ships a [Claude Code](https://docs.claude.com/en/docs/claude-code)
skill at [`.claude/skills/morris-minor/`](.claude/skills/morris-minor/SKILL.md).
When you run Claude Code inside this repo it's picked up automatically, so you can
just ask in natural language — e.g. *"find the gaps in my Unity test suite"* or
*"run mutation testing on ../my-firmware"* — and Claude drives `morris-minor.py`
for you, interprets the survivors, and (with your OK) writes verified tests. The
skill references the `morris-minor.py` at the repo root, so there's nothing extra
to install. To make it available in **every** local session, copy that folder to
`~/.claude/skills/morris-minor/` (bundle `morris-minor.py` alongside its
`SKILL.md` so it works from any directory).

---

## How it works

1. **Configure + baseline** — `cmake -S <test-dir> -B <build-dir> -G <gen>
   -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`, then build + `ctest`. Must pass, and
   times the run to set a `3×` mutation timeout (min 30 s).
2. **Discover** — reads `compile_commands.json` and keeps the `.c` files under
   `--source-root` (the modules actually compiled into the tests), excluding the
   test harness, Unity, and stubs. Or use the explicit `paths` you pass.
3. **Mutation plan** *(AI call #1)* — Claude returns 5–8 single-line mutations as
   JSON (boundaries, arithmetic, logic, off-by-one, return values).
4. **Test loop** — for each mutation: back up the file, apply the one-line
   change, rebuild (incremental), run `ctest`, classify the outcome, restore.
5. **Summary** — killed / survived / testable counts.
6. **Analysis** *(AI call #2, only if something survived)* — explains why each
   survivor slips through and shows a Unity test that would catch it.
7. **Auto mode** *(optional)* — Claude returns new Unity test functions + their
   `RUN_TEST` registrations as JSON; Morris Minor inserts each function before
   `main()` and its runner after `UNITY_BEGIN()`, rebuilds, and runs the suite.
   If the build or tests fail, every touched file is reverted.

---

## Project layout it expects

```
<project>/                 # your firmware project root  (--project)
├── Core/                  # modules under test         (--source-root)
│   ├── dsp/filter.c
│   └── util/ringbuf.c
└── test/                  # host CMake test project    (--test-dir)
    ├── CMakeLists.txt      #   enable_testing(); add_test(...)
    ├── unity/              #   vendored Unity
    ├── test_filter.c       #   main() with UNITY_BEGIN()/RUN_TEST/UNITY_END()
    ├── test_ringbuf.c
    └── build/              #   generated by Morris Minor (--build-dir)
```

All four path flags are relative to `--project`: `--source-root` (`Core`),
`--test-dir` (`test`), and `--build-dir` (defaults to `test/build`, where Morris Minor
writes the CMake build and reads `compile_commands.json` for discovery).

`--auto` relies on each `test_*.c` having the standard Unity `main()` shape
(`UNITY_BEGIN()` … `RUN_TEST(...)` … `return UNITY_END();`).

---

## Notes for STM32 / embedded C projects

- **It only mutates host-buildable logic.** Morris Minor mutates exactly the `.c`
  files your `test/` build compiles (it reads them from
  `compile_commands.json`). On a typical STM32CubeMX project that's the portable
  logic you've factored out — e.g. `Core/dsp/filter.c`, `Core/util/ringbuf.c`. HAL,
  peripheral drivers, `main.c`, `Drivers/`, `Middlewares/`, and the USB stack
  aren't in the host build, so they're skipped automatically. To widen coverage,
  extract more hardware-independent modules and add them to `test/CMakeLists.txt`
  — with stubs for their hardware surface (e.g. a fake filesystem layer so a
  file-parsing module can run against an in-memory buffer).

- **Host compiler, not the ARM cross-compiler.** The host suite builds for *your*
  machine, so you need native `gcc`/`clang`/MSVC — **not** `arm-none-eabi-gcc`.

- **`--source-root Core` matches the CubeMX convention** (application code under
  `Core/`). Override it if your project keeps its logic elsewhere.

- **It mirrors your CI.** Morris Minor runs the same
  `cmake -S test -B test/build -G Ninja` → `cmake --build` → `ctest` sequence a
  typical host-test CI job uses, so a clean Morris Minor run reproduces CI locally
  before you push.

- **Deterministic tests only.** Mutation testing assumes repeatable pass/fail.
  Code with randomness or timing should be asserted on invariants or membership
  (e.g. "the output stays within the expected set") rather than an exact value,
  so a mutant isn't flagged inconsistently between runs.

- **Host-side, not on-target.** Morris Minor exercises the host unit tests on your
  machine. It does not build, flash, or test on the MCU — on-target/hardware
  testing is out of scope.

---

## Morris Minor vs exhaustive mutation testing

Tools like [Mull](https://github.com/mull-project/mull) (LLVM-IR based) and
[Dextool mutate](https://github.com/joakim-brannstrom/dextool) do *exhaustive*
mutation testing for C/C++:

- Systematically generate every mutation — often hundreds to thousands
- Work at the AST / LLVM-IR level
- Produce comprehensive mutation-score reports
- Best for: CI gates and full audits

Morris Minor takes the **AI-guided** approach instead:

- Fixed workflow; the AI only selects ~5–8 strategic mutations and explains the
  survivors (it never drives the build or files)
- Source-level, single-line edits rebuilt with your existing CMake/CTest
- Contextual, actionable explanations — plus optional `--auto` test writing
- Best for: interactive development, learning, and a fast "where are my test
  gaps?" pass

The exhaustive tools are more mature and thorough — reach for them when you want
a complete audit. Morris Minor is the quick, conversational complement.

---

## Tests

The deterministic logic (line matching, mutation apply/restore, JSON/fence
extraction, discovery filtering, prompt building, Unity insertion) has its own
suite that needs no compiler or AI backend:

```bash
python -m unittest test_morris -v
```

---

## License & credits

Morris Minor is a **modified derivative work** of
[marcbrooker/morris](https://github.com/marcbrooker/morris) — original concept
and Rust implementation by Marc Brooker.

Morris is licensed under the **Apache License, Version 2.0**, and Morris Minor is
distributed under the same license. See [LICENSE](LICENSE) for the full terms and
[NOTICE](NOTICE) for attribution and a summary of the changes made in this port.
