"""Reproducible HelioS / SEL-CCI phase-score back-test.

No outcome data are bundled. This program reports results only for a user-supplied,
source-documented panel and daily weather table. Synthetic data are used only by unit tests.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import math
import platform
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import scipy
from scipy.stats import spearmanr
import sklearn
from sklearn.metrics import roc_auc_score

LOGGER = logging.getLogger("helios_backtest")

VERSION = "helios-v0.9-int-flower-fill"
PHASES = (
    "establishment",
    "vegetative_growth",
    "flowering",
    "stress_exposure",
    "fill",
    "harvest",
)
VARIABLES = ("land", "crop", "climate", "moisture")
WEIGHTS: dict[str, dict[str, float]] = {
    "establishment":     {"land": 0.40, "crop": 0.30, "climate": 0.10, "moisture": 0.20},
    "vegetative_growth": {"land": 0.20, "crop": 0.40, "climate": 0.30, "moisture": 0.10},
    "flowering":         {"land": 0.10, "crop": 0.40, "climate": 0.40, "moisture": 0.10},
    "stress_exposure":   {"land": 0.20, "crop": 0.20, "climate": 0.40, "moisture": 0.20},
    "fill":              {"land": 0.10, "crop": 0.30, "climate": 0.10, "moisture": 0.50},
    "harvest":           {"land": 0.40, "crop": 0.40, "climate": 0.10, "moisture": 0.10},
}
FLAT_WEIGHTS: dict[str, float] = {"land": 0.30, "crop": 0.40, "climate": 0.20, "moisture": 0.10}
DEFAULT_DIRECTION = {"land": "higher", "crop": "higher", "climate": "lower", "moisture": "higher"}
PHASE_DATE_COLS = {p: (f"{p}_start", f"{p}_end") for p in PHASES}
OUTCOME_CANDIDATES = ("yield_anomaly", "yield")
ID_COLS = ("crop", "district", "year")


@dataclass(frozen=True)
class GridConfig:
    lambda_values: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8)
    tcrit_values: tuple[float, ...] = (31.0, 32.0, 33.0, 34.0)
    vpd_low_values: tuple[float, ...] = (0.8, 0.9, 1.0, 1.1)
    scale_q_low: float = 0.05
    scale_q_high: float = 0.95
    min_inner_years: int = 3
    # Pre-registered DESIGN CHOICES (not established scientific thresholds):
    min_heldout_years: int = 4
    min_test_rows: int = 3
    bootstrap_reps: int = 2000
    seed: int = 20261019
    min_mean_rho_lift: float = 0.03
    min_positive_year_fraction: float = 0.70
    min_ci_lower: float = 0.0
    min_sensitivity_fraction_positive: float = 0.60
    directions: dict[str, str] = field(default_factory=lambda: DEFAULT_DIRECTION.copy())


@dataclass(frozen=True)
class InteractionParams:
    lambda_flow: float
    lambda_fill: float
    tcrit_c: float
    vpd_low_kpa: float | None
    mode: str


def _md_table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    """Dependency-free GitHub-markdown table (avoids the optional `tabulate` package)."""
    def cell(v: Any) -> str:
        if isinstance(v, (float, np.floating)):
            return "" if not np.isfinite(v) else format(float(v), floatfmt)
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
        return str(v).replace("|", "\\|").replace("\n", " ")
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path)
    raise ValueError(f"Unsupported file extension for {path}; use CSV or Parquet.")


def save_table(df: pd.DataFrame, path_stem: str | Path) -> list[str]:
    """Write a CSV always and Parquet when the configured runtime supports it."""
    stem = Path(path_stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    csv_path = stem.with_suffix(".csv")
    df.to_csv(csv_path, index=False)
    outputs.append(str(csv_path))
    try:
        parquet_path = stem.with_suffix(".parquet")
        df.to_parquet(parquet_path, index=False)
        outputs.append(str(parquet_path))
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        LOGGER.warning("Parquet export skipped (%s). CSV was written.", exc)
    return outputs


def ensure_season_id(seasons: pd.DataFrame) -> pd.DataFrame:
    out = seasons.copy()
    if "season_id" not in out.columns:
        missing = [c for c in ("crop", "district", "year") if c not in out.columns]
        if missing:
            raise ValueError(f"Season metadata missing identity fields: {missing}")
        season_label = out["season"].astype(str) if "season" in out else pd.Series("season-1", index=out.index)
        out["season_id"] = (
            out["crop"].astype(str) + "|" + out["district"].astype(str) + "|" +
            out["year"].astype(str) + "|" + season_label.astype(str)
        )
    if out["season_id"].astype(str).duplicated().any():
        dup = out.loc[out["season_id"].astype(str).duplicated(keep=False), "season_id"].head(5).tolist()
        raise ValueError(f"season_id must uniquely identify a crop-district-season row. Example duplicates: {dup}")
    out["season_id"] = out["season_id"].astype(str)
    return out


def _date_col_as_dates(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out:
            out[c] = pd.to_datetime(out[c], errors="coerce")
    return out


def validate_seasons(seasons: pd.DataFrame, allow_overlapping_phases: bool = False) -> pd.DataFrame:
    out = ensure_season_id(seasons)
    required = ["season_id", "crop", "district", "year"]
    missing = [c for c in required if c not in out]
    if missing:
        raise ValueError(f"Season table missing required columns: {missing}")
    date_cols = [c for pair in PHASE_DATE_COLS.values() for c in pair]
    missing_dates = [c for c in date_cols if c not in out.columns]
    if missing_dates:
        raise ValueError(
            "All six phase windows must be supplied explicitly. Missing date columns: "
            f"{missing_dates}. Do not infer 'stress_exposure' timing without a documented rule."
        )
    out = _date_col_as_dates(out, date_cols)
    invalid_examples = []
    for idx, row in out.iterrows():
        intervals: list[tuple[pd.Timestamp, pd.Timestamp, str]] = []
        for phase, (scol, ecol) in PHASE_DATE_COLS.items():
            s, e = row[scol], row[ecol]
            if pd.isna(s) or pd.isna(e) or e <= s:
                invalid_examples.append((row["season_id"], phase, str(s), str(e)))
            else:
                intervals.append((s, e, phase))
        declared_order = [(s, p) for p, (sc, ec) in PHASE_DATE_COLS.items()
                          for s in [row[sc]] if not pd.isna(s)]
        if [p for _, p in declared_order] != [p for _, p in sorted(declared_order, key=lambda x: x[0])]:
            LOGGER.warning("season_id=%s: phase windows are not in the declared order %s; confirm timing was fixed before outcomes were examined.",
                           row["season_id"], list(PHASES))
        if not allow_overlapping_phases:
            intervals.sort(key=lambda x: x[0])
            for left, right in zip(intervals[:-1], intervals[1:]):
                if right[0] < left[1]:
                    raise ValueError(
                        f"Overlapping phase windows for season_id={row['season_id']}: "
                        f"{left[2]} [{left[0].date()}, {left[1].date()}) and "
                        f"{right[2]} [{right[0].date()}, {right[1].date()}). "
                        "Reconcile the windows or opt in to overlap explicitly. "
                        "'stress_exposure' is not automatically a phenological stage."
                    )
    if invalid_examples:
        raise ValueError(f"Invalid/missing start-end phase dates; examples: {invalid_examples[:5]}")
    if out["year"].isna().any():
        raise ValueError("year contains missing values")
    out["year"] = pd.to_numeric(out["year"], errors="raise").astype(int)
    return out


def aggregate_daily_to_phase_features(
    seasons: pd.DataFrame,
    daily: pd.DataFrame,
    *,
    raw_columns: dict[str, str] | None = None,
    allow_overlapping_phases: bool = False,
) -> pd.DataFrame:
    """Aggregate four raw input series to the six declared phase windows.

    `land_raw`, `crop_raw`, `climate_raw`, and `moisture_raw` must be source-mapped,
    numeric measures. Recommended mappings: land suitability/stability; NDVI/EVI;
    Tmax or a documented climate-stress index; root-zone soil moisture or a documented
    rainfall/moisture anomaly. Scaling to 0-100 is fitted only inside each training fold.
    Intervals are start-inclusive/end-exclusive [start, end).
    """
    seasons = validate_seasons(seasons, allow_overlapping_phases=allow_overlapping_phases)
    raw_columns = raw_columns or {v: f"{v}_raw" for v in VARIABLES}
    expected = {"crop", "district", "date", *raw_columns.values()}
    missing = expected.difference(daily.columns)
    if missing:
        raise ValueError(f"Daily table missing columns required to aggregate phase features: {sorted(missing)}")
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    if d["date"].isna().any():
        raise ValueError("Daily table contains invalid dates")
    for col in raw_columns.values():
        d[col] = pd.to_numeric(d[col], errors="coerce")
    # The data contract is one spatially aggregated value per crop/district/day.
    # Refuse silent double-weighting if a raster export still contains multiple pixels.
    key_cols = ["crop", "district", "date"]
    if d.duplicated(key_cols).any():
        raise ValueError(
            "Daily table has duplicate crop/district/date rows. Aggregate gridded data to the "
            "district-day first (document area-weighting and masks); do not let pixels inflate N."
        )
    grouped = {(str(c), str(ds)): g.sort_values("date") for (c, ds), g in d.groupby(["crop", "district"], sort=False)}
    records = []
    for _, season in seasons.iterrows():
        g = grouped.get((str(season["crop"]), str(season["district"])))
        record = {"season_id": season["season_id"]}
        if g is None:
            for phase in PHASES:
                for var in VARIABLES:
                    record[f"{var}_raw__{phase}"] = np.nan
                    record[f"{var}_cov__{phase}"] = 0.0
            records.append(record)
            continue
        for phase, (scol, ecol) in PHASE_DATE_COLS.items():
            mask = (g["date"] >= season[scol]) & (g["date"] < season[ecol])
            pg = g.loc[mask]
            window_days = max(int((season[ecol] - season[scol]).days), 1)
            for var, source_col in raw_columns.items():
                vals = pd.to_numeric(pg[source_col], errors="coerce").dropna()
                record[f"{var}_raw__{phase}"] = float(vals.mean()) if not vals.empty else np.nan
                # Observed-day coverage of the window (no gap filling is performed anywhere).
                record[f"{var}_cov__{phase}"] = float(len(vals)) / window_days
        records.append(record)
    feature_df = pd.DataFrame.from_records(records)
    out = seasons.merge(feature_df, on="season_id", how="left", validate="one_to_one")
    return out


def validate_phase_panel(panel: pd.DataFrame, target_col: str | None = None) -> tuple[pd.DataFrame, str]:
    out = ensure_season_id(panel)
    for col in ("crop", "district", "year"):
        if col not in out:
            raise ValueError(f"Panel missing required identity field {col!r}")
    if target_col is None:
        target_col = next((c for c in OUTCOME_CANDIDATES if c in out.columns), None)
    if target_col is None or target_col not in out:
        raise ValueError("Panel must contain yield_anomaly (preferred) or yield, or pass --target.")
    out[target_col] = pd.to_numeric(out[target_col], errors="coerce")
    if out[target_col].isna().any():
        raise ValueError(f"Outcome column {target_col!r} contains missing/non-numeric values.")
    raw_cols = [f"{v}_raw__{p}" for p in PHASES for v in VARIABLES]
    normalized_cols = [f"{v}__{p}" for p in PHASES for v in VARIABLES]
    if all(c in out for c in raw_cols):
        for c in raw_cols:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        if out[raw_cols].isna().any().any():
            bad = out.loc[out[raw_cols].isna().any(axis=1), "season_id"].head(8).tolist()
            raise ValueError(f"Missing raw phase features (examples {bad}); no silent imputation is applied.")
    elif all(c in out for c in normalized_cols):
        LOGGER.warning("Using pre-normalized features; validate that all 24 inputs are [0,100] and higher means favorable.")
        for c in normalized_cols:
            out[c] = pd.to_numeric(out[c], errors="coerce")
            if out[c].isna().any() or ((out[c] < 0) | (out[c] > 100)).any():
                raise ValueError(f"Normalized feature {c!r} must be complete and in [0,100].")
    else:
        missing = [c for c in raw_cols if c not in out]
        raise ValueError(f"Need all 24 phase-specific raw features or all 24 normalized features; missing examples {missing[:5]}.")
    return out, target_col


def fit_feature_scaler(train: pd.DataFrame, q_low: float = 0.05, q_high: float = 0.95,
                       directions: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Fit pooled-across-phases percentile scaling on training observations only."""
    directions = directions or DEFAULT_DIRECTION
    if not 0 <= q_low < q_high <= 1:
        raise ValueError("Require 0 <= q_low < q_high <= 1")
    scaler: dict[str, dict[str, Any]] = {}
    raw_exists = all(f"{v}_raw__{p}" in train for v in VARIABLES for p in PHASES)
    if not raw_exists:
        return {v: {"low": 0.0, "high": 100.0, "direction": "higher", "already_scaled": True} for v in VARIABLES}
    for var in VARIABLES:
        vals = pd.concat([train[f"{var}_raw__{p}"] for p in PHASES], ignore_index=True).dropna().astype(float)
        if vals.empty:
            raise ValueError(f"No training values for feature {var!r}")
        if directions.get(var) not in {"higher", "lower"}:
            raise ValueError(f"Direction for {var!r} must be 'higher' or 'lower'.")
        scaler[var] = {"low": float(vals.quantile(q_low)), "high": float(vals.quantile(q_high)),
                       "direction": directions[var], "already_scaled": False}
    return scaler


