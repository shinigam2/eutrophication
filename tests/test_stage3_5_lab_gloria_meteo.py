"""Проверки Этапов 3 и 5: лабораторные таблицы БГУ, мировой эталон GLORIA и погода NASA POWER."""

from __future__ import annotations

import pandas as pd

from src.etl.gloria_loader import GLORIA_PARQUET_PATH
from src.etl.lab_loader import LAB_CLEAN_PARQUET_PATH, LAB_CSV_PATH, NAROCH_APP_CSV_PATH
from src.etl.meteo_fetcher import METEO_PARQUET_PATH


def test_bsu_lab_tables_extracted_and_exact() -> None:
    assert LAB_CSV_PATH.exists()
    assert NAROCH_APP_CSV_PATH.exists()
    assert LAB_CLEAN_PARQUET_PATH.exists()

    df = pd.read_parquet(LAB_CLEAN_PARQUET_PATH)
    assert len(df) == 36  # 3 озера × 6 месяцев (V–X) × 2 года (2018–2019)
    assert set(df["water_body_id"]) == {"naroch", "myastro", "batorino"}
    assert not df.duplicated(subset=["water_body_id", "sample_date"]).any()
    assert (df["chl_a_lab_ug_l"] > 0).all()

    # Контрольная сверка нескольких реальных значений из Таблицы 2.17 бюллетеня 2019 г.
    b19_sep = df[(df["water_body_id"] == "batorino") & (df["sample_date"] == "2019-09-15")]
    assert float(b19_sep["chl_a_lab_ug_l"].iloc[0]) == 9.04
    m18_sep = df[(df["water_body_id"] == "myastro") & (df["sample_date"] == "2018-09-15")]
    assert float(m18_sep["chl_a_lab_ug_l"].iloc[0]) == 11.34

    app = pd.read_csv(NAROCH_APP_CSV_PATH)
    assert len(app) >= 100
    assert app["sample_date"].min().startswith("2004-")
    assert app["sample_date"].max().startswith("2018-")


def test_gloria_sentinel2_pairs_valid() -> None:
    assert GLORIA_PARQUET_PATH.exists()
    df = pd.read_parquet(GLORIA_PARQUET_PATH)
    assert len(df) >= 5000
    assert df["is_inland_lake"].sum() >= 3000
    for col in ("b02_blue", "b03_green", "b04_red", "b05_rededge1", "ndci", "ndti", "chl_a_ug_l"):
        assert col in df.columns
        assert df[col].notna().all()
    assert df["ndci"].between(-1.0, 1.0).all()
    assert df["ndti"].between(-1.0, 1.0).all()


def test_meteo_clean_complete_and_in_bounds() -> None:
    assert METEO_PARQUET_PATH.exists()
    df = pd.read_parquet(METEO_PARQUET_PATH)
    assert df["water_body_id"].nunique() >= 8
    assert not df.duplicated(subset=["water_body_id", "date"]).any()
    assert df["date"].min() == "2018-01-01"
    assert df["date"].max() >= "2026-11-15"
    assert df["temp_mean_c"].isna().sum() == 0
    assert df["temp_mean_c"].between(-45.0, 45.0).all()
    assert (df["wind_speed_ms"] >= 0.0).all()
    assert (df["precip_mm"] >= 0.0).all()
