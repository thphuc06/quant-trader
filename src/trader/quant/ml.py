"""Baseline ML pipeline: triple-barrier labels, purged walk-forward, calibration, honest reporting.

The model is NOT used for decisions unless it passes the gates in configs/app.yaml and an operator flips
``ml.use_for_decisions``. Predictions always carry their validation status.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from trader.domain import PredictionOutput
from trader.quant.features import compute_features
from trader.timeutil import from_ms, interval_ms, iso, utc_now

# 'atr' and 'close' are in price units (non-stationary) and are excluded from model inputs.
MODEL_FEATURES = [
    "ema_spread", "tsmom", "adx", "rsi", "atr_pct", "rv", "rv_ratio", "rv_pct",
    "donchian_pos", "rel_volume", "taker_buy_ratio", "vwap_dev",
]  # fmt: skip
MODELS = {
    "logreg_v1": "logistic regression on standardized features",
    "hgb_v1": "histogram gradient boosting",
}


def triple_barrier_labels(
    candles: pd.DataFrame, atr: pd.Series, k_up: float, k_dn: float, horizon: int
) -> pd.DataFrame:
    """Label each bar: 1 if close + k_up*ATR is touched before close - k_dn*ATR within ``horizon`` bars.

    Both touched in the same bar -> lower first (pessimistic). Timeout -> 0. Rows without ``horizon`` future
    bars or without ATR get NaN. ``gross_return`` is the return to the exit that the label implies.
    """
    close = candles["close"].to_numpy(float)
    high = candles["high"].to_numpy(float)
    low = candles["low"].to_numpy(float)
    a = atr.to_numpy(float)
    n = len(candles)
    label = np.full(n, np.nan)
    gross = np.full(n, np.nan)
    for i in range(n - horizon):
        if not np.isfinite(a[i]) or a[i] <= 0:
            continue
        up, dn = close[i] + k_up * a[i], close[i] - k_dn * a[i]
        outcome, exit_price = 0.0, close[i + horizon]
        for j in range(i + 1, i + horizon + 1):
            if low[j] <= dn:
                outcome, exit_price = 0.0, dn
                break
            if high[j] >= up:
                outcome, exit_price = 1.0, up
                break
        label[i] = outcome
        gross[i] = exit_price / close[i] - 1.0
    return pd.DataFrame({"label": label, "gross_return": gross}, index=candles.index)


def walk_forward_folds(n: int, train: int, test: int, purge: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Rolling-origin folds. The last ``purge`` rows of each training window are dropped because their
    label windows overlap the test block."""
    folds = []
    start = 0
    while start + train + test <= n:
        train_idx = np.arange(start, start + train - purge)
        test_idx = np.arange(start + train, start + train + test)
        folds.append((train_idx, test_idx))
        start += test
    return folds


def _make_model(name: str) -> Any:
    if name == "logreg_v1":
        return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500))
    if name == "hgb_v1":
        return HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=150, l2_regularization=1.0, random_state=7
        )
    raise ValueError(f"unknown model {name!r}; available: {sorted(MODELS)}")


def _fit_calibrated(name: str, x: np.ndarray, y: np.ndarray, purge: int) -> Any:
    """Fit on the first 80% of the window, Platt-calibrate on the last 20% (with a purge gap between)."""
    cut = int(len(x) * 0.8)
    model = _make_model(name).fit(x[: cut - purge], y[: cut - purge])
    calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
    return calibrated.fit(x[cut:], y[cut:])


def expected_calibration_error(
    y: np.ndarray, p: np.ndarray, bins: int = 10
) -> tuple[float, list[dict[str, float]]]:
    edges = np.linspace(0, 1, bins + 1)
    table, ece = [], 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (p >= lo) & (p < hi) if hi < 1 else (p >= lo) & (p <= hi)
        if mask.sum() == 0:
            continue
        mean_p, freq = float(p[mask].mean()), float(y[mask].mean())
        ece += mask.mean() * abs(mean_p - freq)
        table.append(
            {
                "bin_low": float(lo),
                "bin_high": float(hi),
                "count": int(mask.sum()),
                "mean_predicted": mean_p,
                "observed_frequency": freq,
            }
        )
    return float(ece), table


