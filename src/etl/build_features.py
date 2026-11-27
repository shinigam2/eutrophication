"""Инженерия признаков и сборка финальных витрин данных Части 1 (Этап 6б).

Создаёт выходные витрины строго по DATA_CONTRACT.md:
  1. data/processed/features_satellite.parquet (+ .csv)
     Главная витрина спутникового режима (8 активных озёр, 2018–2026, шаг 10 дней).
     Содержит:
       - служебные поля (код озера, дата, статус, число пикселей, последний scene_id);
       - сезонность (month, day_of_year, sin_doy, cos_doy);
       - свойства озера и экологический контекст для Части 3 (площадь, глубины, удельный водосбор);
       - реальное оптическое состояние сейчас и 10 дней назад (7 каналов B02..B11,
         NDCI, NDTI, B03/B02, B04/B02, B05/B04, Chl-a Mishra, TSI, лаги и темп роста);
       - накопленную погоду NASA POWER строго ДО даты среза (окна 7, 14, 21 день);
       - целевые переменные ровно на следующую декаду (+10 дней) и флаг `is_trainable_target`.
  2. data/processed/features_lab.parquet (+ .csv)
     Витрина лабораторного режима (Нарочь, Мястро, Баторино, 36 реальных проб БГУ 2018–2019),
     соединённая с ближайшими чистыми снимками Sentinel-2 (±10 дней) и строгими погодными окнами.
  3. data/processed/dataset_passport.json
     Автоматический паспорт витрин для Участников 2, 3 и 4 (списки колонок, объёмы, периоды).

Гарантия отсутствия утечки будущего (Zero Future Leakage):
  - Погодные скользящие окна на дату `D` считаются строго по суткам `[D - (W-1), D]`.
  - В витрину попадают только строки с `obs_status == 'valid'` (реальный снимок <= D).
  - Целевые колонки `target_*_next_10d` заполняются ТОЛЬКО если следующая декада (D + ~10д)
    также имеет `obs_status == 'valid'` (никогда не предсказываем интерполированное значение!).

Запуск:
  python -m src.etl.build_features
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.etl.config import PROCESSED_DIR, WaterBodyConfig, load_config
from src.etl.lab_loader import LAB_CLEAN_PARQUET_PATH
from src.etl.meteo_fetcher import METEO_PARQUET_PATH
from src.etl.sentinel_fetcher import SCENES_PARQUET_PATH
from src.etl.validate_data import DECADAL_PARQUET_PATH, MAX_B11_HAZE_THRESHOLD

FEATURES_SAT_PARQUET = PROCESSED_DIR / "features_satellite.parquet"
FEATURES_SAT_CSV = PROCESSED_DIR / "features_satellite.csv"
FEATURES_LAB_PARQUET = PROCESSED_DIR / "features_lab.parquet"
FEATURES_LAB_CSV = PROCESSED_DIR / "features_lab.csv"
DATASET_PASSPORT_PATH = PROCESSED_DIR / "dataset_passport.json"

# Пороги для погодных признаков (соответствуют DATA_CONTRACT.md)
HOT_DAY_TEMP_C = 22.0
CALM_WIND_MS = 2.2


def compute_daily_meteo_windows(meteo_df: pd.DataFrame) -> pd.DataFrame:
    """Считает строго прошлые скользящие погодные окна (7, 14, 21 день) для каждого озера и дня."""
    out_parts: list[pd.DataFrame] = []
    for lid, grp in meteo_df.groupby("water_body_id", sort=True):
        g = grp.sort_values("date").copy().reset_index(drop=True)
        # Все rolling-окна в pandas по умолчанию правосторонние: [t - W + 1 .. t] -> СТРОГО <= t!
        g["temp_mean_7d"] = g["temp_mean_c"].rolling(7, min_periods=5).mean().round(3)
        g["temp_mean_14d"] = g["temp_mean_c"].rolling(14, min_periods=10).mean().round(3)
        g["temp_mean_21d"] = g["temp_mean_c"].rolling(21, min_periods=15).mean().round(3)
        g["temp_max_7d"] = g["temp_max_c"].rolling(7, min_periods=5).max().round(3)
        g["hot_days_14d"] = (
            (g["temp_max_c"] >= HOT_DAY_TEMP_C).astype(float).rolling(14, min_periods=10).sum().round(1)
        )
        g["precip_sum_7d"] = g["precip_mm"].rolling(7, min_periods=5).sum().round(2)
        g["precip_sum_14d"] = g["precip_mm"].rolling(14, min_periods=10).sum().round(2)
        g["radiation_mean_7d"] = g["solar_rad_mj"].rolling(7, min_periods=5).mean().round(3)
        g["wind_mean_7d"] = g["wind_speed_ms"].rolling(7, min_periods=5).mean().round(3)
        g["calm_fraction_7d"] = (
            (g["wind_speed_ms"] < CALM_WIND_MS).astype(float).rolling(7, min_periods=5).mean().round(4)
        )
        out_parts.append(
            g[
                [
                    "water_body_id",
                    "date",
                    "temp_mean_7d",
                    "temp_mean_14d",
                    "temp_mean_21d",
                    "temp_max_7d",
                    "hot_days_14d",
                    "precip_sum_7d",
                    "precip_sum_14d",
                    "radiation_mean_7d",
                    "wind_mean_7d",
                    "calm_fraction_7d",
                ]
            ]
        )
    return pd.concat(out_parts, ignore_index=True)


def _add_seasonality(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    dt = pd.to_datetime(df[date_col])
    doy = dt.dt.day_of_year
    df["month"] = dt.dt.month.astype(int)
    df["day_of_year"] = doy.astype(int)
    df["sin_doy"] = np.sin(2.0 * math.pi * doy / 365.25).round(5)
    df["cos_doy"] = np.cos(2.0 * math.pi * doy / 365.25).round(5)
    return df


def _add_lake_metadata(df: pd.DataFrame, lakes_by_id: dict[str, WaterBodyConfig]) -> pd.DataFrame:
    df["water_body_name"] = df["water_body_id"].map(lambda k: lakes_by_id[k].name_ru)
    df["area_km2"] = df["water_body_id"].map(lambda k: lakes_by_id[k].area_km2)
    df["mean_depth_m"] = df["water_body_id"].map(lambda k: lakes_by_id[k].mean_depth_m)
    df["max_depth_m"] = df["water_body_id"].map(lambda k: lakes_by_id[k].max_depth_m)
    df["watershed_ratio"] = df["water_body_id"].map(lambda k: lakes_by_id[k].watershed_ratio)
    return df


def build_satellite_feature_mart(
    decadal_df: pd.DataFrame,
    meteo_win_df: pd.DataFrame,
    lakes_by_id: dict[str, WaterBodyConfig],
) -> pd.DataFrame:
    """Строит главную витрину спутникового режима `features_satellite` без утечки будущего."""
    parts: list[pd.DataFrame] = []

    for lid, grp in decadal_df.groupby("water_body_id", sort=True):
        if lid not in lakes_by_id:
            continue
        wb = lakes_by_id[lid]
        g = grp.sort_values("date").copy().reset_index(drop=True)
        g["dt"] = pd.to_datetime(g["date"])

        # Лаги с предыдущей декады (t - 1): строго <= date (безопасно для valid и interpolated_1step,
        # т.к. interpolated_1step на t-1 опирается на t-2 и t, оба <= date!)
        prev_status = g["obs_status"].shift(1)
        prev_dt = g["dt"].shift(1)
        prev_ok = prev_status.isin(["valid", "interpolated_1step"]) & (
            (g["dt"] - prev_dt).dt.days <= 12
        )

        g["ndci_current"] = g["ndci"]
        g["ndci_lag10d"] = g["ndci"].shift(1).where(prev_ok, np.nan)
        g["ndci_growth_10d"] = (g["ndci_current"] - g["ndci_lag10d"]).round(5)

        g["ndti_current"] = g["ndti"]
        g["b03_b02_ratio_current"] = g["b03_b02_ratio"]
        g["b04_b02_ratio_current"] = g["b04_b02_ratio"]
        g["b04_b02_ratio_lag10d"] = g["b04_b02_ratio"].shift(1).where(prev_ok, np.nan)
        g["b04_b02_growth_10d"] = (
            g["b04_b02_ratio_current"] - g["b04_b02_ratio_lag10d"]
        ).round(4)
        g["b05_b04_ratio_current"] = g["b05_b04_ratio"]

        g["chl_a_est_current"] = g["chl_a_est_ug_l"]
        g["chl_a_est_lag10d"] = g["chl_a_est_ug_l"].shift(1).where(prev_ok, np.nan)
        g["tsi_current"] = g["tsi_est"]

        # Целевые переменные ровно на следующую декаду (t + 1):
        # ЗАПОЛНЯЮТСЯ ТОЛЬКО ЕСЛИ t+1 — РЕАЛЬНОЕ НАБЛЮДЕНИЕ (`valid`) в пределах <= 12 дней!
        next_status = g["obs_status"].shift(-1)
        next_dt = g["dt"].shift(-1)
        next_valid = (next_status == "valid") & ((next_dt - g["dt"]).dt.days <= 12)

        g["target_date_next_10d"] = g["date"].shift(-1).where(next_valid, None)
        g["target_ndci_next_10d"] = g["ndci"].shift(-1).where(next_valid, np.nan)
        g["target_b04_b02_next_10d"] = g["b04_b02_ratio"].shift(-1).where(next_valid, np.nan)
        g["target_chl_a_est_next_10d"] = (
            g["chl_a_est_ug_l"].shift(-1).where(next_valid, np.nan)
        )
        g["target_attention_risk_next_10d"] = np.where(
            next_valid,
            (g["chl_a_est_ug_l"].shift(-1) >= wb.thresholds.attention_ug_l).astype(float),
            np.nan,
        )
        g["target_bloom_risk_next_10d"] = np.where(
            next_valid,
            (g["chl_a_est_ug_l"].shift(-1) >= wb.thresholds.bloom_ug_l).astype(float),
            np.nan,
        )
        g["is_trainable_target"] = next_valid

        # Оставляем ТОЛЬКО реальные спутниковые срезы (`valid`), чтобы исключить любую утечку
        # из двусторонней интерполяции и не включать зимний лёд / облака в витрину ML
        g = g[g["obs_status"] == "valid"].copy()
        parts.append(g)

    sat_df = pd.concat(parts, ignore_index=True)
    sat_df = sat_df.drop(columns=["dt"])

    # Приклеиваем строго прошлые погодные окна по (water_body_id, date)
    sat_df = sat_df.merge(meteo_win_df, on=["water_body_id", "date"], how="left")
    sat_df = _add_seasonality(sat_df, "date")
    sat_df = _add_lake_metadata(sat_df, lakes_by_id)

    ordered_cols = [
        # Группа А. Служебная информация
        "water_body_id",
        "water_body_name",
        "date",
        "obs_status",
        "valid_water_pixels",
        "water_coverage_pct",
        "scenes_in_decade",
        "sun_glint_flag",
        "last_scene_id",
        "last_scene_date",
        "is_trainable_target",
        "target_date_next_10d",
        # Группа Б1. Сезонность
        "month",
        "day_of_year",
        "sin_doy",
        "cos_doy",
        # Группа Б2. Свойства озера
        "area_km2",
        "mean_depth_m",
        "max_depth_m",
        "watershed_ratio",
        # Группа Б3. Сырые каналы Sentinel-2 (для 6-канального переводчика GLORIA в Части 2)
        "b02_blue",
        "b03_green",
        "b04_red",
        "b05_rededge1",
        "b06_rededge2",
        "b08_nir",
        "b11_swir1",
        # Группа Б4. Оптические индексы, отношения и лаги
        "ndci_current",
        "ndci_lag10d",
        "ndci_growth_10d",
        "ndti_current",
        "b03_b02_ratio_current",
        "b04_b02_ratio_current",
        "b04_b02_ratio_lag10d",
        "b04_b02_growth_10d",
        "b05_b04_ratio_current",
        "chl_a_est_current",
        "chl_a_est_lag10d",
        "tsi_current",
        # Группа Б5. Накопленная погода (строго ДО даты среза)
        "temp_mean_7d",
        "temp_mean_14d",
        "temp_mean_21d",
        "temp_max_7d",
        "hot_days_14d",
        "precip_sum_7d",
        "precip_sum_14d",
        "radiation_mean_7d",
        "wind_mean_7d",
        "calm_fraction_7d",
        # Группа В. Целевые переменные (+10 дней вперёд, строго реальные valid декады)
        "target_ndci_next_10d",
        "target_b04_b02_next_10d",
        "target_chl_a_est_next_10d",
        "target_attention_risk_next_10d",
        "target_bloom_risk_next_10d",
    ]
    sat_df = sat_df[ordered_cols].sort_values(["water_body_id", "date"]).reset_index(drop=True)
    return sat_df


def build_lab_feature_mart(
    lab_df: pd.DataFrame,
    scenes_df: pd.DataFrame,
    meteo_win_df: pd.DataFrame,
    lakes_by_id: dict[str, WaterBodyConfig],
) -> pd.DataFrame:
    """Строит витрину лабораторного режима `features_lab` (БГУ 2018–2019 + Sentinel-2 ±10д + погода)."""
    from src.etl.validate_data import _compute_mishra, _dark_artifact_mask

    s2 = scenes_df[~scenes_df["sun_glint_flag"]].copy()
    s2 = s2[s2["b11_swir1"].isna() | (s2["b11_swir1"] <= MAX_B11_HAZE_THRESHOLD)]
    s2 = s2[~_dark_artifact_mask(s2)].copy()
    s2["dt"] = pd.to_datetime(s2["scene_date"])

    opt_cols = [
        "b02_blue",
        "b03_green",
        "b04_red",
        "b05_rededge1",
        "b06_rededge2",
        "b08_nir",
        "b11_swir1",
        "ndci",
        "ndti",
        "b03_b02_ratio",
        "b04_b02_ratio",
        "b05_b04_ratio",
        "chl_a_est_ug_l",
    ]

    def _opt_float(val: Any) -> float:
        return float(val) if pd.notna(val) else np.nan

    def _opt_str(val: Any, default: str = "") -> str:
        return str(val).strip() if pd.notna(val) and str(val).strip() else default

    lab_rows: list[dict[str, Any]] = []
    for _, r in lab_df.sort_values(["water_body_id", "sample_date"]).iterrows():
        lid = str(r["water_body_id"])
        if lid not in lakes_by_id:
            continue
        s_date = str(r["sample_date"])
        s_dt = pd.Timestamp(s_date)

        # Сопоставляем с чистыми снимками Sentinel-2 в окне ±10 дней вокруг даты пробы БГУ
        sub = s2[
            (s2["water_body_id"] == lid) & ((s2["dt"] - s_dt).abs() <= pd.Timedelta(days=10))
        ]
        rec: dict[str, Any] = {
            "water_body_id": lid,
            "date": s_date,
            "chl_a_lab_ug_l": float(r["chl_a_lab_ug_l"]),
            "chl_a_filter_1_5_um_ug_l": _opt_float(r.get("chl_a_filter_1_5_um_ug_l")),
            "secchi_depth_m": _opt_float(r.get("secchi_depth_m")),
            "total_phosphorus_ug_l": _opt_float(r.get("total_phosphorus_ug_l")),
            "sample_type": _opt_str(r.get("sample_type"), "web_upload"),
            "date_precision": _opt_str(r.get("date_precision"), "exact"),
            "source_note": _opt_str(r.get("source_note"), ""),
            "matched_s2_scenes_pm10d": int(len(sub)),
        }
        for c in opt_cols:
            col_out = f"s2_{c}"
            rec[col_out] = (
                float(round(sub[c].median(), 6))
                if (len(sub) > 0 and sub[c].notna().any())
                else np.nan
            )
        if pd.notna(rec["s2_ndci"]):
            rec["s2_chl_a_est_ug_l"] = round(_compute_mishra(float(rec["s2_ndci"])), 3)
        lab_rows.append(rec)

    out_df = pd.DataFrame(lab_rows)
    out_df = out_df.merge(meteo_win_df, on=["water_body_id", "date"], how="left")
    out_df = _add_seasonality(out_df, "date")
    out_df = _add_lake_metadata(out_df, lakes_by_id)

    # Лабораторные лаги (предыдущая проба того же года) и следующая проба (+1 мес)
    out_parts: list[pd.DataFrame] = []
    for lid, grp in out_df.groupby("water_body_id", sort=True):
        g = grp.sort_values("date").copy().reset_index(drop=True)
        g["year"] = g["date"].str[:4]
        same_yr_prev = g["year"] == g["year"].shift(1)
        same_yr_next = g["year"] == g["year"].shift(-1)
        g["chl_a_lab_prev_month"] = g["chl_a_lab_ug_l"].shift(1).where(same_yr_prev, np.nan)
        g["target_chl_a_lab_next_month"] = (
            g["chl_a_lab_ug_l"].shift(-1).where(same_yr_next, np.nan)
        )
        g = g.drop(columns=["year"])
        out_parts.append(g)

    final_lab = pd.concat(out_parts, ignore_index=True)
    return final_lab


def build_all_features() -> tuple[Path, Path, Path]:
    """Собирает обе витрины (спутниковую и лабораторную) и паспорт dataset_passport.json."""
    cfg = load_config()
    active_lakes = cfg.get_lakes()
    lakes_by_id = {wb.id: wb for wb in active_lakes}

    decadal_df = pd.read_parquet(DECADAL_PARQUET_PATH)
    meteo_df = pd.read_parquet(METEO_PARQUET_PATH)
    scenes_df = pd.read_parquet(SCENES_PARQUET_PATH)
    lab_df = pd.read_parquet(LAB_CLEAN_PARQUET_PATH)

    meteo_win_df = compute_daily_meteo_windows(meteo_df)

    sat_mart = build_satellite_feature_mart(decadal_df, meteo_win_df, lakes_by_id)
    lab_mart = build_lab_feature_mart(lab_df, scenes_df, meteo_win_df, lakes_by_id)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    sat_mart.to_parquet(FEATURES_SAT_PARQUET, index=False)
    sat_mart.to_csv(FEATURES_SAT_CSV, index=False)
    lab_mart.to_parquet(FEATURES_LAB_PARQUET, index=False)
    lab_mart.to_csv(FEATURES_LAB_CSV, index=False)

    trainable = sat_mart[sat_mart["is_trainable_target"]]

    passport = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": "1.0.0",
        "active_lakes": [wb.id for wb in active_lakes],
        "satellite_mart": {
            "parquet_path": "data/processed/features_satellite.parquet",
            "csv_path": "data/processed/features_satellite.csv",
            "total_valid_rows": int(len(sat_mart)),
            "trainable_rows_with_valid_next_10d_target": int(len(trainable)),
            "date_min": str(sat_mart["date"].min()),
            "date_max": str(sat_mart["date"].max()),
            "meta_columns": [
                "water_body_id",
                "water_body_name",
                "date",
                "obs_status",
                "valid_water_pixels",
                "water_coverage_pct",
                "scenes_in_decade",
                "sun_glint_flag",
                "last_scene_id",
                "last_scene_date",
                "is_trainable_target",
                "target_date_next_10d",
            ],
            "optical_band_columns": [
                "b02_blue",
                "b03_green",
                "b04_red",
                "b05_rededge1",
                "b06_rededge2",
                "b08_nir",
                "b11_swir1",
            ],
            "feature_columns": [
                "month",
                "day_of_year",
                "sin_doy",
                "cos_doy",
                "area_km2",
                "mean_depth_m",
                "max_depth_m",
                "watershed_ratio",
                "b02_blue",
                "b03_green",
                "b04_red",
                "b05_rededge1",
                "b06_rededge2",
                "b08_nir",
                "b11_swir1",
                "ndci_current",
                "ndci_lag10d",
                "ndci_growth_10d",
                "ndti_current",
                "b03_b02_ratio_current",
                "b04_b02_ratio_current",
                "b04_b02_ratio_lag10d",
                "b04_b02_growth_10d",
                "b05_b04_ratio_current",
                "chl_a_est_current",
                "chl_a_est_lag10d",
                "tsi_current",
                "temp_mean_7d",
                "temp_mean_14d",
                "temp_mean_21d",
                "temp_max_7d",
                "hot_days_14d",
                "precip_sum_7d",
                "precip_sum_14d",
                "radiation_mean_7d",
                "wind_mean_7d",
                "calm_fraction_7d",
            ],
            "target_columns": [
                "target_ndci_next_10d",
                "target_b04_b02_next_10d",
                "target_chl_a_est_next_10d",
                "target_attention_risk_next_10d",
                "target_bloom_risk_next_10d",
            ],
            "recommended_temporal_split": {
                "train_years": [2018, 2019, 2020, 2021, 2022, 2023, 2024],
                "test_years": [2025, 2026],
                "note": "Разделение строго по годам без перемешивания; 2018–2019 также используются для валидации переводчика GLORIA по пробам БГУ.",
            },
            "rows_per_lake": sat_mart["water_body_id"].value_counts().sort_index().to_dict(),
            "trainable_rows_per_lake": trainable["water_body_id"].value_counts().sort_index().to_dict(),
        },
        "lab_mart": {
            "parquet_path": "data/processed/features_lab.parquet",
            "csv_path": "data/processed/features_lab.csv",
            "total_rows": int(len(lab_mart)),
            "lakes": sorted(lab_mart["water_body_id"].unique().tolist()),
            "date_min": str(lab_mart["date"].min()),
            "date_max": str(lab_mart["date"].max()),
            "matched_s2_rate_pct": round(
                100.0 * (lab_mart["matched_s2_scenes_pm10d"] > 0).mean(), 1
            ),
            "target_column": "chl_a_lab_ug_l",
        },
        "gloria_reference": {
            "parquet_path": "data/interim/gloria_sentinel2_pairs.parquet",
            "purpose": "Обучение 6-канального оптико-лабораторного переводчика (B02..B08 + NDCI/NDTI/отношения -> Chl-a мкг/л) в Части 2.",
        },
    }

    DATASET_PASSPORT_PATH.write_text(
        json.dumps(passport, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("=" * 100)
    print("ВИТРИНЫ ПРИЗНАКОВ ЧАСТИ 1 (2018–2026) — СОБРАНЫ И ГОТОВЫ ДЛЯ ЧАСТЕЙ 2–4")
    print("=" * 100)
    print(
        f"1) Спутниковая витрина: {len(sat_mart)} валидных декадных срезов "
        f"(из них {len(trainable)} с валидной целью +10д) | {sat_mart['date'].min()} .. {sat_mart['date'].max()}"
    )
    print(
        f"   • Признаков: {len(passport['satellite_mart']['feature_columns'])}, "
        f"целей: {len(passport['satellite_mart']['target_columns'])}"
    )
    print(
        f"   • В обучающих строках (Mishra baseline): внимание >=15 = "
        f"{int(trainable['target_attention_risk_next_10d'].sum())} ({trainable['target_attention_risk_next_10d'].mean()*100:.1f}%), "
        f"цветение >=25 = {int(trainable['target_bloom_risk_next_10d'].sum())} ({trainable['target_bloom_risk_next_10d'].mean()*100:.1f}%)"
    )
    print(f"   • Файлы: {FEATURES_SAT_PARQUET} + .csv")
    print(
        f"2) Лабораторная витрина (БГУ 2018–2019): {len(lab_mart)} проб "
        f"(совпадение со спутником ±10д: {passport['lab_mart']['matched_s2_rate_pct']}%) -> {FEATURES_LAB_PARQUET}"
    )
    print(f"3) Паспорт витрин: {DATASET_PASSPORT_PATH}")
    return FEATURES_SAT_PARQUET, FEATURES_LAB_PARQUET, DATASET_PASSPORT_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сборка финальных витрин признаков (спутниковой и лабораторной) + dataset_passport.json"
    )
    parser.parse_args()
    build_all_features()


if __name__ == "__main__":
    main()
