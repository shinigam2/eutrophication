import json
from datetime import date as Date, datetime, timezone
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from scipy.stats import norm

try:
    from src.models.features import build_optical_features
except ImportError:
    from features import build_optical_features

ROOT_DIR = Path(__file__).resolve().parents[2]
SAVED_MODELS_DIR = Path(__file__).resolve().parent / "saved_models"
DATASET_PATH = ROOT_DIR / "data/processed/features_forecast_10d.parquet"
SATELLITE_PATH = ROOT_DIR / "data/processed/features_satellite.parquet"
RESULT_PATH = ROOT_DIR / "data/processed/result.json"

ATTENTION_THRESHOLD_UG_L = 15.0
BLOOM_THRESHOLD_UG_L = 25.0


def _clean_float(val, digits: int = 2) -> float | None:
    """Безопасно округляет число для JSON (заменяет NaN на None)."""
    if val is None or pd.isna(val):
        return None
    return round(float(val), digits)


def _next_decade_date(base_date_str: str) -> str:
    """Возвращает дату следующей календарной декады (1, 11 или 21 число)."""
    dt = datetime.strptime(base_date_str, "%Y-%m-%d")
    if dt.day < 11:
        next_dt = dt.replace(day=11)
    elif dt.day < 21:
        next_dt = dt.replace(day=21)
    else:
        if dt.month == 12:
            next_dt = dt.replace(year=dt.year + 1, month=1, day=1)
        else:
            next_dt = dt.replace(month=dt.month + 1, day=1)
    return next_dt.strftime("%Y-%m-%d")


def _load_dataset(reg_artifact: dict) -> pd.DataFrame:
    """Читает features_forecast_10d.parquet (или собирает нужные лаги на лету из features_satellite.parquet)."""
    if DATASET_PATH.is_file():
        df = pd.read_parquet(DATASET_PATH)
    else:
        df = pd.read_parquet(SATELLITE_PATH)
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["water_body_id", "date"]).reset_index(drop=True)
        X_opt = build_optical_features(df, prefix="", subtract_glint=True)
        df["chl_a_current_ug_l"] = np.clip(np.expm1(reg_artifact["model"].predict(X_opt)), 0.1, None)
        prev_date = df.groupby("water_body_id")["date"].shift(1)
        prev_ok = (df["date"] - prev_date).dt.days <= 12
        df["chl_a_lag10d"] = df.groupby("water_body_id")["chl_a_current_ug_l"].shift(1).where(prev_ok, np.nan)
        df["chl_a_delta_10d"] = df["chl_a_current_ug_l"] - df["chl_a_lag10d"]
    return df


def _select_lake_row(lake_df: pd.DataFrame, lake_id: str, date_str: str | None) -> pd.DataFrame:
    """Выбирает строку по точной ISO-дате YYYY-MM-DD (или ближайшей декаде в пределах <= 10 дней)."""
    if date_str is None:
        return lake_df.iloc[[-1]].copy()
    try:
        req = Date.fromisoformat(str(date_str).strip())
    except ValueError as exc:
        raise ValueError(f"Укажите полную дату в формате YYYY-MM-DD (получено: {date_str!r})") from exc

    dts = pd.to_datetime(lake_df["date"]).dt.date
    exact = lake_df[dts == req]
    if not exact.empty:
        return exact.iloc[[-1]].copy()

    diffs = dts.map(lambda d: abs((d - req).days))
    best_idx = int(diffs.idxmin())
    if int(diffs.loc[best_idx]) <= 10:
        return lake_df.loc[[best_idx]].copy()
    raise ValueError(
        f"Для озера '{lake_id}' нет данных на дату {req} (ближайшая декада: {dts.loc[best_idx]})"
    )


