# 🧬 Morris Minor (Morris embedded C port)

### AI-Powered Mutation Testing for C firmware

A Python port of [Marc Brooker's Morris](https://github.com/marcbrooker/morris),
adapted from Rust/`cargo` to **embedded C projects with a host-side CMake + CTest
unit suite** (e.g. [Unity](https://github.com/ThrowTheSwitch/Unity)). It works
with any project laid out that way — see *Project layout*.

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│  Your C     │ ───> │    Morris    │ ───> │  Test Gaps  │
│  + Unity    │      │ (Fixed Flow) │      │  + Fixes    │
│   tests     │      └──────────────┘      └─────────────┘
└─────────────┘
```

Like [the original](https://github.com/marcbrooker/morris), Morris follows a
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
    CLI on `PATH`, signed in. Morris calls `claude -p`.
  - **`api`** — `pip install anthropic` and set `ANTHROPIC_API_KEY`.

---

## Quick start

Point it at your firmware project root (the directory containing the test dir):

```bash
python morris-minor.py --project path/to/firmware
```

That's it. Morris configures the test build, runs the baseline, asks Claude for
5–8 strategic mutations, tries each one, and reports what survived.

### Example run

```text
🧬 Morris Minor (Morris embedded C port) - AI-Powered Mutation Testing

⏱️  Configuring + running baseline tests...
   ✅ Baseline passed in 0.1s (mutation timeout: 30.0s)
📁 Discovering source files...
   Core/IO/wav.c
   Core/Music/arp.c
🧬 Asking AI for mutation plan...
   Got 4 mutations
🧪 Testing mutations...
   [1/4] Core/Music/arp.c:57 - Change >= 1 to > 1 in UPDOWN descent... ✅ KILLED
   [2/4] Core/IO/wav.c:54 - Change (csz & 1) to (csz & 0)...           ✅ KILLED
   [3/4] Core/Music/arp.c:123 - Change >= 0 to > 0 on clock firing... ✅ KILLED
   [4/4] Core/Music/arp.c:86 - Halve the derived gate duration...      ✅ KILLED

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
| `--backend {auto,cli,api}` | AI backend (default: `auto`). |
| `--auto` | Write & verify new Unity tests for survivors. |
| `--quick` | Use the faster Haiku model. |
| `-n, --mutations N` | Request exactly N mutations (default: 5–8). |
| `-v, --verbose` | Print the CMake/CTest commands as they run. |

`--backend auto` uses `api` if `ANTHROPIC_API_KEY` is set, otherwise the `claude`
CLI.

### Examples

```bash
# Standard analysis
python morris-minor.py --project path/to/firmware

# Only mutate one module, quick model
python morris-minor.py --project path/to/firmware --quick Core/Music/arp.c

# Hands-free: auto-write tests that kill the survivors, then verify
python morris-minor.py --project path/to/firmware --auto

# Force the Anthropic API backend
ANTHROPIC_API_KEY=sk-... python morris-minor.py --project path/to/firmware --backend api
```

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
   `RUN_TEST` registrations as JSON; Morris inserts each function before
   `main()` and its runner after `UNITY_BEGIN()`, rebuilds, and runs the suite.
   If the build or tests fail, every touched file is reverted.

---

## Project layout it expects

```
<project>/                 # your firmware project root  (--project)
├── Core/                  # modules under test         (--source-root)
│   ├── Music/arp.c
│   └── IO/wav.c
└── test/                  # host CMake test project    (--test-dir)
    ├── CMakeLists.txt      #   enable_testing(); add_test(...)
    ├── unity/              #   vendored Unity
    ├── test_arp.c          #   main() with UNITY_BEGIN()/RUN_TEST/UNITY_END()
    ├── test_wav.c
    └── build/              #   generated by Morris      (--build-dir)
```

All four path flags are relative to `--project`: `--source-root` (`Core`),
`--test-dir` (`test`), and `--build-dir` (defaults to `test/build`, where Morris
writes the CMake build and reads `compile_commands.json` for discovery).

`--auto` relies on each `test_*.c` having the standard Unity `main()` shape
(`UNITY_BEGIN()` … `RUN_TEST(...)` … `return UNITY_END();`).

---

## Notes for STM32 / embedded C projects

- **It only mutates host-buildable logic.** Morris mutates exactly the `.c`
  files your `test/` build compiles (it reads them from
  `compile_commands.json`). On a typical STM32CubeMX project that's the portable
  logic you've factored out — here `Core/Music/arp.c` and `Core/IO/wav.c`. HAL,
  peripheral drivers, `main.c`, `Drivers/`, `Middlewares/`, and the USB stack
  aren't in the host build, so they're skipped automatically. To widen coverage,
  extract more hardware-independent modules and add them to `test/CMakeLists.txt`
  — with stubs for their hardware surface (e.g. a fake FatFs so a WAV parser can
  be tested against an in-memory file).

- **Host compiler, not the ARM cross-compiler.** The host suite builds for *your*
  machine, so you need native `gcc`/`clang`/MSVC — **not** `arm-none-eabi-gcc`.

- **`--source-root Core` matches the CubeMX convention** (application code under
  `Core/`). Override it if your project keeps its logic elsewhere.

- **It mirrors your CI.** Morris runs the same
  `cmake -S test -B test/build -G Ninja` → `cmake --build` → `ctest` sequence a
  typical host-test CI job uses, so a clean Morris run reproduces CI locally
  before you push.

- **Deterministic tests only.** Mutation testing assumes repeatable pass/fail.
  Code with randomness or timing should be asserted on invariants or membership
  (e.g. "the output stays within the expected set") rather than an exact value,
  so a mutant isn't flagged inconsistently between runs.

- **Host-side, not on-target.** Morris exercises the host unit tests on your
  machine. It does not build, flash, or test on the MCU — on-target/hardware
  testing is out of scope.

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
