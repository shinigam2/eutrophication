import argparse
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
DECADAL_PATH = ROOT_DIR / "data/interim/satellite_decadal.parquet"
RESULT_PATH = ROOT_DIR / "data/processed/result.json"

ATTENTION_THRESHOLD_UG_L = 15.0
BLOOM_THRESHOLD_UG_L = 25.0
MISSING_OBSERVATION_STATUSES = ("ice_cover", "cloudy_missing", "insufficient_pixels")


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


def _load_decadal() -> pd.DataFrame | None:
    """Читает полную декадную ленту satellite_decadal.parquet (включая ice_cover, cloudy_missing, interpolated_1step)."""
    if not DECADAL_PATH.is_file():
        return None
    dec = pd.read_parquet(DECADAL_PATH).copy()
    dec["date"] = pd.to_datetime(dec["date"])
    return dec.sort_values(["water_body_id", "date"]).reset_index(drop=True)


def _parse_iso_date(date_str: str) -> Date:
    try:
        return Date.fromisoformat(str(date_str).strip())
    except ValueError as exc:
        raise ValueError(f"Укажите полную дату в формате YYYY-MM-DD (получено: {date_str!r})") from exc


def _select_lake_row(lake_df: pd.DataFrame, lake_id: str, date_str: str | None) -> pd.DataFrame:
    """Выбирает строку по точной ISO-дате YYYY-MM-DD (или ближайшей декаде в пределах <= 10 дней)."""
    if date_str is None:
        return lake_df.iloc[[-1]].copy()
    req = _parse_iso_date(date_str)

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


def _missing_or_interpolated_record(
    lake_id: str,
    base_date_str: str,
    obs_status: str,
    chl_current: float | None = None,
) -> dict:
    """Формирует честную запись result.json для пропуска (ice_cover/cloudy_missing/...) или 1-шаговой интерполяции."""
    return {
        "water_body_id": lake_id,
        "base_date": base_date_str,
        "obs_status": obs_status,
        "chl_a_current_ug_l": None if chl_current is None else round(chl_current, 2),
        "date": None,
        "chl_a_pred_ug_l": None,
        "chl_a_pred_low_ug_l": None,
        "chl_a_pred_high_ug_l": None,
        "attention_threshold_ug_l": ATTENTION_THRESHOLD_UG_L,
        "bloom_threshold_ug_l": BLOOM_THRESHOLD_UG_L,
        "attention_prob": None,
        "bloom_prob": None,
        "weather_context": {
            "temp_mean_14d": None,
            "calm_fraction_7d": None,
            "precip_sum_14d": None,
        },
        "top_drivers": [],
    }


def predict_lake(
    lake_id: str,
    date: str | None = None,
    *,
    reg_artifact: dict | None = None,
    fc_artifact: dict | None = None,
    df: pd.DataFrame | None = None,
    decadal_df: pd.DataFrame | None = None,
) -> dict:
    """
    Вызывает Модель 1 (сейчас) и Модель 2 (прогноз +10 дней) для указанного озера.
    Если в полной декадной ленте (satellite_decadal.parquet) на выбранную дату стоит
    пропуск (ice_cover / cloudy_missing / insufficient_pixels) или interpolated_1step,
    честно возвращает obs_status и null вместо выдуманного прогноза — чтобы Часть 3
    могла фиксировать пропуски в истории графа знаний.
    """
    if reg_artifact is None:
        reg_artifact = joblib.load(SAVED_MODELS_DIR / "regressor.joblib")
    if fc_artifact is None:
        fc_artifact = joblib.load(SAVED_MODELS_DIR / "forecaster.joblib")
    if df is None:
        df = _load_dataset(reg_artifact)
    if decadal_df is None:
        decadal_df = _load_decadal()

    lake_df = df[df["water_body_id"] == lake_id].sort_values("date").copy()
    lake_dec = (
        decadal_df[decadal_df["water_body_id"] == lake_id].sort_values("date").copy()
        if decadal_df is not None
        else pd.DataFrame()
    )

    if lake_df.empty and lake_dec.empty:
        available = sorted(df["water_body_id"].unique().tolist())
        raise ValueError(f"Озеро '{lake_id}' не найдено. Доступные: {available}")

    # Проверяем полную декадную ленту Части 1, чтобы не терять статусы ice_cover / cloudy_missing / interpolated_1step
    if not lake_dec.empty:
        dec_row_df = _select_lake_row(lake_dec, lake_id, date)
        dec_row = dec_row_df.iloc[0]
        dec_date_str = str(dec_row["date"])[:10]
        obs_status = str(dec_row.get("obs_status") or "valid")

        if obs_status in MISSING_OBSERVATION_STATUSES:
            return _missing_or_interpolated_record(lake_id, dec_date_str, obs_status, chl_current=None)

        if obs_status == "interpolated_1step":
            chl_interp = None
            if {"b02_blue", "b03_green", "b04_red"} <= set(dec_row_df.columns):
                X_opt = build_optical_features(dec_row_df, prefix="", subtract_glint=True)
                if not X_opt.isna().any().any():
                    chl_interp = float(np.clip(np.expm1(reg_artifact["model"].predict(X_opt)[0]), 0.1, None))
            return _missing_or_interpolated_record(lake_id, dec_date_str, obs_status, chl_current=chl_interp)

        # obs_status == "valid": берём точно ту же декаду из витрины признаков
        valid_match = lake_df[pd.to_datetime(lake_df["date"]).dt.strftime("%Y-%m-%d") == dec_date_str]
        if valid_match.empty:
            return _missing_or_interpolated_record(lake_id, dec_date_str, "cloudy_missing", chl_current=None)
        row_df = valid_match.iloc[[-1]].copy()
    else:
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
        "obs_status": "valid",
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


