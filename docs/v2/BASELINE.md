# InsightPilot V2: P0 baseline

P0 adds tests, fixtures, documentation and a feature-flag module that the app does not read. **No application code was changed**: none of `agent/`, `sandbox/`, `utils/`, `ui/`, `rules/`, `skills/`, `data/`, `requirements.txt` or `README.md`.

## Reference commits

| Ref | SHA | Notes |
|---|---|---|
| `main` | `fd5783c6202e3ea506a69e607160617a23eed807` | Examination code; must never change |
| `exam-backup-2026-11-17` | `fd5783c6202e3ea506a69e607160617a23eed807` | Read-only preservation snapshot |
| `feature/insightpilot-v2` | `86ac707a415acece9c7592861e3380972533b87f` | `main` + `CLAUDE.md` |
| `feature/v2-p0-baseline` | based on `86ac707a…` | This phase |

## Results

| Environment | Python | Key packages | Result |
|---|---|---|---|
| **A** Linux container, latest dependencies from unpinned `requirements.txt` | 3.13.18 | langgraph 1.2.22, langchain 1.5.0, pandas 3.1.1, streamlit 1.67.0 | 200 existing: **200 passed**. Full suite: **270 passed, 13 xfailed, 0 failed** |
| **B** Linux container, 100 packages pinned to the exact versions in the committed macOS `venv/` | 3.11.18 | langgraph 1.2.9, langchain 1.3.14, pandas 3.0.3, streamlit 1.59.2 | 200 existing: **200 passed**. Full suite: **270 passed, 13 xfailed, 0 failed** (1 visible harness warning, see below) |
| **C** Examination MacBook, macOS Monterey, committed `venv/` | 3.11.9 (python.org framework build) | as B | **Not verified.** See the checklist at the end |

The 13 xfailed tests are deliberate: they are the confirmed V1 security gaps listed further down.

Environment B reproduces the exam machine's *package versions*. It does **not** reproduce macOS, the framework Python build, the macOS wheels (`.dylib`), file-system semantics or fonts. B is evidence, not equivalence.

## Commands