def transform_phase_features(df: pd.DataFrame, scaler: dict[str, dict[str, Any]]) -> pd.DataFrame:
    out = df.copy()
    raw_exists = all(f"{v}_raw__{p}" in out for v in VARIABLES for p in PHASES)
    for var in VARIABLES:
        for phase in PHASES:
            dest = f"{var}__{phase}"
            if not raw_exists:
                out[dest] = pd.to_numeric(out[dest], errors="coerce").clip(0, 100)
                continue
            x = pd.to_numeric(out[f"{var}_raw__{phase}"], errors="coerce")
            lo, hi = float(scaler[var]["low"]), float(scaler[var]["high"])
            values = pd.Series(50.0, index=x.index) if hi <= lo else (x.clip(lo, hi) - lo) / (hi - lo) * 100.0
            if scaler[var]["direction"] == "lower":
                values = 100.0 - values
            out[dest] = values
    required = [f"{v}__{p}" for v in VARIABLES for p in PHASES]
    if out[required].isna().any().any():
        raise ValueError("Feature transform has missing values; resolve before running the test.")
    return out


def score_models(panel_norm: pd.DataFrame, counts: pd.DataFrame | None = None,
                 params: InteractionParams | None = None) -> pd.DataFrame:
    """Compute integer scores. A higher score means conditions are more favorable."""
    out = panel_norm.copy()
    flat = {v: np.mean(np.column_stack([out[f"{v}__{p}"].to_numpy(float) for p in PHASES]), axis=1)
            for v in VARIABLES}
    m0 = sum(FLAT_WEIGHTS[v] * flat[v] for v in VARIABLES)
    phase_scores = {
        p: sum(WEIGHTS[p][v] * out[f"{v}__{p}"].to_numpy(float) for v in VARIABLES)
        for p in PHASES
    }
    m1 = np.mean(np.column_stack([phase_scores[p] for p in PHASES]), axis=1)
    m2 = m1.copy()
    if params is not None:
        if counts is None:
            raise ValueError("M2 requires interaction counts")
        c = counts.copy()
        c["season_id"] = c["season_id"].astype(str)
        c = c.set_index("season_id").reindex(out["season_id"].astype(str))
        if c[["S_int_flow", "S_int_fill"]].isna().any().any():
            raise ValueError("Missing interaction counts for one or more seasons")
        adj = np.column_stack([phase_scores[p] for p in PHASES])
        adj[:, PHASES.index("flowering")] -= params.lambda_flow * c["S_int_flow"].to_numpy(float)
        adj[:, PHASES.index("fill")] -= params.lambda_fill * c["S_int_fill"].to_numpy(float)
        m2 = np.mean(adj, axis=1)
    out["score_M0"] = np.rint(np.clip(m0, 0, 100)).astype(int)
    out["score_M1"] = np.rint(np.clip(m1, 0, 100)).astype(int)
    out["score_M2"] = np.rint(np.clip(m2, 0, 100)).astype(int)
    return out