def save_predictions_to_json(lake_ids: list[str] | None = None, date: str | None = None) -> dict:
    """Считает срез и прогноз по выбранным озёрам (или всем), печатает в консоль и пишет result.json."""
    reg_artifact = joblib.load(SAVED_MODELS_DIR / "regressor.joblib")
    fc_artifact = joblib.load(SAVED_MODELS_DIR / "forecaster.joblib")
    df = _load_dataset(reg_artifact)
    decadal_df = _load_decadal()
    if lake_ids is None:
        source_ids = set(df["water_body_id"].unique().tolist())
        if decadal_df is not None:
            source_ids |= set(decadal_df["water_body_id"].unique().tolist())
        lake_ids = sorted(source_ids)

    results = {}
    print("=== ПРОГНОЗ КОНЦЕНТРАЦИИ ХЛОРОФИЛЛА-А (СЕЙЧАС -> СЛЕДУЮЩАЯ ДЕКАДА) ===")
    print(
        f"{'Озеро':14s} | {'База':10s} | {'Статус':18s} | {'Прогноз на':10s} | {'Сейчас':7s} | {'Прогноз (коридор)':23s} | "
        f"{'P(>=15)':7s} | {'P(>=25)':7s}"
    )
    print("-" * 118)

    for lid in lake_ids:
        res = predict_lake(lid, date, reg_artifact=reg_artifact, fc_artifact=fc_artifact, df=df, decadal_df=decadal_df)
        results[lid] = res
        status = res.get("obs_status", "valid")
        curr_s = f"{res['chl_a_current_ug_l']:7.2f}" if res["chl_a_current_ug_l"] is not None else f"{'—':>7s}"
        if res["chl_a_pred_ug_l"] is not None:
            fc_date_s = f"{res['date']:10s}"
            corridor = f"{res['chl_a_pred_ug_l']:.2f} ({res['chl_a_pred_low_ug_l']:.1f}..{res['chl_a_pred_high_ug_l']:.1f})"
            att_s = f"{res['attention_prob']*100:6.1f}%"
            blm_s = f"{res['bloom_prob']*100:6.1f}%"
        else:
            fc_date_s = f"{'—':10s}"
            corridor = "— (нет прогноза)"
            att_s = f"{'—':>7s}"
            blm_s = f"{'—':>7s}"
        print(
            f"{lid:14s} | {res['base_date']:10s} | {status:18s} | {fc_date_s} | {curr_s} | "
            f"{corridor:23s} | {att_s} | {blm_s}"
        )

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_horizon_days": 10,
        "predictions": results,
    }
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] Итоговый файл сохранён в: {RESULT_PATH}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Прогноз концентрации Chl-a (+10 дней) и сохранение result.json")
    parser.add_argument("--lake", help="ID одного водоёма для точечного вывода (JSON в stdout без перезаписи result.json)")
    parser.add_argument("--date", help="Целевая базовая дата в формате YYYY-MM-DD (по умолчанию — последняя декада)")
    args = parser.parse_args()

    if args.lake:
        res = predict_lake(args.lake, args.date)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        save_predictions_to_json(date=args.date)


if __name__ == "__main__":
    main()