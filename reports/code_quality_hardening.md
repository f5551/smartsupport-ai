# Task 04 — Code quality hardening

## Scope and evidence

This pass changed only active code and tests. Historical evidence under `reports/evidence` was not edited. Ruff's 14 semantic findings were reviewed: 13 unused or duplicate test imports/fixtures were corrected, while the sole remaining F841 at `reports/evidence/performance_harness.py:170` is in historical evidence. The active source and tests pass `ruff check --select F401,F811,F841 --exclude reports/evidence`.

Ruff reported 178 findings before this pass and 150 afterward across the full tree. The remaining 149 nonsemantic findings are mostly preexisting import order, broad exception, and style diagnostics. Broad automated rewrites were avoided. Imports in the files touched for resource cleanup were sorted mechanically.

## Bandit review

The production scan has 10 Low findings (unchanged), no Medium or High findings. All were reviewed; none warrants an insecure workaround:

| Finding | Count | Review |
| --- | ---: | --- |
| B105 | 3 | UI text or synthetic marker strings; no hardcoded account or API password. |
| B110 | 3 | `mcp_server.py` marks an ambiguous write outcome as unknown and raises; `web_runtime.py` denies Live Mode on settings read failure. These are fail closed paths. |
| B404/B603/B607 | 4 | `release_check.py` invokes fixed Git argument arrays in a disposable index and checks return codes where required; no shell invocation or user supplied command. |

The focused Bandit test scan retains four Medium findings: two B310 calls in `test_deployment.py` fetch only a local loopback test server, and two B104 assertions in `test_runtime_policy.py` and `test_web_runtime.py` explicitly reject `0.0.0.0`. These are test fixtures, not exposed listeners or unrestricted URL fetches. The scan also reports Low test findings, primarily assertions. No findings were suppressed from the scan.

## Resource warnings and regression

With Python 3.13 and `PYTHONTRACEMALLOC=1`, four remaining ResourceWarnings were traced to `with sqlite3.connect(...)` in `test_auth_store.py`, `test_mcp_integration.py`, and `test_phase5_security.py`. The SQLite connection context manager commits or rolls back but does not close; these call sites now use `contextlib.closing`. Earlier in this pass, the same error was corrected in other test helpers and the `web_runtime.live_allowed` settings read, including its exceptional path. A regression test confirms the Live Mode gate closes its connection when the settings query fails. A focused rerun of 91 tests produced zero warnings.

## Verification

The final full run completed with **408 passed, 0 failed, 2 skipped, 0 warnings**. Coverage was **87.18%** (2645/3034 statements), above the 87.15% baseline. Mypy passed on 22 source files. `release_check.py` scanned 189 distributable text files with no findings, `pip check` found no broken requirements, and `git diff --check` exited successfully. The two skipped tests remain the opt-in paid OpenAI test and the Linux runner test; neither was treated as passing.

Ruff still reports 150 findings across the whole Python tree, including the one historical F841. The scanner also noted access denied in a local `reports/evidence/ui_fixture` directory; this did not alter the count returned for accessible Python files. Ruff semantic checks on active source and tests pass. The remaining style findings are not claimed to be fixed.