def detect_interaction_mode(daily: pd.DataFrame, requested: str = "auto") -> str:
    if requested not in {"auto", "vpd", "soil_moisture"}:
        raise ValueError("interaction mode must be auto, vpd, or soil_moisture")
    has_vpd = "vpd_kpa" in daily and daily["vpd_kpa"].notna().any()
    has_flag = "soil_moisture_deficit" in daily and daily["soil_moisture_deficit"].notna().any()
    mode = ("vpd" if has_vpd else "soil_moisture") if requested == "auto" else requested
    if mode == "vpd" and not has_vpd:
        raise ValueError("VPD mode requested but daily data has no usable vpd_kpa column")
    if mode == "soil_moisture" and not has_flag:
        raise ValueError("Need vpd_kpa or a source-documented soil_moisture_deficit flag; no implicit substitution")
    if "tmax_c" not in daily or pd.to_numeric(daily["tmax_c"], errors="coerce").notna().sum() == 0:
        raise ValueError("Daily weather needs tmax_c")
    return mode


def interaction_counts(seasons: pd.DataFrame, daily: pd.DataFrame, *, tcrit_c: float,
                       vpd_low_kpa: float | None, mode: str) -> pd.DataFrame:
    """Count valid daily interactions in flowering/fill windows [start,end)."""
    ss = ensure_season_id(seasons)
    d = daily.copy().reset_index(drop=True)
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["tmax_c"] = pd.to_numeric(d["tmax_c"], errors="coerce")
    if "vpd_kpa" in d:
        d["vpd_kpa"] = pd.to_numeric(d["vpd_kpa"], errors="coerce")
    if d["date"].isna().any():
        raise ValueError("Daily weather has invalid dates")
    if d.duplicated(["crop", "district", "date"]).any():
        raise ValueError("Aggregate weather to one row per crop/district/date; do not treat grid pixels as independent days")
    d["crop"] = d["crop"].astype(str)
    d["district"] = d["district"].astype(str)
    tmax = d["tmax_c"].to_numpy(float)
    dates = d["date"].to_numpy(dtype="datetime64[ns]")
    if mode == "vpd":
        if "vpd_kpa" not in d or vpd_low_kpa is None:
            raise ValueError("VPD mode requires vpd_kpa and a VPD-low threshold")
        vpd = d["vpd_kpa"].to_numpy(float)
        valid_weather = np.isfinite(tmax) & np.isfinite(vpd)
    else:
        if "soil_moisture_deficit" not in d:
            raise ValueError("Soil-moisture mode requires a soil_moisture_deficit boolean flag")
        # Missing deficit status is missing, not a safe/non-stress day.
        flag_series = d["soil_moisture_deficit"].astype("boolean")
        flag = flag_series.fillna(False).astype(bool).to_numpy()
        valid_flag = flag_series.notna().to_numpy()
        valid_weather = np.isfinite(tmax) & valid_flag
    group_indices = {
        (c, district): g.index.to_numpy(dtype=int)
        for (c, district), g in d.groupby(["crop", "district"], sort=False)
    }
    records = []
    for _, season in ss.iterrows():
        key = (str(season["crop"]), str(season["district"]))
        idx = group_indices.get(key, np.array([], dtype=int))
        rec: dict[str, Any] = {"season_id": str(season["season_id"])}
        for phase, dest in (("flowering", "S_int_flow"), ("fill", "S_int_fill")):
            if len(idx) == 0:
                rec[dest] = 0
                rec[f"{dest}_weather_coverage"] = 0.0
                continue
            start, end = season[f"{phase}_start"], season[f"{phase}_end"]
            in_window = (dates[idx] >= np.datetime64(start)) & (dates[idx] < np.datetime64(end))
            px = idx[in_window]
            if len(px) == 0:
                rec[dest] = 0
                rec[f"{dest}_weather_coverage"] = 0.0
                continue
            if mode == "vpd":
                event = (tmax[px] > tcrit_c) & (vpd[px] < float(vpd_low_kpa))
            else:
                event = (tmax[px] > tcrit_c) & flag[px]
            valid = valid_weather[px]
            rec[dest] = int(np.sum(event & valid))
            rec[f"{dest}_weather_coverage"] = float(np.mean(valid))
        records.append(rec)
    return pd.DataFrame(records)