def predict_lake(
    lake_id: str,
    date: str | None = None,
    *,
    reg_artifact: dict | None = None,
    fc_artifact: dict | None = None,
    df: pd.DataFrame | None = None,
) -> dict:
    """
    Вызывает Модель 1 (сейчас) и Модель 2 (прогноз +10 дней) для указанного озера.
    Возвращает словарь строго по общему контракту result.json.
    """
    if reg_artifact is None:
        reg_artifact = joblib.load(SAVED_MODELS_DIR / "regressor.joblib")
    if fc_artifact is None:
        fc_artifact = joblib.load(SAVED_MODELS_DIR / "forecaster.joblib")
    if df is None:
        df = _load_dataset(reg_artifact)

    lake_df = df[df["water_body_id"] == lake_id].sort_values("date").copy()

    if lake_df.empty:
        available = sorted(df["water_body_id"].unique().tolist())
        raise ValueError(f"Озеро '{lake_id}' не найдено. Доступные: {available}")

    row_df = _select_lake_row(lake_df, lake_id, date)

    # 1. Вызов Модели 1: оценка текущей концентрации хлорофилла-а (мкг/л) + согласованная дельта
    X_opt = build_optical_features(row_df, prefix="", subtract_glint=True)
    chl_current = float(np.clip(np.expm1(reg_artifact["model"].predict(X_opt)[0]), 0.1, None))
    row_df["chl_a_current_ug_l"] = chl_current
    if "chl_a_lag10d" in row_df.columns:
        lag_val = row_df["chl_a_lag10d"].iloc[0]
        row_df["chl_a_delta_10d"] = chl_current - float(lag_val) if pd.notna(lag_val) else np.nan

    # 2. Вызов Модели 2: прогноз концентрации на следующую декаду
    X_fc = row_df[fc_artifact["feature_cols"]]
    pred_log = float(fc_artifact["model"].predict(X_fc)[0])
    chl_pred = float(np.clip(np.expm1(pred_log), 0.1, None))

    mae_margin = float(fc_artifact["test_mae_ug_l"])
    chl_low = max(0.1, chl_pred - mae_margin)
    chl_high = chl_pred + mae_margin

    # 3. Вероятности превышения порогов 15 и 25 мкг/л (из распределения ошибок Модели 2)
    log_std = float(fc_artifact["log_residual_std"])
    attention_prob = float(norm.sf(np.log1p(ATTENTION_THRESHOLD_UG_L), loc=pred_log, scale=log_std))
    bloom_prob = float(norm.sf(np.log1p(BLOOM_THRESHOLD_UG_L), loc=pred_log, scale=log_std))

    row = row_df.iloc[0]
    base_date_str = str(row["date"])[:10]
    forecast_date_str = _next_decade_date(base_date_str)

    return {
        "water_body_id": lake_id,
        "base_date": base_date_str,
        "chl_a_current_ug_l": round(chl_current, 2),
        "date": forecast_date_str,
        "chl_a_pred_ug_l": round(chl_pred, 2),
        "chl_a_pred_low_ug_l": round(chl_low, 2),
        "chl_a_pred_high_ug_l": round(chl_high, 2),
        "attention_threshold_ug_l": ATTENTION_THRESHOLD_UG_L,
        "bloom_threshold_ug_l": BLOOM_THRESHOLD_UG_L,
        "attention_prob": round(attention_prob, 3),
        "bloom_prob": round(bloom_prob, 3),
        "weather_context": {
            "temp_mean_14d": _clean_float(row.get("temp_mean_14d"), 1),
            "calm_fraction_7d": _clean_float(row.get("calm_fraction_7d"), 2),
            "precip_sum_14d": _clean_float(row.get("precip_sum_14d"), 1),
        },
        "top_drivers": fc_artifact["top_drivers"][:3],
    }


def save_predictions_to_json(lake_ids: list[str] | None = None) -> dict:
    """Считает прогноз по выбранным озёрам (или всем), печатает в консоль и пишет result.json."""
    reg_artifact = joblib.load(SAVED_MODELS_DIR / "regressor.joblib")
    fc_artifact = joblib.load(SAVED_MODELS_DIR / "forecaster.joblib")
    df = _load_dataset(reg_artifact)
    if lake_ids is None:
        lake_ids = sorted(df["water_body_id"].unique().tolist())

    results = {}
    print("=== ПРОГНОЗ КОНЦЕНТРАЦИИ ХЛОРОФИЛЛА-А (СЕЙЧАС -> СЛЕДУЮЩАЯ ДЕКАДА) ===")
    print(
        f"{'Озеро':14s} | {'База':10s} | {'Прогноз на':10s} | {'Сейчас':7s} | {'Прогноз (коридор)':23s} | "
        f"{'P(>=15)':7s} | {'P(>=25)':7s}"
    )
    print("-" * 97)

    for lid in lake_ids:
        res = predict_lake(lid, reg_artifact=reg_artifact, fc_artifact=fc_artifact, df=df)
        results[lid] = res
        corridor = f"{res['chl_a_pred_ug_l']:.2f} ({res['chl_a_pred_low_ug_l']:.1f}..{res['chl_a_pred_high_ug_l']:.1f})"
        print(
            f"{lid:14s} | {res['base_date']:10s} | {res['date']:10s} | {res['chl_a_current_ug_l']:7.2f} | "
            f"{corridor:23s} | {res['attention_prob']*100:6.1f}% | {res['bloom_prob']*100:6.1f}%"
        )

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_horizon_days": 10,
        "predictions": results,
    }
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] Итоговый файл сохранён в: {RESULT_PATH}")
    return payload


if __name__ == "__main__":
    save_predictions_to_json()