"""Загрузка и подготовка мирового оптико-лабораторного эталона GLORIA (Lehmann et al., 2023).

Источник данных (открытая лицензия CC-BY-4.0, без API-ключей):
  PANGAEA Archive: https://doi.pangaea.de/10.1594/PANGAEA.948492
  Прямой файл:     https://download.pangaea.de/dataset/948492/files/GLORIA-2022.zip (56,2 МБ)
  Статья:          Lehmann et al. (2023), Nature Scientific Data,
                   DOI: 10.1038/s41597-023-01973-y

Что делает этот модуль:
  1. Скачивает (если ещё нет в кэше data/raw/gloria/GLORIA-2022.zip) официальный
     архив GLORIA (7 572 гиперспектральных замера отражения воды Rrs(350..900 нм)
     из 450 водоёмов 31 страны мира с парными лабораторными анализами Chl-a).
  2. Соединяет 3 официальные таблицы архива по ключу `GLORIA_ID`:
       - GLORIA_meta_and_lab.csv (координаты, страна, тип водоёма, лабораторный Chl-a, Секки, TSS)
       - GLORIA_qc_flags.csv     (официальные флаги контроля качества спектра)
       - GLORIA_Rrs.csv          (гиперспектральные кривые Rrs с шагом 1 нм)
  3. Моделирует ровно те же 6 каналов спутника Sentinel-2 MSI, усредняя Rrs по
     официальным спектральным окнам (FWHM) сенсора ESA и переводя коэффициент
     яркости воды Rrs (ср⁻¹) в безразмерное поверхностное отражение ρ = π · Rrs:
       - b02_blue     (459–525 нм, центр 492 нм)
       - b03_green    (542–578 нм, центр 560 нм)
       - b04_red      (650–680 нм, центр 665 нм)
       - b05_rededge1 (697–711 нм, центр 704 нм)
       - b06_rededge2 (733–747 нм, центр 740 нм)
       - b08_nir      (780–886 нм, центр 833 нм)
  4. Считает точно те же оптические индексы и отношения (`ndci`, `ndti`, `b03_b02_ratio`,
     `b04_b02_ratio`, `b05_b04_ratio`, `b06_b04_ratio`), что и `sentinel_fetcher.py`.
  5. Отбрасывает спектры с критическими флагами брака (`Suspect`, `Baseline_shift`,
     `Noisy_red`, `Noisy_blue`, `QWIP_fail`) и строки без лабораторного хлорофилла.
  6. Сохраняет чистую обучающую таблицу для оптико-лабораторного переводчика (Часть 2):
       data/interim/gloria_sentinel2_pairs.parquet

Запуск:
  python -m src.etl.gloria_loader
"""

from __future__ import annotations

import argparse
import math
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from src.etl.config import INTERIM_DIR, RAW_DIR

GLORIA_URL = "https://download.pangaea.de/dataset/948492/files/GLORIA-2022.zip"
GLORIA_RAW_DIR = RAW_DIR / "gloria"
GLORIA_ZIP_PATH = GLORIA_RAW_DIR / "GLORIA-2022.zip"
GLORIA_PARQUET_PATH = INTERIM_DIR / "gloria_sentinel2_pairs.parquet"

USER_AGENT = "BelarusLakeEutrophicationETL/1.0 (BSUIR/BSU coursework research)"

# Официальные спектральные диапазоны (FWHM, нм) каналов Sentinel-2A/2B MSI (ESA)
S2_BAND_WINDOWS_NM: dict[str, tuple[int, int]] = {
    "b02_blue": (459, 525),
    "b03_green": (542, 578),
    "b04_red": (650, 680),
    "b05_rededge1": (697, 711),
    "b06_rededge2": (733, 747),
    "b08_nir": (780, 886),
}

# Критические флаги брака спектра в GLORIA_qc_flags.csv
# (Oxygen_signal на 760 нм не мешает нашим каналам Sentinel-2, поэтому отдельно не бракуем)
CRITICAL_QC_FLAGS = [
    "Noisy_red",
    "Noisy_blue",
    "Baseline_shift",
    "QWIP_fail",
    "Suspect",
]

WATER_BODY_TYPE_NAMES: dict[int, str] = {
    1: "lake",
    2: "estuary",
    3: "coastal",
    4: "river",
    5: "other",
}


def download_gloria_zip(force_refresh: bool = False) -> Path:
    """Скачивает GLORIA-2022.zip с PANGAEA в data/raw/gloria/ (с кэшированием)."""
    GLORIA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    if GLORIA_ZIP_PATH.exists() and GLORIA_ZIP_PATH.stat().st_size > 40_000_000 and not force_refresh:
        return GLORIA_ZIP_PATH

    print(f"[GLORIA] Скачивание {GLORIA_URL} (56,2 МБ)...")
    with requests.get(
        GLORIA_URL, headers={"User-Agent": USER_AGENT}, stream=True, timeout=120
    ) as resp:
        resp.raise_for_status()
        tmp_path = GLORIA_ZIP_PATH.with_suffix(".zip.part")
        with tmp_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
        tmp_path.rename(GLORIA_ZIP_PATH)
    print(f"[GLORIA] Архив сохранён: {GLORIA_ZIP_PATH}")
    return GLORIA_ZIP_PATH