def _rho(a: Iterable[float], b: Iterable[float]) -> float:
    aa, bb = np.asarray(list(a), dtype=float), np.asarray(list(b), dtype=float)
    valid = np.isfinite(aa) & np.isfinite(bb)
    if valid.sum() < 3 or np.std(aa[valid]) == 0 or np.std(bb[valid]) == 0:
        return float("nan")
    val = spearmanr(aa[valid], bb[valid]).statistic
    return float(val) if np.isfinite(val) else float("nan")


def _auc(y_binary: Iterable[int], risk_score: Iterable[float]) -> float:
    y = np.asarray(list(y_binary), dtype=int)
    risk = np.asarray(list(risk_score), dtype=float)
    ok = np.isfinite(y) & np.isfinite(risk)
    if ok.sum() < 3 or len(np.unique(y[ok])) != 2:
        return float("nan")
    try:
        return float(roc_auc_score(y[ok], risk[ok]))
    except ValueError:
        return float("nan")


def _fit_transform_pair(train_raw: pd.DataFrame, test_raw: pd.DataFrame, grid: GridConfig,
                        directions: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    scaler = fit_feature_scaler(train_raw, grid.scale_q_low, grid.scale_q_high, directions)
    return transform_phase_features(train_raw, scaler), transform_phase_features(test_raw, scaler), scaler


def _inner_cv_lift(train_raw: pd.DataFrame, daily: pd.DataFrame, target_col: str,
                   grid: GridConfig, directions: dict[str, str], mode: str
                   ) -> tuple[InteractionParams, float, pd.DataFrame]:
    years = sorted(train_raw["year"].astype(int).unique())
    vpd_candidates: tuple[float | None, ...] = (None,) if mode == "soil_moisture" else tuple(grid.vpd_low_values)
    default = InteractionParams(0.0, 0.0, float(grid.tcrit_values[len(grid.tcrit_values)//2]),
                                None if mode == "soil_moisture" else float(grid.vpd_low_values[len(grid.vpd_low_values)//2]), mode)
    if len(years) < grid.min_inner_years:
        return default, float("nan"), pd.DataFrame([{"note": "insufficient years for nested tuning"}])
    candidates = [InteractionParams(float(lf), float(lfill), float(tc), None if vl is None else float(vl), mode)
                  for lf, lfill, tc, vl in itertools.product(grid.lambda_values, grid.lambda_values,
                                                              grid.tcrit_values, vpd_candidates)]
    # Precompute the inner year test partitions/scalers once; interaction counts depend only on candidate thresholds.
    inner_parts = []
    for val_year in years:
        tr = train_raw[train_raw["year"].astype(int) != val_year].copy()
        va = train_raw[train_raw["year"].astype(int) == val_year].copy()
        if tr.empty or len(va) < 3:
            continue
        try:
            _, va_norm, _ = _fit_transform_pair(tr, va, grid, directions)
            m0 = score_models(va_norm)["score_M0"].to_numpy()
            y = va[target_col].astype(float).to_numpy()
            rho0 = _rho(m0, y)
            inner_parts.append((val_year, va, va_norm, y, rho0))
        except ValueError as exc:
            LOGGER.debug("Skipping inner fold %s because of feature issues: %s", val_year, exc)
    if not inner_parts:
        return default, float("nan"), pd.DataFrame([{"note": "no valid nested folds"}])
    # Cache the daily interaction counts by inner fold and threshold pair. Counts do not depend
    # on lambda, so this avoids recalculating them for every lambda combination.
    count_cache = {}
    for val_year, va_raw, va_norm, y, rho0 in inner_parts:
        for tc, vl in itertools.product(grid.tcrit_values, vpd_candidates):
            count_cache[(val_year, float(tc), None if vl is None else float(vl))] = interaction_counts(
                va_raw, daily, tcrit_c=float(tc), vpd_low_kpa=None if vl is None else float(vl), mode=mode
            )
    scores = []
    for p in candidates:
        lifts, rhos0, rhos2 = [], [], []
        for val_year, va_raw, va_norm, y, rho0 in inner_parts:
            counts = count_cache[(val_year, float(p.tcrit_c), None if p.vpd_low_kpa is None else float(p.vpd_low_kpa))]
            scored = score_models(va_norm, counts, p)
            rho2 = _rho(scored["score_M2"], y)
            if np.isfinite(rho0) and np.isfinite(rho2):
                lifts.append(rho2 - rho0)
                rhos0.append(rho0)
                rhos2.append(rho2)
        scores.append({**asdict(p),
                       "inner_mean_rho_lift": float(np.mean(lifts)) if lifts else np.nan,
                       "inner_mean_rho_M0": float(np.mean(rhos0)) if rhos0 else np.nan,
                       "inner_mean_rho_M2": float(np.mean(rhos2)) if rhos2 else np.nan,
                       "inner_folds_used": len(lifts)})
    table = pd.DataFrame(scores)
    viable = table[np.isfinite(table["inner_mean_rho_lift"])].copy()
    if viable.empty:
        return default, float("nan"), table
    viable["complexity"] = viable["lambda_flow"].abs() + viable["lambda_fill"].abs()
    # The objective is incremental rank lift vs M0; tie-break toward lower penalties.
    sort_cols = ["inner_mean_rho_lift", "complexity", "tcrit_c"]
    ascending = [False, True, True]
    if mode == "vpd":
        sort_cols.append("vpd_low_kpa")
        ascending.append(True)
    best = viable.sort_values(sort_cols, ascending=ascending).iloc[0]
    chosen = InteractionParams(float(best["lambda_flow"]), float(best["lambda_fill"]), float(best["tcrit_c"]),
                               None if pd.isna(best["vpd_low_kpa"]) else float(best["vpd_low_kpa"]), mode)
    return chosen, float(best["inner_mean_rho_lift"]), table


def _training_loss_threshold(train: pd.DataFrame, target_col: str) -> float:
    # The held-out labels never set the tercile cutoff.
    return float(pd.to_numeric(train[target_col], errors="coerce").quantile(1 / 3))


def _evaluate_one_year(train_raw: pd.DataFrame, test_raw: pd.DataFrame, daily: pd.DataFrame,
                       target_col: str, grid: GridConfig, directions: dict[str, str], mode: str,
                       crop: str, year: int) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    params, inner_lift, search = _inner_cv_lift(train_raw, daily, target_col, grid, directions, mode)
    _, test_norm, scaler = _fit_transform_pair(train_raw, test_raw, grid, directions)
    threshold = _training_loss_threshold(train_raw, target_col)
    y = test_raw[target_col].astype(float).to_numpy()
    y_loss = (y <= threshold).astype(int)
    counts = interaction_counts(test_raw, daily, tcrit_c=params.tcrit_c, vpd_low_kpa=params.vpd_low_kpa, mode=mode)
    scored = score_models(test_norm, counts, params)
    rho0, rho1, rho2 = (_rho(scored[f"score_{m}"], y) for m in ("M0", "M1", "M2"))
    auc0, auc1, auc2 = (_auc(y_loss, -scored[f"score_{m}"]) for m in ("M0", "M1", "M2"))
    p = asdict(params)
    row = {
        "crop": crop, "heldout_type": "year", "heldout_group": int(year), "n_test": len(test_raw),
        "n_loss_events_test": int(y_loss.sum()), "loss_cutoff_from_train_only": threshold,
        "rho_M0": rho0, "rho_M1": rho1, "rho_M2": rho2,
        "lift_rho_M1_minus_M0": rho1-rho0 if np.isfinite(rho1) and np.isfinite(rho0) else np.nan,
        "lift_rho_M2_minus_M0": rho2-rho0 if np.isfinite(rho2) and np.isfinite(rho0) else np.nan,
        "lift_rho_M2_minus_M1": rho2-rho1 if np.isfinite(rho2) and np.isfinite(rho1) else np.nan,
        "auc_M0": auc0, "auc_M1": auc1, "auc_M2": auc2,
        "lift_auc_M2_minus_M0": auc2-auc0 if np.isfinite(auc2) and np.isfinite(auc0) else np.nan,
        "lift_auc_M1_minus_M0": auc1-auc0 if np.isfinite(auc1) and np.isfinite(auc0) else np.nan,
        "lift_auc_M2_minus_M1": auc2-auc1 if np.isfinite(auc2) and np.isfinite(auc1) else np.nan,
        "inner_cv_mean_lift_used_for_calibration": inner_lift,
        "calibration_status": "nested_inner_year_cv" if np.isfinite(inner_lift) else "fallback_no_tuning",
        **{f"param_{k}": v for k, v in p.items()},
    }
    pred_cols = ["season_id", "crop", "district", "year", target_col]
    pred = scored[pred_cols + ["score_M0", "score_M1", "score_M2"]].copy()
    pred["loss_event"] = y_loss
    pred["loss_cutoff_from_train_only"] = threshold
    # Sensitivity: perturb thresholds on held-out observations but never choose them from test labels.
    sens = []
    vpd_options: tuple[float | None, ...] = (None,) if mode == "soil_moisture" else tuple(grid.vpd_low_values)
    for tc, vl in itertools.product(grid.tcrit_values, vpd_options):
        sp = InteractionParams(params.lambda_flow, params.lambda_fill, float(tc), None if vl is None else float(vl), mode)
        scounts = interaction_counts(test_raw, daily, tcrit_c=sp.tcrit_c, vpd_low_kpa=sp.vpd_low_kpa, mode=mode)
        ss = score_models(test_norm, scounts, sp)
        r2 = _rho(ss["score_M2"], y)
        sens.append({"crop": crop, "heldout_year": int(year), "lambda_flow_fixed": params.lambda_flow,
                     "lambda_fill_fixed": params.lambda_fill, "tcrit_c": tc, "vpd_low_kpa": vl,
                     "rho_M0": rho0, "rho_M2": r2,
                     "lift_rho": r2-rho0 if np.isfinite(r2) and np.isfinite(rho0) else np.nan})
    lams = []
    for lam in grid.lambda_values:
        sp = InteractionParams(float(lam), float(lam), params.tcrit_c, params.vpd_low_kpa, mode)
        scounts = interaction_counts(test_raw, daily, tcrit_c=sp.tcrit_c, vpd_low_kpa=sp.vpd_low_kpa, mode=mode)
        ss = score_models(test_norm, scounts, sp)
        r2 = _rho(ss["score_M2"], y)
        lams.append({"crop": crop, "heldout_year": int(year), "lambda_common": lam,
                     "tcrit_c_fixed": params.tcrit_c, "vpd_low_kpa_fixed": params.vpd_low_kpa,
                     "rho_M0": rho0, "rho_M2": r2,
                     "lift_rho": r2-rho0 if np.isfinite(r2) and np.isfinite(rho0) else np.nan})
    search = search.copy()
    search["outer_crop"] = crop
    search["outer_heldout_year"] = int(year)
    return row, pd.DataFrame(sens), pd.DataFrame(lams), pred, search


def moving_block_ci(values: Iterable[float], reps: int, seed: int) -> tuple[float, float, float]:
    x = np.asarray(list(values), dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return np.nan, np.nan, np.nan
    mean = float(np.mean(x))
    if len(x) == 1:
        return mean, np.nan, np.nan
    rng = np.random.default_rng(seed)
    # Circular moving blocks retain some short-range temporal dependence. The block length is a
    # declared heuristic, not an estimate derived from this data.
    block_len = min(len(x), max(2, int(round(len(x) ** (1 / 3)))))
    boot = []
    for _ in range(reps):
        sample = []
        while len(sample) < len(x):
            start = int(rng.integers(0, len(x)))
            sample.extend(x[(start + j) % len(x)] for j in range(block_len))
        boot.append(float(np.mean(sample[:len(x)])))
    low, high = np.quantile(boot, [0.025, 0.975])
    return mean, float(low), float(high)


def summarize_year_results(results: pd.DataFrame, threshold_sensitivity: pd.DataFrame,
                           grid: GridConfig) -> pd.DataFrame:
    rows = []
    for crop, group in results.groupby("crop", sort=True):
        lifts = group["lift_rho_M2_minus_M0"]
        auc_lifts = group["lift_auc_M2_minus_M0"]
        mean_lift, ci_low, ci_high = moving_block_ci(lifts, grid.bootstrap_reps, grid.seed)
        mean_auc, auc_low, auc_high = moving_block_ci(auc_lifts, grid.bootstrap_reps, grid.seed + 1)
        frac_pos = float((lifts.dropna() > 0).mean()) if lifts.notna().any() else np.nan
        # Decomposition: M1-M0 = phase weighting; M2-M1 = interaction term. M2-M0 alone cannot separate them.
        m1_lift = group["lift_rho_M1_minus_M0"]
        m21_lift = group["lift_rho_M2_minus_M1"]
        m1_mean, m1_low, m1_high = moving_block_ci(m1_lift, grid.bootstrap_reps, grid.seed + 2)
        m21_mean, m21_low, m21_high = moving_block_ci(m21_lift, grid.bootstrap_reps, grid.seed + 3)
        neg_years = sorted(int(y) for y in group.loc[lifts < 0, "heldout_group"])
        sg = threshold_sensitivity[threshold_sensitivity["crop"] == crop] if not threshold_sensitivity.empty else pd.DataFrame()
        if not sg.empty:
            settings = sg.groupby(["tcrit_c", "vpd_low_kpa"], dropna=False)["lift_rho"].mean()
            sens_pos = float((settings.dropna() > 0).mean()) if settings.notna().any() else np.nan
        else:
            sens_pos = np.nan
        n_years = group["heldout_group"].nunique()
        enough = n_years >= grid.min_heldout_years and group["n_test"].min() >= grid.min_test_rows
        core = (np.isfinite(mean_lift) and mean_lift >= grid.min_mean_rho_lift and
                np.isfinite(frac_pos) and frac_pos >= grid.min_positive_year_fraction and
                np.isfinite(ci_low) and ci_low > grid.min_ci_lower)
        robust = np.isfinite(sens_pos) and sens_pos >= grid.min_sensitivity_fraction_positive
        if not enough:
            verdict = "NOT EVALUABLE: insufficient held-out years/rows"
        elif core and robust:
            verdict = "SUPPORTED against pre-registered ranked-skill criteria"
        elif np.isfinite(mean_lift) and mean_lift > 0 and (frac_pos >= 0.5 or robust):
            verdict = "PARTIALLY SUPPORTED: some lift, but at least one criterion is unmet"
        else:
            verdict = "NOT SUPPORTED against pre-registered ranked-skill criteria"
        rows.append({
            "crop": crop, "heldout_years_evaluated": n_years,
            "mean_spearman_lift_M2_minus_M0": mean_lift,
            "spearman_lift_block_bootstrap_95ci_low": ci_low,
            "spearman_lift_block_bootstrap_95ci_high": ci_high,
            "fraction_years_positive_lift": frac_pos,
            "mean_auc_lift_M2_minus_M0": mean_auc,
            "auc_lift_block_bootstrap_95ci_low": auc_low,
            "auc_lift_block_bootstrap_95ci_high": auc_high,
            "fraction_threshold_settings_with_positive_mean_lift": sens_pos,
            "strict_mean_lift_ge_0_05": bool(np.isfinite(mean_lift) and mean_lift >= 0.05),
            "mean_spearman_lift_M1_minus_M0_phase_weighting": m1_mean,
            "m1_minus_m0_block_bootstrap_95ci_low": m1_low,
            "m1_minus_m0_block_bootstrap_95ci_high": m1_high,
            "mean_spearman_lift_M2_minus_M1_interaction": m21_mean,
            "m2_minus_m1_block_bootstrap_95ci_low": m21_low,
            "m2_minus_m1_block_bootstrap_95ci_high": m21_high,
            "negative_lift_years_M2_minus_M0": ";".join(map(str, neg_years)),
            "verdict": verdict,
        })
    return pd.DataFrame(rows)


def build_report(results: pd.DataFrame, summary: pd.DataFrame, panel: pd.DataFrame,
                 target_col: str, mode: str, grid: GridConfig) -> str:
    display = ["crop", "heldout_years_evaluated", "mean_spearman_lift_M2_minus_M0",
               "spearman_lift_block_bootstrap_95ci_low", "spearman_lift_block_bootstrap_95ci_high",
               "fraction_years_positive_lift", "mean_auc_lift_M2_minus_M0",
               "mean_spearman_lift_M1_minus_M0_phase_weighting", "mean_spearman_lift_M2_minus_M1_interaction",
               "m2_minus_m1_block_bootstrap_95ci_low", "m2_minus_m1_block_bootstrap_95ci_high",
               "negative_lift_years_M2_minus_M0", "verdict"]
    annual = ["crop", "heldout_group", "n_test", "rho_M0", "rho_M1", "rho_M2",
              "lift_rho_M2_minus_M0", "lift_rho_M2_minus_M1", "auc_M0", "auc_M1", "auc_M2",
              "param_lambda_flow", "param_lambda_fill", "param_tcrit_c", "param_vpd_low_kpa", "calibration_status"]
    text = [
        f"# HelioS / SEL-CCI Back-test Report — {VERSION}", "",
        "> This report reflects only the supplied data. Do not interpret it as evidence until the source manifest, data joins, yield units, and phase definitions have been checked.", "",
        "## Protocol summary", "",
        f"- Target column: `{target_col}`; higher score means more favorable conditions.",
        f"- Interaction mode: `{mode}`.",
        "- Primary validation: leave one calendar year out within each crop.",
        "- Hyperparameters are selected through inner leave-one-training-year-out validation, optimizing Spearman lift over M0. Outer test-year labels are not used to tune parameters.",
        "- Binary loss-event cutoff is the bottom-third quantile of training-fold outcomes only; held-out outcomes do not set the cutoff.",
        "- Confidence intervals use a circular moving-block bootstrap over yearly lifts. The block-length rule is a heuristic and should be sensitivity-checked when many years are available.",
        "- RMSE/MAE are omitted: the score is a 0–100 index, not a yield-unit forecast.", "",
        "## Summary", "",
        _md_table(summary[display]) if not summary.empty else "No summary rows.", "",
        "## Held-out year results", "",
        _md_table(results[annual]) if not results.empty else "No annual rows.", "",
        "## Decision rule", "",
        f"Full support requires mean held-out Spearman lift >= {grid.min_mean_rho_lift:.2f}, positive lift in >= {grid.min_positive_year_fraction:.0%} of held-out years, lower block-bootstrap 95% interval > {grid.min_ci_lower:.2f}, and positive mean lift for >= {grid.min_sensitivity_fraction_positive:.0%} of threshold settings.",
        "The stricter mean lift of 0.05 is separately flagged. A favourable result does not establish causality or actuarial calibration.",
        f"The sample-count rule (>= {grid.min_heldout_years} held-out years, >= {grid.min_test_rows} rows per year) and all pass/fail cut-offs above are pre-registered DESIGN CHOICES, not established scientific thresholds.",
        "M1-M0 measures the phase-weighting contribution; M2-M1 measures the heat/low-VPD interaction contribution. M2-M0 alone cannot separate the two. The low-VPD direction is a candidate hypothesis and is not confirmed by this report.", "",
        "## Audit cautions", "",
        f"- Panel size: {len(panel)} crop-season rows; {panel['district'].nunique()} districts; {panel['year'].nunique()} unique years.",
        "- `stress_exposure` is not generally a standalone phenological stage. Six windows must be explicitly defined. By default the builder rejects overlap; if an overlay is intentional, record that rationale and opt in explicitly.",
        "- All four normalized inputs must share a favorable direction: higher means better. The default climate orientation assumes the raw climate field is Tmax (lower is better).",
        "- Raw scores are scaled using the training fold's 5th/95th percentiles, pooled across phases; held-out rows are clipped to those limits. This is an index scale, not a physiological calibration.",
        "- M0 uses flat weights applied to seasonal means of the same phase-specific inputs. An additional benchmark using independently sourced seasonal aggregates would be a useful sensitivity check.",
        "- If raw yield is used instead of a source-documented yield anomaly, persistent district productivity may confound rank skill. Leave-one-year-out holds year constant within each evaluation year but does not by itself remove district effects.",
        "- The default lambda, temperature and VPD grids are exploratory search ranges. They are not literature-derived crop thresholds and must be declared before interpreting results.",
    ]
    return "\n".join(text) + "\n"


def run_year_backtest(panel: pd.DataFrame, daily: pd.DataFrame, *, target_col: str | None = None,
                      grid: GridConfig | None = None, directions: dict[str, str] | None = None,
                      interaction_mode: str = "auto", allow_overlapping_phases: bool = False,
                      output_dir: str | Path = "outputs") -> dict[str, Any]:
    grid = grid or GridConfig()
    directions = directions or grid.directions.copy()
    panel = validate_seasons(panel, allow_overlapping_phases=allow_overlapping_phases)
    panel, target_col = validate_phase_panel(panel, target_col)
    mode = detect_interaction_mode(daily, interaction_mode)
    daily = daily.copy().reset_index(drop=True)
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    if target_col == "yield":
        LOGGER.warning("Using raw yield. District productivity differences can confound this crop-risk test; prefer a historical yield anomaly without future-data leakage.")
    if not all(f"{v}_raw__{p}" in panel for p in PHASES for v in VARIABLES):
        LOGGER.warning("Panel contains pre-normalized phase inputs. Fold-safe scaling from raw values cannot be independently checked.")
    outdir = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    results, sensitivities, lambda_sens, predictions, searches = [], [], [], [], []
    for crop, crop_panel in panel.groupby("crop", sort=True):
        years = sorted(crop_panel["year"].astype(int).unique())
        for year in years:
            train = crop_panel[crop_panel["year"].astype(int) != year].copy()
            test = crop_panel[crop_panel["year"].astype(int) == year].copy()
            if train.empty or test.empty:
                continue
            row, sens, lams, pred, search = _evaluate_one_year(
                train, test, daily, target_col, grid, directions, mode, str(crop), int(year)
            )
            results.append(row)
            sensitivities.append(sens)
            lambda_sens.append(lams)
            predictions.append(pred)
            searches.append(search)
            LOGGER.info("crop=%s year=%s Spearman lift=%s (%s)", crop, year,
                        row["lift_rho_M2_minus_M0"], row["calibration_status"])
    if not results:
        raise ValueError("No valid leave-one-year-out folds were produced")
    res = pd.DataFrame(results).sort_values(["crop", "heldout_group"])
    sens = pd.concat(sensitivities, ignore_index=True)
    lam_sens = pd.concat(lambda_sens, ignore_index=True)
    pred = pd.concat(predictions, ignore_index=True)
    search_df = pd.concat(searches, ignore_index=True)
    summary = summarize_year_results(res, sens, grid)
    model_spec = {
        "version": VERSION, "phase_order": list(PHASES), "phase_weights": WEIGHTS,
        "flat_weights": FLAT_WEIGHTS, "grid": asdict(grid), "directions": directions,
        "interaction_mode": mode, "target_column": target_col,
        "outer_fold_parameters": res[["crop", "heldout_group", "param_lambda_flow", "param_lambda_fill",
                                        "param_tcrit_c", "param_vpd_low_kpa", "calibration_status"]].to_dict("records"),
    }
    manifest = {
        "model_version": VERSION,
        "weight_table_sha256": canonical_json_sha256({"phase_order": list(PHASES), "phase_weights": WEIGHTS, "flat_weights": FLAT_WEIGHTS}),
        "run_spec_sha256": canonical_json_sha256(model_spec),
        "model_spec": model_spec,
        "runtime": {"python": platform.python_version(), "pandas": pd.__version__, "numpy": np.__version__,
                    "scipy": scipy.__version__, "scikit_learn": sklearn.__version__},
        "data": {"rows": int(len(panel)), "crops": sorted(panel["crop"].astype(str).unique()),
                 "districts": int(panel["district"].nunique()), "years": sorted(panel["year"].unique()),
                 "target_column": target_col, "input_panel_sha256": None, "daily_data_sha256": None},
        "interpretation": {"score_direction": "higher=favorable conditions; lower score is treated as higher loss risk",
                           "auc_cutoff": "bottom-third quantile of outer training-fold target only",
                           "rmse_mae": "not computed; risk score is not a yield-unit prediction",
                           "confidence_intervals": "circular moving-block bootstrap on held-out-year metric series"},
    }
    for name, frame in (("yearly_results", res), ("summary", summary), ("threshold_sensitivity", sens),
                        ("lambda_sensitivity", lam_sens), ("heldout_predictions", pred),
                        ("nested_parameter_search", search_df)):
        save_table(frame, outdir / name)
    (outdir / "model_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    report = build_report(res, summary, panel, target_col, mode, grid)
    (outdir / "report.md").write_text(report, encoding="utf-8")
    return {"results": res, "summary": summary, "threshold_sensitivity": sens,
            "lambda_sensitivity": lam_sens, "predictions": pred, "parameter_search": search_df,
            "manifest": manifest, "report": report}


def run_district_holdout(panel: pd.DataFrame, daily: pd.DataFrame, *, target_col: str | None = None,
                         grid: GridConfig | None = None, directions: dict[str, str] | None = None,
                         interaction_mode: str = "auto", allow_overlapping_phases: bool = False,
                         output_dir: str | Path = "outputs/district_holdout") -> pd.DataFrame:
    """Secondary leave-one-district-out check; interpret separately from temporal holdout."""
    grid = grid or GridConfig()
    directions = directions or grid.directions.copy()
    panel = validate_seasons(panel, allow_overlapping_phases=allow_overlapping_phases)
    panel, target_col = validate_phase_panel(panel, target_col)
    mode = detect_interaction_mode(daily, interaction_mode)
    daily = daily.copy().reset_index(drop=True)
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    rows, preds = [], []
    outdir = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    for crop, cp in panel.groupby("crop", sort=True):
        for district in sorted(cp["district"].astype(str).unique()):
            train = cp[cp["district"].astype(str) != district].copy()
            test = cp[cp["district"].astype(str) == district].copy()
            if len(test) < 3 or train["year"].nunique() < grid.min_inner_years:
                continue
            params, inner_lift, _ = _inner_cv_lift(train, daily, target_col, grid, directions, mode)
            _, test_norm, _ = _fit_transform_pair(train, test, grid, directions)
            counts = interaction_counts(test, daily, tcrit_c=params.tcrit_c,
                                        vpd_low_kpa=params.vpd_low_kpa, mode=mode)
            scored = score_models(test_norm, counts, params)
            y = test[target_col].astype(float).to_numpy()
            rho0, rho1, rho2 = (_rho(scored[f"score_{m}"], y) for m in ("M0", "M1", "M2"))
            cutoff = _training_loss_threshold(train, target_col)
            y_loss = (y <= cutoff).astype(int)
            auc0, auc1, auc2 = (_auc(y_loss, -scored[f"score_{m}"]) for m in ("M0", "M1", "M2"))
            rows.append({"crop": crop, "heldout_type": "district", "heldout_group": district,
                         "n_test": len(test), "rho_M0": rho0, "rho_M1": rho1, "rho_M2": rho2,
                         "lift_rho_M2_minus_M0": rho2-rho0 if np.isfinite(rho0) and np.isfinite(rho2) else np.nan,
                         "auc_M0": auc0, "auc_M1": auc1, "auc_M2": auc2,
                         "lift_auc_M2_minus_M0": auc2-auc0 if np.isfinite(auc0) and np.isfinite(auc2) else np.nan,
                         "inner_cv_mean_lift_used_for_calibration": inner_lift,
                         "param_lambda_flow": params.lambda_flow, "param_lambda_fill": params.lambda_fill,
                         "param_tcrit_c": params.tcrit_c, "param_vpd_low_kpa": params.vpd_low_kpa,
                         "calibration_status": "nested_inner_year_cv" if np.isfinite(inner_lift) else "fallback_no_tuning"})
            pr = scored[["season_id", "crop", "district", "year", target_col, "score_M0", "score_M1", "score_M2"]].copy()
            pr["loss_event"] = y_loss
            preds.append(pr)
    result = pd.DataFrame(rows)
    save_table(result, outdir / "district_holdout_results")
    if preds:
        save_table(pd.concat(preds, ignore_index=True), outdir / "district_holdout_predictions")
    return result


def load_grid(path: str | Path | None) -> GridConfig:
    if path is None:
        return GridConfig()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("lambda_values", "tcrit_values", "vpd_low_values"):
        if key in payload:
            payload[key] = tuple(payload[key])
    allowed = set(GridConfig.__dataclass_fields__)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"Unknown grid/config keys: {unknown}. Allowed: {sorted(allowed)}")
    return GridConfig(**payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nested and auditable HelioS/SEL-CCI crop-risk back-test")
    subs = parser.add_subparsers(dest="command", required=True)
    b = subs.add_parser("build-panel", help="aggregate daily source inputs to phase-level raw features")
    b.add_argument("--seasons", required=True, help="CSV/Parquet yield and six phase windows")
    b.add_argument("--daily", required=True, help="CSV/Parquet, one district/crop/date row with the four *_raw inputs")
    b.add_argument("--out", required=True, help="Output path stem (CSV plus optional Parquet)")
    b.add_argument("--allow-overlapping-phases", action="store_true")
    r = subs.add_parser("run", help="nested leave-one-year-out primary back-test")
    r.add_argument("--panel", required=True)
    r.add_argument("--daily", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--target", default=None, help="default: yield_anomaly if present, otherwise yield")
    r.add_argument("--grid", default=None, help="optional JSON grid override")
    r.add_argument("--interaction-mode", choices=("auto", "vpd", "soil_moisture"), default="auto")
    r.add_argument("--allow-overlapping-phases", action="store_true")
    r.add_argument("--source-manifest", default=None, help="JSON provenance manifest for both data inputs")
    r.add_argument("--run-district-holdout", action="store_true", help="also run the secondary spatial holdout")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "build-panel":
        season_df, daily_df = read_table(args.seasons), read_table(args.daily)
        phase_panel = aggregate_daily_to_phase_features(season_df, daily_df,
                                                        allow_overlapping_phases=args.allow_overlapping_phases)
        stem = Path(args.out)
        if stem.suffix.lower() in {".csv", ".parquet", ".pq"}:
            stem = stem.with_suffix("")
        print("Wrote:", ", ".join(save_table(phase_panel, stem)))
        return 0

    panel_path, daily_path = Path(args.panel), Path(args.daily)
    panel_df, daily_df = read_table(panel_path), read_table(daily_path)
    grid = load_grid(args.grid)
    outdir = Path(args.out)
    run = run_year_backtest(panel_df, daily_df, target_col=args.target, grid=grid,
                            interaction_mode=args.interaction_mode,
                            allow_overlapping_phases=args.allow_overlapping_phases, output_dir=outdir)
    manifest_path = outdir / "model_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["data"].update({"input_panel_sha256": file_sha256(panel_path),
                             "daily_data_sha256": file_sha256(daily_path),
                             "panel_path": str(panel_path), "daily_path": str(daily_path),
                             "source_manifest_path": args.source_manifest})
    if args.source_manifest:
        source_manifest = json.loads(Path(args.source_manifest).read_text(encoding="utf-8"))
        manifest["data"]["source_manifest"] = source_manifest
    manifest["run_spec_sha256"] = canonical_json_sha256({
        "model_spec": manifest["model_spec"],
        "input_panel_sha256": manifest["data"]["input_panel_sha256"],
        "daily_data_sha256": manifest["data"]["daily_data_sha256"],
        "source_manifest": manifest["data"].get("source_manifest", {}),
    })
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    if args.run_district_holdout:
        run_district_holdout(panel_df, daily_df, target_col=args.target, grid=grid,
                             interaction_mode=args.interaction_mode,
                             allow_overlapping_phases=args.allow_overlapping_phases,
                             output_dir=outdir / "district_holdout")
    print(f"Completed: {outdir}")
    print(f"Model version: {manifest['model_version']}")
    print(f"Weight-table SHA-256: {manifest['weight_table_sha256']}")
    print(f"Run/data SHA-256: {manifest['run_spec_sha256']}")
    print("Read report.md. No validity claim is warranted without checking the supplied data and source manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
