"""Финальные интеграционные тесты Этапов 6–7: схема витрин, качество и СТРОГОЕ отсутствие утечки будущего."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.etl.build_features import (
    DATASET_PASSPORT_PATH,
    FEATURES_LAB_PARQUET,
    FEATURES_SAT_PARQUET,
)
from src.etl.meteo_fetcher import METEO_PARQUET_PATH
from src.etl.validate_data import DECADAL_PARQUET_PATH, QUALITY_REPORT_PATH


def test_outputs_and_passport_exist_and_consistent() -> None:
    for p in (
        DECADAL_PARQUET_PATH,
        QUALITY_REPORT_PATH,
        FEATURES_SAT_PARQUET,
        FEATURES_LAB_PARQUET,
        DATASET_PASSPORT_PATH,
    ):
        assert p.exists(), f"Отсутствует обязательный выходной файл: {p}"

    passport = json.loads(DATASET_PASSPORT_PATH.read_text(encoding="utf-8"))
    sat = pd.read_parquet(FEATURES_SAT_PARQUET)
    lab = pd.read_parquet(FEATURES_LAB_PARQUET)

    assert len(sat) == passport["satellite_mart"]["total_valid_rows"]
    assert int(sat["is_trainable_target"].sum()) == (
        passport["satellite_mart"]["trainable_rows_with_valid_next_10d_target"]
    )
    assert len(lab) == passport["lab_mart"]["total_rows"]

    # Проверяем, что все объявленные в паспорте колонки реально присутствуют
    for group in ("meta_columns", "optical_band_columns", "feature_columns", "target_columns"):
        for col in passport["satellite_mart"][group]:
            assert col in sat.columns, f"Колонка {col} из паспорта ({group}) отсутствует в витрине!"


def test_no_duplicates_and_physical_bounds() -> None:
    sat = pd.read_parquet(FEATURES_SAT_PARQUET)
    assert not sat.duplicated(subset=["water_body_id", "date"]).any()
    assert (sat["obs_status"] == "valid").all()
    assert sat["ndci_current"].between(-1.0, 1.0).all()
    assert sat["ndti_current"].between(-1.0, 1.0).all()
    assert sat["calm_fraction_7d"].between(0.0, 1.0).all()
    assert (sat["precip_sum_7d"] >= 0.0).all()
    assert (sat["precip_sum_14d"] >= 0.0).all()
    assert sat["hot_days_14d"].between(0.0, 14.0).all()
    assert (sat["valid_water_pixels"] >= 20).all()

    trainable = sat[sat["is_trainable_target"]]
    assert len(trainable) >= 500
    for tcol in (
        "target_ndci_next_10d",
        "target_b04_b02_next_10d",
        "target_chl_a_est_next_10d",
        "target_attention_risk_next_10d",
        "target_bloom_risk_next_10d",
    ):
        assert trainable[tcol].notna().all(), f"NaN в обучающей цели {tcol}!"


def test_zero_future_leakage_satellite_and_targets() -> None:
    """Строгая математическая проверка отсутствия утечки будущего в витрине признаков."""
    sat = pd.read_parquet(FEATURES_SAT_PARQUET)
    dec = pd.read_parquet(DECADAL_PARQUET_PATH)

    # 1. Последний использованный спутниковый снимок для среза date всегда строго <= date
    assert (pd.to_datetime(sat["last_scene_date"]) <= pd.to_datetime(sat["date"])).all(), (
        "Обнаружена утечка будущего: снимок снят позже даты декады!"
    )

    # 2. Целевая декада всегда строго позже date и не дальше 12 дней
    trainable = sat[sat["is_trainable_target"]].copy()
    dt_cur = pd.to_datetime(trainable["date"])
    dt_tgt = pd.to_datetime(trainable["target_date_next_10d"])
    delta_days = (dt_tgt - dt_cur).dt.days
    assert (delta_days > 0).all() and (delta_days <= 12).all()

    # 3. Целевая декада в satellite_decadal обязана иметь obs_status == 'valid' (НИКОГДА не интерполяция!)
    dec_status = dec.set_index(["water_body_id", "date"])["obs_status"]
    dec_ndci = dec.set_index(["water_body_id", "date"])["ndci"]
    for _, row in trainable.sample(min(50, len(trainable)), random_state=42).iterrows():
        key = (row["water_body_id"], row["target_date_next_10d"])
        assert dec_status.loc[key] == "valid", "Цель взята из интерполированной декады!"
        assert row["target_ndci_next_10d"] == pytest.approx(float(dec_ndci.loc[key]))


def test_zero_future_leakage_meteo_rolling_windows() -> None:
    """Сверяет скользящие погодные окна в витрине с ручным расчётом строго по прошлым дням."""
    sat = pd.read_parquet(FEATURES_SAT_PARQUET)
    meteo = pd.read_parquet(METEO_PARQUET_PATH)
    meteo["dt"] = pd.to_datetime(meteo["date"])

    sample_rows = sat.sample(min(15, len(sat)), random_state=7)
    for _, row in sample_rows.iterrows():
        lid = row["water_body_id"]
        d = pd.Timestamp(row["date"])
        m_sub = meteo[meteo["water_body_id"] == lid]

        win7 = m_sub[(m_sub["dt"] > d - pd.Timedelta(days=7)) & (m_sub["dt"] <= d)]
        win14 = m_sub[(m_sub["dt"] > d - pd.Timedelta(days=14)) & (m_sub["dt"] <= d)]

        assert len(win7) == 7
        assert len(win14) == 14
        assert row["temp_mean_7d"] == pytest.approx(float(win7["temp_mean_c"].mean()), abs=1e-3)
        assert row["precip_sum_14d"] == pytest.approx(float(win14["precip_mm"].sum()), abs=1e-2)
        assert row["calm_fraction_7d"] == pytest.approx(
            float((win7["wind_speed_ms"] < 2.2).mean()), abs=1e-4
        )


def test_bsu_2018_2019_lab_mart_and_quality_report() -> None:
    lab = pd.read_parquet(FEATURES_LAB_PARQUET)
    assert len(lab) == 36
    assert (lab["matched_s2_scenes_pm10d"] > 0).all()
    assert lab["temp_mean_14d"].notna().all()

    qr = json.loads(QUALITY_REPORT_PATH.read_text(encoding="utf-8"))
    matchup = qr["lab_and_gloria_summary"]["bsu_2018_2019_sentinel2_matchup"]
    assert matchup["matched_samples"] == 36
    # Подтверждаем, что наш оптический сигнал B04/B02 сильно коррелирует с реальной пробиркой БГУ
    assert matchup["spearman_b04_b02_vs_lab_chl_a"] > 0.70
