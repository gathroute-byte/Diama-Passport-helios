# HelioS / SEL-CCI six-phase back-test

**Purpose:** compare M0 (flat), M1 (six-phase, no interaction), and M2 (six-phase with a flowering/fill heat interaction) using out-of-sample district-season crop yields. The package contains no farm/yield observations and does not claim any empirical result. It has been written to be run on a source-documented input panel.

## Important methodological corrections

1. **Phase-specific inputs are compulsory.** The previous code skeleton reused one annual `land`, `crop`, `climate`, and `moisture` value in every phase. With that schema, M1 is algebraically just a different flat weighting of the four annual inputs; there is no phase-timing information in the score. This package requires all 24 features (four inputs in each of six phases), each built from that phase's observations.
2. **No held-out-year outcome leakage.** The bottom-tercile loss cutoff is learned from training years only and applied unchanged to the held-out year. It is not computed from the held-out year's yield distribution.
3. **Nested calibration.** `lambda_flow`, `lambda_fill`, `Tcrit`, and `VPD_low` are chosen using inner leave-one-training-year-out folds only. The outer held-out year is not used to tune parameters.
4. **Scores are not yield forecasts.** The code reports rank correlation and loss-event AUC. It does not calculate yield-unit RMSE/MAE because a 0–100 score is not a yield prediction. The source links in `sources_and_data_plan.md` were checked as public starting points, but no source data have been downloaded or joined by this release. A separate training-only yield calibration layer would be required to report those metrics.
5. **This is not a data acquisition service.** It does not download climate data, raster products, protected microdata, or district polygons. It provides the reproducible aggregation and analysis pipeline once the data have been legitimately acquired and harmonized.

## Files

- `helios_backtest.py` — panel aggregation, score-generation functions, nested temporal CV, AUC/Spearman outputs, threshold/lambda sensitivity, bootstrap intervals, hash manifest, and secondary leave-one-district-out check.
- `requirements.txt` — pinned Python dependencies.
- `config.example.json` — exploratory parameter grid and pre-registered decision thresholds.
- `source_manifest.example.json` — source lineage template.
- `data_templates/season_panel_template.csv` — one row per crop × district × crop-season.
- `data_templates/district_daily_template.csv` — one row per crop × district × date.
- `sources_and_data_plan.md` — verified public data starting points and limitations.
- `requirements-dev.txt` — test-only extras (`pytest`, `httpx`).
- `tests/` — synthetic smoke tests only. Synthetic test data are not used as crop-performance evidence.

## Environment setup

Use Python 3.11 or 3.12 in a clean virtual environment. From this directory:

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt   # adds pytest and httpx for the tests
pytest -q
```

## Input data contract

### Season panel

One row per `season_id` (or unique crop + district + year + season). Required identity/outcome fields:

- `crop`: for example `maize`, `common_bean`, or `coffee`.
- `district`, `year`, and preferably `season` / unique `season_id`.
- `yield` and, preferably, `yield_anomaly`. If `yield_anomaly` is supplied, it must be computed from a documented historical baseline that does not use future years.
- Start and end dates for **all six phases**: `{phase}_start`, `{phase}_end`, e.g. `flowering_start`, `flowering_end`. Intervals are start-inclusive/end-exclusive.

The six phase windows must be specified before examining the held-out outcomes. `stress_exposure` is not a standard standalone phenological stage. By default, windows must not overlap. If it is deliberately an overlay on other stages, document why and pass `--allow-overlapping-phases`; do not silently double-count a window.

### Daily feature table

One row per `crop`, `district`, `date`, spatially aggregated to district-day. It must contain these four source-mapped numerical series:

- `land_raw`: an externally sourced land stability/suitability measure. Static values can be repeated through dates.
- `crop_raw`: a crop-condition measure such as an appropriately scaled NDVI/EVI observation. It may be sparse on non-observation dates; no gap filling is done here.
- `climate_raw`: e.g. Tmax in °C or a documented climate-stress index. Default direction assumes larger means worse because it assumes the raw series is Tmax.
- `moisture_raw`: root-zone soil moisture or a documented rainfall/moisture anomaly. The direction default assumes larger means more favorable; change in code/config if your chosen measure has different semantics.

It must also contain `tmax_c` and either `vpd_kpa` or a precomputed, source-documented boolean `soil_moisture_deficit`. If the daily data contain multiple pixels per district, aggregate them first using documented area weights / QA masks. The script intentionally errors on duplicate crop/district/date rows rather than treating pixels as independent observations.

The pipeline constructs the 24 phase-specific features by averaging source-mapped daily measures inside each documented window, then scales each raw variable to 0–100 with training-fold 5th/95th percentiles. The direction is applied so that **100 means more favorable conditions**. This is a consistent, reproducible statistical normalization, not a physiological calibration or an assertion that a land score, NDVI, Tmax, and soil moisture are interchangeable.

For exact transforms and source metadata, fill `source_manifest.example.json` and keep source files immutable. Do not use raw rainfall totals as a moisture score without documenting seasonal climatology, anomaly baseline, and waterlogging treatment.

## Run commands

### 1. Build phase-specific raw features

```bash
python helios_backtest.py build-panel \
  --seasons data/season_panel.csv \
  --daily data/district_daily.csv \
  --out intermediate/phase_features
