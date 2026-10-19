# DIAMA HelioS: Execution Report (2026-10-19)

Four things are kept separate below: (a) source-code tests, (b) deployment, (c) real-data availability, (d) scientific performance. **No scientific performance evidence exists. No service is deployed. No repository was created.**

## (a) Source recovery, audit and tests

**Recovered:** all 14 embedded `FILE:` sections (14 headers in the handoff, 14 written) with exact relative paths; root contains `Dockerfile`, `cloud_app.py`, `requirements.txt`, `helios_backtest.py`, `render.yaml`.

**Defects found and fixed (model logic, weights, interaction definition and verdict rules unchanged):**
1. `build_report` used `DataFrame.to_markdown`, which needs `tabulate`, absent from `requirements.txt`. A clean Docker image would have failed every successful `/run` with a 422. Replaced by a dependency-free table writer; tested with `tabulate` blocked.
2. M2−M1 (interaction) was never reported, so M2−M0 conflated phase weighting and interaction. Added `lift_rho_M2_minus_M1`, `lift_auc_M1_minus_M0`, `lift_auc_M2_minus_M1` per year, plus summary columns with block-bootstrap intervals and the list of negative-lift years. The inner-CV argmax is unaffected, because M1−M0 does not depend on the candidate parameters.
3. Hard-coded sample rules (≥4 held-out years, ≥3 rows) are now `GridConfig` fields and the report labels them pre-registered design choices.
4. Per-phase observed-day coverage columns are added to the panel (no gap filling anywhere). A warning is logged if phase windows are not chronological in the declared order.
5. `cloud_app.py`: constant-time token comparison (`hmac.compare_digest`); tokens under 16 chars treated as unconfigured; blocking subprocess moved off the event loop; 900 s timeout now returns 504 instead of an unhandled 500; one job at a time (429 otherwise, sized for a 512 MB free instance); `target` validated as a plain column name (prevents option injection such as `--grid`); `source_manifest` capped at 256 KB; subprocess output trimmed and temp paths masked; temp dirs removed on every error path including cancellation; interactive docs/OpenAPI disabled; `/health` reports `auth_configured` (boolean only).
6. `Dockerfile` now runs as a non-root user. `render.yaml` uses `generateValue: true` so the secret is created on the host and never passes through chat or source control.
7. Added the missing `config.example.json` and `source_manifest.example.json` (the README referred to them; they did not exist), `requirements-dev.txt` (pytest and httpx, which the original tests needed but did not declare) and `.gitignore`.

**Tests actually run in this session**, under Python 3.12.3 with numpy 2.4.4, pandas 3.0.2, scipy 1.17.1, scikit-learn 1.8.0. These are **not** the versions pinned in `requirements.txt`. Command: a small shim runner (pytest is not installed here) calling each function in `tests/test_backtest_synthetic.py`:

```
test_duplicate_daily_rows_rejected                        PASS
test_full_run_outputs_decomposition_and_no_tabulate_needed PASS (10.0 s)
test_heldout_year_outcomes_do_not_change_its_own_prediction PASS (20.6 s)
test_overlapping_phases_rejected_by_default               PASS
test_phase_features_are_phase_specific_and_have_coverage  PASS
test_scaler_uses_training_rows_only                       PASS
6 passed 0 failed
```

**Not run:** `tests/test_cloud_app.py` (3 original + 5 added tests). `fastapi`, `starlette`, `httpx` and `pytest` are not installed in this sandbox and it has no network, so the HTTP layer, including every `cloud_app.py` change above, is **compile-checked only (`py_compile`), never executed**. The earlier "3 passed" in the handoff was not reproducible here.

**What the tests do not prove:** the synthetic data are random numbers. The tests check that code paths run and that leakage guards hold (scaler and loss cutoff fit on training years only; changing only a held-out year's outcomes does not change its scores). They say nothing about whether the crop hypothesis is true. A synthetic end-to-end run gave a "NOT SUPPORTED" verdict, as expected for noise.

**Dependency pins:** `uvicorn==0.48.0` (released 2026-05-24) and `python-multipart==0.0.29` (2026-05-17) were confirmed to exist. `fastapi==0.128.2`, `numpy==2.3.5` and `pandas==2.2.3` were not independently confirmed; the first Docker build will test them. Performance note: the default grid on the full model is slow in pure Python; the full-grid synthetic run took ~67 s for 56 rows, so a ~400-row panel needs timing before relying on the 900 s limit.

## (b) GitHub and Render

- **GitHub: blocked, nothing created.** This session has no GitHub tool, and the connector registry search returned no official GitHub connector. I therefore could not check whether `gathroute-byte/Helios_runner_backside` exists, nor create or push to it. There is no commit hash. The previous 404 remains unresolved.
- **Render: connected, nothing created.** Workspace "My Workspace" (`gathroute@gmail.com`) is reachable; its service list is empty. Render's current docs describe a $0 Free web-service instance (512 MB, spins down after 15 minutes idle, 750 free hours per month per workspace), so a free deployment looks possible. Creating a service requires a Git repository, which does not exist yet. No service, no URL, no `/health` result, no cost incurred, no secret created.

## (c) Real-data availability (desk research only)

No data file was downloaded, hashed or joined. Findings and gaps are in the addendum of `sources_and_data_plan.md`, with retrieval metadata in `research_manifest.json`. In short: NISR SAS appears to publish district-by-crop-by-season yield tables (Season A 2025 across all 30 districts; xlsx and PDF); NASA POWER offers daily Tmax and RH but at coarse native resolution, so districts share cells. Licence terms for NISR were not read.

## (d) Scientific result: **NOT EVALUABLE**

No harmonized panel exists. Even with the yield and weather downloads, no source found provides district-season dates for the six phases, crop-specific vegetation, or a time-varying land index, and the Tmax threshold range may rarely be reached in highland districts. No M0/M1/M2 figures are reported because none were produced from real data. Nothing here supports or refutes the heat/low-VPD hypothesis.

## Outstanding blockers

1. **GitHub write access**, the only thing stopping repository creation and the Render deploy. Either connect an official GitHub integration to this Claude session, or run this package from Claude Code in a terminal that is logged in to the `gathroute-byte` account. After that, I would create the private repo, push, and create the free Render service without further input.
2. **Network-enabled execution** for downloading NISR/NASA POWER files and hashing them.
3. **A scientific decision:** a documented, pre-outcome source or rule for the six phase windows, and a crop mask, before the back-test can be called evaluable.
