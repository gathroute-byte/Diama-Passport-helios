"""SOFTWARE tests only. All data here are random synthetic numbers generated in-test.
They verify that code paths run and that leakage guards hold. They are NOT crop-performance
evidence and must never be reported as validation of the HelioS hypothesis."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import helios_backtest as hb

SMALL = hb.GridConfig(lambda_values=(0.0, 0.4, 0.8), tcrit_values=(31.0, 33.0),
                      vpd_low_values=(0.8, 1.0), bootstrap_reps=50)  # test-speed grid only
WINDOWS = [("establishment", "03-01", "03-20"), ("vegetative_growth", "03-20", "04-20"),
           ("flowering", "04-20", "05-10"), ("stress_exposure", "05-10", "05-25"),
           ("fill", "05-25", "06-30"), ("harvest", "06-30", "07-20")]


def synthetic(n_dist=8, years=range(2015, 2022), seed=0):
    rng = np.random.default_rng(seed)
    seas, daily = [], []
    for d in range(n_dist):
        land = rng.uniform(40, 80)
        for y in years:
            dates = pd.date_range(f"{y}-02-20", f"{y}-07-31", freq="D")
            n = len(dates)
            daily.append(pd.DataFrame({
                "crop": "maize", "district": f"D{d}", "date": dates, "land_raw": land,
                "crop_raw": rng.uniform(0.3, 0.8, n), "climate_raw": rng.normal(30, 3, n),
                "moisture_raw": rng.uniform(0.1, 0.4, n), "tmax_c": rng.normal(30, 3, n),
                "vpd_kpa": rng.uniform(0.4, 2, n)}))
            row = {"season_id": f"maize|D{d}|{y}|A", "crop": "maize", "district": f"D{d}", "year": y,
                   "season": "A", "yield": rng.normal(2, .5), "yield_anomaly": rng.normal(0, .3)}
            for p, s, e in WINDOWS:
                row[f"{p}_start"], row[f"{p}_end"] = f"{y}-{s}", f"{y}-{e}"
            seas.append(row)
    return pd.DataFrame(seas), pd.concat(daily, ignore_index=True)


def test_phase_features_are_phase_specific_and_have_coverage():
    s, d = synthetic()
    panel = hb.aggregate_daily_to_phase_features(s, d)
    cols = [f"{v}_raw__{p}" for v in hb.VARIABLES for p in hb.PHASES]
    assert len(cols) == 24 and not panel[cols].isna().any().any()
    # climate differs between phases for a season (not one annual value copied six times)
    assert panel[[f"climate_raw__{p}" for p in hb.PHASES]].nunique(axis=1).min() > 1
    assert (panel[[c.replace("_raw__", "_cov__") for c in cols]] > 0.99).all().all()


def test_overlapping_phases_rejected_by_default():
    s, d = synthetic(n_dist=1, years=[2015])
    s.loc[0, "flowering_end"] = "2015-05-20"  # runs into stress_exposure
    try:
        hb.validate_seasons(s)
    except ValueError as exc:
        assert "Overlapping" in str(exc)
    else:
        raise AssertionError("overlap should be rejected")
    hb.validate_seasons(s, allow_overlapping_phases=True)


def test_duplicate_daily_rows_rejected():
    s, d = synthetic(n_dist=1, years=[2015])
    try:
        hb.aggregate_daily_to_phase_features(s, pd.concat([d, d.iloc[:1]]))
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicates should be rejected")


def test_scaler_uses_training_rows_only():
    s, d = synthetic()
    panel = hb.aggregate_daily_to_phase_features(s, d)
    train, test = panel[panel.year != 2021], panel[panel.year == 2021].copy()
    scaler = hb.fit_feature_scaler(train)
    test.loc[:, "climate_raw__flowering"] = 1e6  # extreme held-out value must not move the scaler
    assert hb.fit_feature_scaler(train) == scaler
    out = hb.transform_phase_features(test, scaler)
    assert out["climate__flowering"].between(0, 100).all()


def test_full_run_outputs_decomposition_and_no_tabulate_needed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "tabulate", None)  # clean-image condition
    s, d = synthetic()
    panel = hb.aggregate_daily_to_phase_features(s, d)
    run = hb.run_year_backtest(panel, d, grid=SMALL, output_dir=tmp_path / "o")
    res = run["results"]
    assert {"lift_rho_M2_minus_M1", "lift_auc_M2_minus_M1"} <= set(res.columns)
    # M2-M0 == (M1-M0) + (M2-M1) per held-out year
    chk = res["lift_rho_M1_minus_M0"] + res["lift_rho_M2_minus_M1"] - res["lift_rho_M2_minus_M0"]
    assert np.allclose(chk.dropna(), 0, atol=1e-9)
    # loss cutoff comes from training years only
    for _, r in res.iterrows():
        tr = panel[panel.year != r["heldout_group"]]["yield_anomaly"]
        assert abs(r["loss_cutoff_from_train_only"] - tr.quantile(1 / 3)) < 1e-12
    assert (tmp_path / "o" / "report.md").read_text().count("DESIGN CHOICES") >= 1


def test_heldout_year_outcomes_do_not_change_its_own_prediction(tmp_path):
    """Changing only the held-out year's outcomes must not change that year's scores/parameters."""
    s, d = synthetic(seed=3)
    panel = hb.aggregate_daily_to_phase_features(s, d)
    a = hb.run_year_backtest(panel, d, grid=SMALL, output_dir=tmp_path / "a")["predictions"]
    p2 = panel.copy()
    p2.loc[p2.year == 2018, "yield_anomaly"] = np.random.default_rng(9).normal(0, 5, (p2.year == 2018).sum())
    b = hb.run_year_backtest(p2, d, grid=SMALL, output_dir=tmp_path / "b")["predictions"]
    ka = a[a.year == 2018].sort_values("season_id")[["score_M0", "score_M1", "score_M2"]].to_numpy()
    kb = b[b.year == 2018].sort_values("season_id")[["score_M0", "score_M1", "score_M2"]].to_numpy()
    assert (ka == kb).all()