Run from the repository root. All runs are offline; no API key is needed or used.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q -p no:cacheprovider
```

- V2 tests only: `python -m pytest tests/v2 -q -p no:cacheprovider`
- The 200 original tests only: `python -m pytest tests -q -p no:cacheprovider --ignore=tests/v2`
- `PYTHONDONTWRITEBYTECODE=1` stops the run from rewriting the `.pyc` files that are committed under `__pycache__/`.

## What the new tests cover

| File | Tests | Covers |
|---|---|---|
| `tests/v2/test_feature_flags.py` | 28 | Flags default to V1; strict parsing; typos rejected; secure profile refuses non-isolated execution; no silent fallback to the legacy sandbox; flags not imported by any V1 module |
| `tests/v2/test_golden_reference.py` | 16 | Demo datasets byte-identical (SHA-256); committed answers = fresh standard-library computation = separate pandas computation; V1 data-quality report = independent reference |
| `tests/v2/test_e2e_golden.py` | 13 | Full unmodified LangGraph pipeline on all 4 datasets with real sandbox execution; self-correction retry; blocked unsafe code; reflection lessons; charts, Markdown, PDF and notebook; safe-mode pause; follow-up chat; context compression; V1 snapshots |
| `tests/v2/test_smoke_ui.py` | 4 | Real `ui/app.py` via Streamlit AppTest: load, run, approve, edit plan, discard plan, the UI's own post-approval loop, all 7 results pages, follow-up chat, V1 snapshot of the UI path |
| `tests/v2/test_v1_security_baseline.py` | 22 | 13 strict-xfail gap confirmations plus 9 checks of V1 controls that do work |

## Golden-dataset harness

- **Independent answers.** `tests/v2/golden/reference.py` computes 28 analysis answers (7 per dataset) and 20 data-quality values (5 per dataset) using only `csv`, `statistics` and `math`.
- **Cross-check.** `test_golden_reference.py` recomputes every answer separately with pandas. The three sources must agree: committed JSON, fresh standard-library run, pandas.
- **End to end.** In the pipeline tests, the code that runs in the sandbox prints `GOLDEN key=value` lines. Every one of the 28 answers must be produced and must equal the independent value (relative tolerance 1e-9, absolute 1e-6).
- **Snapshots.** `tests/v2/golden/snapshots/*.json` pin V1 behaviour: task statuses, retries, error types, charts, report headings, the execution-log sequence and LLM calls per skill. There is one snapshot per dataset for the graph path, plus `sales_ui_approve_safe_mode_on` for the UI path. Later phases must match them with all flags off. Regenerate only with reviewer approval:

  ```bash
  IP_UPDATE_GOLDEN=1 python -m pytest tests/v2 -q -p no:cacheprovider
  ```

### Datasets

| Dataset | Rows | SHA-256 (prefix) | Golden answers |
|---|---|---|---|
| `sales_data.csv` | 293 | `c5ac4066…` | row count, total sales, sales by region, top region, returns, mean satisfaction, target attainment |
| `attendance_data.csv` | 15,600 | `2fe0b859…` | row count, status counts, present rate, mean hours, hours by department, top department, GPS mismatches |
| `student_data.csv` | 2,500 | `3259adb0…` | row count, pass rate, grade-letter counts, mean score, score by subject, top subject, study-hours/score correlation |
| `ecommerce_timeseries.csv` | 730 | `102eccf6…` | row count, total revenue, total orders, weekend and weekday mean revenue, top month, visitors/revenue correlation |

### What this does not prove

The analysis code is hand-written and stands in for the LLM. The harness proves that the pipeline carries correct numbers end to end, and that the numbers really are correct. It does **not** measure LLM answer quality.

Token figures in the snapshots' `informational` section are character-based estimates of *mock* prompts. They are **not provider tokens**, are not gated, and must never be reported as cost or savings. The live-LLM budget is **0**; no paid call was made.

## V1 security baseline (confirmed by real runs)

Each row is a `strict=True, raises=AssertionError` xfail test, so it only counts when the insecure behaviour is actually observed. When a phase fixes a gap, the test passes unexpectedly, strict mode turns that into a failure, and the marker must be removed deliberately.

| ID | Gap confirmed in V1 | Planned fix |
|---|---|---|
| S1 | Generated code reads the parent's environment, including `GOOGLE_API_KEY` (a dummy value in the test) | P3 |
| S2 | The AST denylist misses `pd.io.common.os`, `getattr(__builtins__, …)`, `__subclasses__`, `pathlib`, `import builtins` (5 tests) | P3 (real isolation, not AST alone) |
| S3 | The dataset path is interpolated into the unscanned wrapper; a crafted file name ending in `.csv` executed code | P3 |
| S4 | A task id from the LLM (`../escaped`) writes the chart outside the charts folder | P2/P3 |
| S5 | No memory limit (a 300 MB allocation succeeded) | P3 |
| FS-read | Generated code read a file outside the dataset | P3 |
| FS-write | Generated code wrote a file outside the chart folder | P3 |
| NET | Generated code opened a connection (to a loopback listener owned by the test) | P3 |
| S7 | The graph continues to `generate_code` after `await_approval`; approval is enforced only by the UI | P8 |

V1 controls that do work and are kept passing: direct `import os/subprocess/socket/shutil`, `eval`, `exec`, `open` and `__import__` are blocked before execution; generated code that keeps violating the scanner fails after the retry limit while the run continues; the 30-second timeout is unchanged.

## Platform differences recorded

1. **Streamlit test harness defect on 1.59.2 (exam version).** After **Approve and run** (which calls `st.rerun()`), AppTest keeps the approval page's un-keyed `text_area` and raises `KeyError('$$ID-…')` on the next run. The application state is correct. Streamlit 1.67.0 does not do this. `tests/v2/test_smoke_ui.py::_continue_session` handles exactly this error by restarting the harness with the same session state, and emits a `UserWarning`. A real browser on the exam Mac still needs to be checked (checklist item 4).
2. **Unpinned `requirements.txt`.** A fresh install today resolves to different versions than the exam `venv/` (for example streamlit 1.67.0 instead of 1.59.2). The app uses `use_container_width`, which Streamlit deprecates with removal announced "after 2025-12-31". **Do not reinstall dependencies on the exam machine.** Pinning is deferred to a reviewed phase; `requirements.txt` is unchanged.
3. **LangGraph recursion limit.** Both 1.2.9 and 1.2.22 default to 10007 (older 0.2.x releases used 25). With safe mode off and many retries, an older LangGraph could stop the graph early. That's not a risk with the pinned versions.
4. **Committed `venv/`.** It contains macOS binaries and a `pyvenv.cfg` pointing to a user home path. It cannot run on Linux, so environment B uses equivalent pins rather than the same files. Cleanup is deferred per review.
5. **Not yet compared on macOS:**
   - `resource`-limit semantics (relevant to P3)
   - `/tmp` → `/private/tmp`
   - the case-insensitive default file system
   - fonts and ReportLab output (charts and PDFs are checked for validity only, never compared pixel by pixel)
   - `fork` vs `spawn` defaults
6. **Test hygiene (existing suite).** The original 200 tests write `reports/Test_Dataset_*` and `reports/charts/t_*.png` into the repository's git-ignored `reports/` folder. Nothing is tracked, but running them on the exam machine adds or overwrites those 5 files there. The V2 tests write only to pytest temp directories.

## Privacy

- No real or personal data is used: the demo datasets are synthetic (`data/generate_datasets.py`, fixed seed) and are only read.
- Test payloads use a dummy secret value. `GOOGLE_API_KEY` is removed from the environment for every V2 test, including the sandbox subprocesses, which inherit it.
- The real Gemini client class is replaced by one that raises, and in-process sockets may only reach loopback. This socket guard does not cover the sandbox subprocess; that isolation is part of P3.

## Examination MacBook checklist (read-only)

Run this in a **separate worktree** so the presentation checkout is not touched. Use the existing venv's Python by absolute path, and **do not `pip install` anything**.

`git fetch` and `git worktree` only update remote-tracking refs and the worktree list in `.git`; they don't touch the checked-out files or branches. Step 5 removes the worktree entry again.

1. From inside the presentation checkout, remember its path and record its state:

   ```bash
   EXAM_DIR="$(pwd)"
   ```

   ```bash
   git status --short --branch
   ```

   ```bash
   git rev-parse HEAD
   ```

2. Create the separate worktree. This adds a new folder next to the checkout, about 850 MB because the tracked `venv/` is checked out too:

   ```bash
   git fetch origin feature/v2-p0-baseline
   ```

   ```bash
   git worktree add ../insight-pilot-v2-check origin/feature/v2-p0-baseline
   ```

3. Run the suite from the worktree with the presentation venv:

   ```bash
   cd ../insight-pilot-v2-check && PYTHONDONTWRITEBYTECODE=1 "$EXAM_DIR/venv/bin/python" -m pytest tests -q -p no:cacheprovider
   ```

   Record the pass, fail and xfail counts and any errors. Expected: 270 passed, 13 xfailed.

4. In the presentation checkout, run `streamlit run ui/app.py` and do one manual demo with a sample dataset: approve, view all pages, ask a follow-up. This confirms item 1 above in a real browser. It uses the real API key and makes a small number of real Gemini calls; do it only if you accept that.

5. Go back to the presentation checkout, remove the worktree, then confirm `git status` and `HEAD` match step 1:

   ```bash
   cd "$EXAM_DIR" && git worktree remove ../insight-pilot-v2-check
   ```