def build_dataset(
    candles: pd.DataFrame, interval: str, feature_params: dict[str, int], ml_cfg: dict[str, Any]
) -> pd.DataFrame:
    features = compute_features(candles, interval, feature_params)
    labels = triple_barrier_labels(
        candles,
        features["atr"],
        float(ml_cfg["barrier_up_atr"]),
        float(ml_cfg["barrier_down_atr"]),
        int(ml_cfg["horizon_bars"]),
    )
    return pd.concat([features, labels], axis=1)


def evaluate_model(
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    model_name: str,
    feature_params: dict[str, int],
    ml_cfg: dict[str, Any],
    cost_per_side: float,
    holdout_cutoff_ms: int,
    max_ece: float,
) -> dict[str, Any]:
    """Purged walk-forward evaluation on the development period only."""
    horizon = int(ml_cfg["horizon_bars"])
    data = build_dataset(
        candles[candles["open_time"] < holdout_cutoff_ms].reset_index(drop=True),
        interval,
        feature_params,
        ml_cfg,
    )
    data = data.dropna(subset=[*MODEL_FEATURES, "label"]).reset_index(drop=True)
    bars_per_day = 86_400_000 // interval_ms(interval)
    train, test = int(ml_cfg["train_days"]) * bars_per_day, int(ml_cfg["test_days"]) * bars_per_day
    folds = walk_forward_folds(len(data), train, test, purge=horizon)
    if not folds:
        return {"status": "insufficient_data", "rows": len(data), "needed": train + test}

    x_all, y_all = data[MODEL_FEATURES].to_numpy(float), data["label"].to_numpy(float)
    gross_all = data["gross_return"].to_numpy(float)
    preds, truth, base, gross = [], [], [], []
    fold_rows: list[dict[str, Any]] = []
    for k, (tr, te) in enumerate(folds):
        if len(np.unique(y_all[tr])) < 2:
            continue
        model = _fit_calibrated(model_name, x_all[tr], y_all[tr], horizon)
        p = model.predict_proba(x_all[te])[:, 1]
        prevalence = float(y_all[tr].mean())
        b = np.full(len(te), prevalence)
        preds.append(p)
        truth.append(y_all[te])
        base.append(b)
        gross.append(gross_all[te])
        fold_rows.append(
            {
                "fold": k,
                "test_start": iso(from_ms(int(data["open_time"].iloc[te[0]]))),
                "log_loss": float(log_loss(y_all[te], p, labels=[0, 1])),
                "log_loss_base_rate": float(log_loss(y_all[te], b, labels=[0, 1])),
                "brier": float(brier_score_loss(y_all[te], p)),
                "brier_base_rate": float(brier_score_loss(y_all[te], b)),
            }
        )
    if not preds:
        return {"status": "insufficient_data", "rows": len(data), "reason": "no fold had both classes"}
    p, y, b, g = map(np.concatenate, (preds, truth, base, gross))
    ece, reliability = expected_calibration_error(y, p)
    net = g - 2 * cost_per_side
    buckets = []
    for lo, hi in ((0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01)):
        mask = (p >= lo) & (p < hi)
        buckets.append(
            {
                "p_low": lo,
                "p_high": min(hi, 1.0),
                "count": int(mask.sum()),
                "mean_net_return": float(net[mask].mean()) if mask.any() else None,
            }
        )
    beats_every_fold = all(
        f["log_loss"] < f["log_loss_base_rate"] and f["brier"] < f["brier_base_rate"] for f in fold_rows
    )
    calibration_ok = ece <= max_ece
    return {
        "status": "ok",
        "model": model_name,
        "symbol": symbol,
        "interval": interval,
        "label": {
            "type": "triple_barrier",
            "horizon_bars": horizon,
            "k_up_atr": ml_cfg["barrier_up_atr"],
            "k_down_atr": ml_cfg["barrier_down_atr"],
        },
        "rows": len(data),
        "folds": len(fold_rows),
        "oos_samples": len(y),
        "note_overlap": "Labels overlap by up to horizon bars, so the effective sample size is far below oos_samples.",
        "positive_rate_oos": float(y.mean()),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "log_loss_base_rate": float(log_loss(y, b, labels=[0, 1])),
        "brier": float(brier_score_loss(y, p)),
        "brier_base_rate": float(brier_score_loss(y, b)),
        "roc_auc": float(roc_auc_score(y, p)) if len(np.unique(y)) > 1 else None,
        "pr_auc": float(average_precision_score(y, p)) if len(np.unique(y)) > 1 else None,
        "ece": ece,
        "reliability_table": reliability,
        "net_return_by_probability_bucket": buckets,
        "per_fold": fold_rows,
        "gates_provisional": {
            "beats_base_rate_every_fold": beats_every_fold,
            "ece_within_limit": calibration_ok,
        },
        "calibration_status": "evaluated_ok" if calibration_ok else "evaluated_poor",
        "out_of_sample_status": "evaluated_passed_gates"
        if beats_every_fold and calibration_ok
        else "evaluated_no_edge",
    }