```

This writes CSV and, if `pyarrow` is available, Parquet. For the default builder, daily field names must be `land_raw`, `crop_raw`, `climate_raw`, and `moisture_raw`. Rename/source-map columns beforehand and record the mapping in the source manifest.

### 2. Run primary leave-one-year-out test

```bash
python helios_backtest.py run \
  --panel intermediate/phase_features.csv \
  --daily data/district_daily.csv \
  --target yield_anomaly \
  --grid config.example.json \
  --source-manifest source_manifest.json \
  --out outputs/maize_beans_coffee
```

For a raw `yield` target, set `--target yield`. That is allowed for a first smoke test, but less satisfactory: persistent district differences may inflate cross-sectional rank skill. Use a causally calculated yield anomaly when possible.

The script runs each crop independently. It does **not** mix maize, beans, and coffee in one calibration. Phase weights remain fixed and identical for all crops; interaction parameters are calibrated inside the training folds for each crop. The results therefore test the fixed phase architecture with crop-specific interaction calibration, not whether one universal set of physiological thresholds fits all crops.

### 3. Add the secondary spatial check

Add `--run-district-holdout`. This produces a separate directory of leave-one-district-out outputs. That diagnostic is a spatial-transfer test, not a replacement for the primary temporal holdout.

## Outputs

- `yearly_results.csv` / `.parquet`: year-by-year M0/M1/M2 rank correlations and AUC, lifts (including the `M1-M0` phase-weighting and `M2-M1` interaction decompositions), selected fold parameters, loss threshold learned from training only.
- `summary.csv`: mean rank lift, circular moving-block bootstrap interval, fraction of years with positive lift, AUC lift, sensitivity robustness summary, and explicit verdict.
- `threshold_sensitivity.csv`: held-out lift for tested Tmax/VPD thresholds while holding that outer fold's selected lambdas fixed. Threshold settings are *not* selected using held-out performance.
- `lambda_sensitivity.csv`: held-out lift for equal flowering/fill lambda values with each fold's selected thresholds frozen.
- `heldout_predictions.csv`: row-level held-out scores and loss labels for audit.
- `nested_parameter_search.csv`: inner-CV parameter comparisons for every outer fold.
- `model_manifest.json`: version, runtime versions, exact weights, selected parameters, data hashes and SHA-256 hashes.
- `report.md`: generated summary, per-year results, decision rule and cautions.

## Success/falsification rule

Default rule follows the supplied pre-registration: mean out-of-sample Spearman lift of at least +0.03, positive lift in at least 70% of held-out years, lower 95% moving-block interval above zero, and positive mean lift for at least 60% of threshold settings. The +0.05 lift threshold is separately flagged. These are **pre-registered decision thresholds**, not literature-derived expected effects. Report the complete sensitivity tables even if the verdict is negative.

A 95% interval over years is unreliable with few unique years. Treat the default moving-block length as a declared heuristic and test alternatives. A minimum of 400 district-seasons is not, by itself, a power calculation; event frequency, number of independent years/districts, correlation structure and spatial coverage matter.

## Important boundary

A result from this script is an out-of-sample association/skill test, not proof that the interaction is causal, not proof that the chosen thresholds are physiologically universal, and not an actuarial premium or probability-of-default calibration. Do not describe the model as validated until the input sources, phenology windows, leakage controls and results have been independently reviewed.