def _compute_s2_bands_from_rrs(rrs_df: pd.DataFrame) -> pd.DataFrame:
    """Синтезирует 6 каналов Sentinel-2 (ρ = π · Rrs) из 1-нм гиперспектра GLORIA."""
    out = pd.DataFrame({"GLORIA_ID": rrs_df["GLORIA_ID"]})
    for col_name, (wl_min, wl_max) in S2_BAND_WINDOWS_NM.items():
        wl_cols = [
            f"Rrs_{wl}" for wl in range(wl_min, wl_max + 1) if f"Rrs_{wl}" in rrs_df.columns
        ]
        # Среднее по полосе пропускания Sentinel-2; переводим Rrs [sr^-1] -> BOA reflectance ρ = π * Rrs
        arr = rrs_df[wl_cols].apply(pd.to_numeric, errors="coerce")
        # Требуем, чтобы хотя бы половина длин волн внутри полосы была измерена
        min_valid = max(1, len(wl_cols) // 2)
        mean_rrs = arr.mean(axis=1, skipna=True)
        mean_rrs = mean_rrs.where(arr.notna().sum(axis=1) >= min_valid, np.nan)
        out[col_name] = (mean_rrs * math.pi).round(6)
    return out


def build_gloria_sentinel2_dataset(force_refresh: bool = False) -> Path:
    """Строит и сохраняет чистую парную таблицу GLORIA -> Sentinel-2 для обучения переводчика."""
    zip_path = download_gloria_zip(force_refresh=force_refresh)

    print(f"[GLORIA] Чтение таблиц из {zip_path}...")
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("GLORIA_2022/GLORIA_meta_and_lab.csv") as f:
            meta = pd.read_csv(f, low_memory=False)
        with zf.open("GLORIA_2022/GLORIA_qc_flags.csv") as f:
            qc = pd.read_csv(f, low_memory=False)
        with zf.open("GLORIA_2022/GLORIA_Rrs.csv") as f:
            rrs = pd.read_csv(f, low_memory=False)

    print(
        f"[GLORIA] Прочитано: meta={len(meta)}, qc={len(qc)}, rrs={len(rrs)} записей."
    )

    # Синтезируем каналы Sentinel-2 (B02..B08)
    bands = _compute_s2_bands_from_rrs(rrs)

    # Флаг чистоты спектра по официальной таблице QC GLORIA
    qc_clean = qc[["GLORIA_ID"]].copy()
    for flag in CRITICAL_QC_FLAGS:
        if flag in qc.columns:
            qc_clean[flag] = pd.to_numeric(qc[flag], errors="coerce").fillna(0)
        else:
            qc_clean[flag] = 0
    qc_clean["qc_passed"] = (qc_clean[CRITICAL_QC_FLAGS].sum(axis=1) == 0)

    # Собираем нужные метаданные и лабораторные пробы
    keep_meta_cols = [
        "GLORIA_ID",
        "Country",
        "Site_name",
        "Latitude",
        "Longitude",
        "Date_Time_UTC",
        "Water_body_type",
        "Chla",
        "Chla_plus_phaeo",
        "TSS",
        "Secchi_depth",
        "aCDOM440",
    ]
    sub_meta = meta[keep_meta_cols].copy()
    for num_col in ("Latitude", "Longitude", "Chla", "Chla_plus_phaeo", "TSS", "Secchi_depth", "aCDOM440"):
        sub_meta[num_col] = pd.to_numeric(sub_meta[num_col], errors="coerce")

    df = (
        sub_meta.merge(qc_clean[["GLORIA_ID", "qc_passed"]], on="GLORIA_ID", how="left")
        .merge(bands, on="GLORIA_ID", how="inner")
    )

    # Единая целевая переменная хлорофилла-а:
    # приоритет — очищенный `Chla`, а если его нет — `Chla_plus_phaeo` (с явной пометкой метода!)
    df["chl_a_ug_l"] = df["Chla"].combine_first(df["Chla_plus_phaeo"])
    df["chl_a_method_type"] = np.where(
        df["Chla"].notna(),
        "chla_pure",
        np.where(df["Chla_plus_phaeo"].notna(), "chla_plus_phaeo", "missing"),
    )

    # Фильтр качества: чистый спектр, валидный хлорофилл (0.05..500 мкг/л),
    # и физически положительные базовые каналы B02, B03, B04, B05 (нужны для NDCI/NDTI и отношений)
    total_raw = len(df)
    valid_mask = (
        df["qc_passed"].fillna(False)
        & df["chl_a_ug_l"].notna()
        & (df["chl_a_ug_l"] >= 0.05)
        & (df["chl_a_ug_l"] <= 500.0)
        & (df["b02_blue"] > 0.0)
        & (df["b03_green"] > 0.0)
        & (df["b04_red"] > 0.0)
        & (df["b05_rededge1"] > 0.0)
    )
    df = df.loc[valid_mask].copy().reset_index(drop=True)

    # Очищаем нефизичные отрицательные отражения в доп. каналах B06/B08 и нулевую прозрачность Секки
    df.loc[df["b06_rededge2"] <= 0.0, "b06_rededge2"] = np.nan
    df.loc[df["b08_nir"] <= 0.0, "b08_nir"] = np.nan
    df.loc[df["Secchi_depth"] <= 0.0, "Secchi_depth"] = np.nan

    # Вычисляем точно те же оптические признаки, что и на реальных снимках Sentinel-2
    df["ndci"] = ((df["b05_rededge1"] - df["b04_red"]) / (df["b05_rededge1"] + df["b04_red"])).round(5)
    df["ndti"] = ((df["b04_red"] - df["b03_green"]) / (df["b04_red"] + df["b03_green"])).round(5)
    df["b03_b02_ratio"] = (df["b03_green"] / df["b02_blue"]).round(4)
    df["b04_b02_ratio"] = (df["b04_red"] / df["b02_blue"]).round(4)
    df["b05_b04_ratio"] = (df["b05_rededge1"] / df["b04_red"]).round(4)
    df["b06_b04_ratio"] = np.where(
        df["b06_rededge2"] > 0, (df["b06_rededge2"] / df["b04_red"]).round(4), np.nan
    )
    # Базовая оценка Mishra & Mishra (2012) с монотонным ограничением в вершине параболы (-0.2216)
    ndci_eff = np.maximum(df["ndci"], -86.115 / (2.0 * 194.325))
    df["chl_a_mishra_ug_l"] = np.maximum(
        0.5, 14.039 + 86.115 * ndci_eff + 194.325 * (ndci_eff ** 2)
    ).round(3)

    # Приводим имена колонок к единому стилю snake_case
    df["water_body_type_code"] = pd.to_numeric(df["Water_body_type"], errors="coerce").astype("Int64")
    df["water_body_type"] = df["water_body_type_code"].map(WATER_BODY_TYPE_NAMES).fillna("unknown")
    df["is_inland_lake"] = df["water_body_type_code"] == 1

    final_cols = [
        "GLORIA_ID",
        "Country",
        "Site_name",
        "Latitude",
        "Longitude",
        "Date_Time_UTC",
        "water_body_type_code",
        "water_body_type",
        "is_inland_lake",
        "chl_a_ug_l",
        "chl_a_method_type",
        "TSS",
        "Secchi_depth",
        "aCDOM440",
        "b02_blue",
        "b03_green",
        "b04_red",
        "b05_rededge1",
        "b06_rededge2",
        "b08_nir",
        "ndci",
        "ndti",
        "b03_b02_ratio",
        "b04_b02_ratio",
        "b05_b04_ratio",
        "b06_b04_ratio",
        "chl_a_mishra_ug_l",
    ]
    df = df[final_cols].rename(
        columns={
            "GLORIA_ID": "gloria_id",
            "Country": "country",
            "Site_name": "site_name",
            "Latitude": "latitude",
            "Longitude": "longitude",
            "Date_Time_UTC": "sample_datetime_utc",
            "TSS": "tss_mg_l",
            "Secchi_depth": "secchi_depth_m",
            "aCDOM440": "acdom_440",
        }
    )

    INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(GLORIA_PARQUET_PATH, index=False)

    lakes_only = df[df["is_inland_lake"]]
    clean_water_sub = lakes_only[lakes_only["chl_a_ug_l"] <= 15.0]
    mishra_mae_low = float(
        np.mean(np.abs(clean_water_sub["chl_a_mishra_ug_l"] - clean_water_sub["chl_a_ug_l"]))
    ) if len(clean_water_sub) else float("nan")

    print("=" * 96)
    print("МИРОВОЙ ЭТАЛОН GLORIA (Nature Scientific Data 2023) — ПОДГОТОВЛЕН ДЛЯ SENTINEL-2")
    print("=" * 96)
    print(f"• Всего записей в архиве GLORIA:        {total_raw}")
    print(f"• Прошли строгий QC + имеют Chl-a и B02..B05: {len(df)} записей ({df['country'].nunique()} стран)")
    print(f"  - из них внутренние озёра (Type 1):   {len(lakes_only)} записей")
    print(f"  - диапазон Chl-a (озёра):             {lakes_only['chl_a_ug_l'].min():.2f} – {lakes_only['chl_a_ug_l'].max():.2f} мкг/л (медиана {lakes_only['chl_a_ug_l'].median():.2f})")
    print(f"  - ошибка старой формулы Mishra (2012) на чистых озёрах (Chl-a <= 15 мкг/л): MAE = {mishra_mae_low:.2f} мкг/л")
    print(f"• Сохранено в: {GLORIA_PARQUET_PATH}")
    return GLORIA_PARQUET_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Загрузка и преобразование мирового датасета GLORIA в каналы Sentinel-2"
    )
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    build_gloria_sentinel2_dataset(force_refresh=args.force_refresh)


if __name__ == "__main__":
    main()