def train_final_model(
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    model_name: str,
    feature_params: dict[str, int],
    ml_cfg: dict[str, Any],
    holdout_cutoff_ms: int,
    evaluation: dict[str, Any],
    models_dir: Path,
) -> dict[str, Any]:
    """Fit on the most recent training window of the development period and store artefact + metadata."""
    horizon = int(ml_cfg["horizon_bars"])
    data = build_dataset(
        candles[candles["open_time"] < holdout_cutoff_ms].reset_index(drop=True),
        interval,
        feature_params,
        ml_cfg,
    )
    data = data.dropna(subset=[*MODEL_FEATURES, "label"]).reset_index(drop=True)
    window = int(ml_cfg["train_days"]) * (86_400_000 // interval_ms(interval))
    data = data.tail(window)
    model = _fit_calibrated(
        model_name, data[MODEL_FEATURES].to_numpy(float), data["label"].to_numpy(float), horizon
    )
    version = utc_now().strftime("%Y%m%d%H%M")
    meta = {
        "model_name": model_name,
        "model_version": version,
        "symbol": symbol,
        "interval": interval,
        "features": MODEL_FEATURES,
        "feature_params": feature_params,
        "label": evaluation.get("label"),
        "trained_through": iso(from_ms(int(data["open_time"].iloc[-1]))),
        "train_rows": len(data),
        "calibration_status": evaluation.get("calibration_status", "not_evaluated"),
        "out_of_sample_status": evaluation.get("out_of_sample_status", "not_evaluated"),
        "created_at": iso(utc_now()),
    }
    models_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{model_name}_{symbol}_{interval}"
    joblib.dump(model, models_dir / f"{stem}.joblib")
    (models_dir / f"{stem}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def predict_latest(
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    model_name: str,
    models_dir: Path,
    use_for_decisions: bool,
    data_ok: bool,
) -> PredictionOutput | None:
    """Predict for the latest closed bar. Returns None when no trained model exists."""
    stem = f"{model_name}_{symbol}_{interval}"
    artefact, meta_path = models_dir / f"{stem}.joblib", models_dir / f"{stem}.json"
    if not artefact.exists() or not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    features = compute_features(candles, interval, meta["feature_params"])
    row = features.iloc[[-1]]
    cutoff = iso(from_ms(int(row["close_time"].iloc[0])))
    label = meta.get("label") or {}
    common: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": interval,
        "horizon": f"next_{label.get('horizon_bars', '?')}_bars_triple_barrier",
        "prediction_timestamp": iso(utc_now()),
        "feature_cutoff_timestamp": cutoff,
        "model_name": meta["model_name"],
        "model_version": meta["model_version"],
        "calibration_status": meta["calibration_status"],
        "out_of_sample_status": meta["out_of_sample_status"],
        "data_quality": "ok" if data_ok else "degraded",
    }
    if row[MODEL_FEATURES].isna().any(axis=1).iloc[0]:
        return PredictionOutput(**common, notes=["features incomplete for the latest bar; no prediction"])
    model = joblib.load(artefact)  # artefact is produced locally by train_final_model
    p_up = float(model.predict_proba(row[MODEL_FEATURES].to_numpy(float))[:, 1][0])
    passed = (
        meta["out_of_sample_status"] == "evaluated_passed_gates"
        and meta["calibration_status"] == "evaluated_ok"
    )
    notes = [
        "p_up = probability the upper ATR barrier is hit before the lower one within the horizon; timeouts count as not-up.",
        "expected_net_return is null: no validated return model exists.",
    ]
    if not passed:
        notes.append("Model did NOT pass validation gates: treat p_up as uninformative.")
    return PredictionOutput(
        **common,
        p_up=p_up,
        p_down=None,
        p_neutral=None,
        usable_for_decisions=bool(passed and use_for_decisions and data_ok),
        notes=notes,
    )
