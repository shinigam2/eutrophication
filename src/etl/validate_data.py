"""Валидация качества данных, агрегация на 10-дневную декадную сетку и генерация отчёта качества.

Что делает этот модуль (Этап 6а Части 1):
1. Проверяет все 4 промежуточных источника:
   - `satellite_scenes.parquet` (реальные пролёты Sentinel-2 L2A 2018–2026)
   - `meteo_clean.parquet`      (реальная суточная погода NASA POWER 2018–2026)
   - `lab_clean.parquet`        (реальные пробы БГУ 2018–2019)
   - `gloria_sentinel2_pairs.parquet` (мировой эталон GLORIA)
2. Отсекает экстремальную дымку/блик (`b11_swir1 > 0,06`), если маска SCL пропустила тонкие облака.
3. Агрегирует неравномерные пролёты Sentinel-2 на ровную **10-дневную сетку**
   (1-е, 11-е, 21-е число каждого месяца).
   **Критически важно (защита от утечки будущего!):**
   Декада с датой `D` (например, `2026-07-11`) включает ТОЛЬКО снимки за прошедшее
   10-дневное окно `(D_prev, D]` (т.е. `2026-07-02 .. 2026-07-11`, СТРОГО `<= D`!).
   При наличии в декаде и чистых, и бликовых (`sun_glint_flag`) сцен в медиану идут только чистые.
4. Выставляет честный статус каждой декады (`obs_status`):
   - `valid`              — есть хотя бы 1 реальный чистый снимок в окне `(D_prev, D]`;
   - `interpolated_1step` — одиночный облачный пропуск между двумя `valid` декадами одного сезона
                            (восстанавливается для непрерывности графиков; в витрине ML как таргет
                            или источник утечки будущего НЕ используется!);
   - `cloudy_missing`     — 2+ пасмурные декады подряд (`NaN`);
   - `ice_cover`          — зимние месяцы (декабрь–март, `NaN`).
5. Проводит контрольную сверку 2018–2019 (Sentinel-2 vs реальные пробы БГУ) и сохраняет:
   - `data/interim/satellite_decadal.parquet`
   - `data/interim/data_quality_report.json`

Запуск:
  python -m src.etl.validate_data
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

from src.etl.config import INTERIM_DIR, load_config
from src.etl.gloria_loader import GLORIA_PARQUET_PATH
from src.etl.lab_loader import LAB_CLEAN_PARQUET_PATH
from src.etl.meteo_fetcher import METEO_PARQUET_PATH
from src.etl.sentinel_fetcher import SCENES_PARQUET_PATH

DECADAL_PARQUET_PATH = INTERIM_DIR / "satellite_decadal.parquet"
QUALITY_REPORT_PATH = INTERIM_DIR / "data_quality_report.json"

# Порог жёсткой отбраковки тонкой дымки/облаков по каналу B11 (1610 нм), пропущенных SCL
MAX_B11_HAZE_THRESHOLD = 0.06
ICE_MONTHS = {12, 1, 2, 3}

OPTICAL_MEDIAN_COLS = [
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
    "tsi_est",
]


def generate_decadal_dates(start_date: str, end_date: str) -> list[pd.Timestamp]:
    """Генерирует регулярную сетку декадных дат (1, 11, 21 число каждого месяца)."""
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    dates: list[pd.Timestamp] = []
    cur = pd.Timestamp(year=start_ts.year, month=start_ts.month, day=1)
    while cur <= end_ts:
        for day in (1, 11, 21):
            d = pd.Timestamp(year=cur.year, month=cur.month, day=day)
            if start_ts <= d <= end_ts:
                dates.append(d)
        cur = cur + pd.DateOffset(months=1)
    return dates


def _compute_mishra(ndci: float) -> float:
    return float(max(0.5, 14.039 + 86.115 * ndci + 194.325 * (ndci ** 2)))


def _compute_tsi(chl_a: float) -> float:
    return float(9.81 * math.log(max(chl_a, 0.01)) + 30.6)


def aggregate_to_decadal_grid(
    scenes_df: pd.DataFrame,
    active_lake_ids: list[str],
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Агрегирует реальные сцены Sentinel-2 на строгую 10-дневную сетку без утечки будущего."""
    raw_count = len(scenes_df)
    df = scenes_df.copy()
    df = df[df["water_body_id"].isin(active_lake_ids)].copy()

    # Проверка физических границ и отсечение экстремальной дымки/облаков по B11 > 0.06
    haze_mask = df["b11_swir1"].notna() & (df["b11_swir1"] > MAX_B11_HAZE_THRESHOLD)
    haze_rejected = int(haze_mask.sum())
    bounds_mask = (
        df["ndci"].between(-1.0, 1.0)
        & df["ndti"].between(-1.0, 1.0)
        & (df["valid_water_pixels"] >= 20)
        & ~haze_mask
    )
    bounds_rejected = int((~bounds_mask & ~haze_mask).sum())
    df = df[bounds_mask].copy()

    # Дедупликация в пределах одной даты и озера (берём сцену с наибольшим числом пикселей и меньшим B11)
    df["b11_sort"] = df["b11_swir1"].fillna(1.0)
    df = (
        df.sort_values(
            ["water_body_id", "scene_date", "valid_water_pixels", "b11_sort"],
            ascending=[True, True, False, True],
        )
        .drop_duplicates(subset=["water_body_id", "scene_date"], keep="first")
        .drop(columns=["b11_sort"])
    )
    df["scene_dt"] = pd.to_datetime(df["scene_date"])

    grid_dates = generate_decadal_dates(start_date, end_date)
    all_rows: list[dict[str, Any]] = []

    for lid in active_lake_ids:
        sub_lake = df[df["water_body_id"] == lid].sort_values("scene_dt")
        lake_rows: list[dict[str, Any]] = []

        for idx, d_ts in enumerate(grid_dates):
            prev_ts = (
                grid_dates[idx - 1] if idx > 0 else d_ts - pd.Timedelta(days=10)
            )
            date_str = d_ts.strftime("%Y-%m-%d")

            if d_ts.month in ICE_MONTHS:
                row: dict[str, Any] = {
                    "water_body_id": lid,
                    "date": date_str,
                    "obs_status": "ice_cover",
                    "scenes_in_decade": 0,
                    "valid_water_pixels": np.nan,
                    "water_coverage_pct": np.nan,
                    "sun_glint_flag": False,
                    "last_scene_id": None,
                    "last_scene_date": None,
                }
                for c in OPTICAL_MEDIAN_COLS:
                    row[c] = np.nan
                lake_rows.append(row)
                continue

            # Строго прошедшее окно (prev_ts, d_ts] — НИ ОДНОГО снимка из будущего!
            win_df = sub_lake[
                (sub_lake["scene_dt"] > prev_ts) & (sub_lake["scene_dt"] <= d_ts)
            ]

            if len(win_df) == 0:
                row = {
                    "water_body_id": lid,
                    "date": date_str,
                    "obs_status": "cloudy_missing",
                    "scenes_in_decade": 0,
                    "valid_water_pixels": np.nan,
                    "water_coverage_pct": np.nan,
                    "sun_glint_flag": False,
                    "last_scene_id": None,
                    "last_scene_date": None,
                }
                for c in OPTICAL_MEDIAN_COLS:
                    row[c] = np.nan
                lake_rows.append(row)
                continue

            # Если в декаде есть и чистые, и бликовые (sun_glint_flag) снимки — берём только чистые!
            non_glint = win_df[~win_df["sun_glint_flag"]]
            used_df = non_glint if len(non_glint) > 0 else win_df
            last_row = used_df.iloc[-1]

            row = {
                "water_body_id": lid,
                "date": date_str,
                "obs_status": "valid",
                "scenes_in_decade": int(len(used_df)),
                "valid_water_pixels": float(round(used_df["valid_water_pixels"].median(), 1)),
                "water_coverage_pct": float(round(used_df["water_coverage_pct"].median(), 2)),
                "sun_glint_flag": bool(used_df["sun_glint_flag"].all()),
                "last_scene_id": str(last_row["scene_id"]),
                "last_scene_date": str(last_row["scene_date"]),
            }
            for c in OPTICAL_MEDIAN_COLS:
                val = used_df[c].median()
                row[c] = float(round(val, 6)) if pd.notna(val) else np.nan
            lake_rows.append(row)

        # Одношаговая интерполяция ТОЛЬКО одиночного облачного пропуска между двумя valid декадами одного года
        for i in range(1, len(lake_rows) - 1):
            cur = lake_rows[i]
            prev_r = lake_rows[i - 1]
            next_r = lake_rows[i + 1]
            if (
                cur["obs_status"] == "cloudy_missing"
                and prev_r["obs_status"] == "valid"
                and next_r["obs_status"] == "valid"
                and cur["date"][:4] == prev_r["date"][:4] == next_r["date"][:4]
            ):
                cur["obs_status"] = "interpolated_1step"
                for c in OPTICAL_MEDIAN_COLS:
                    if pd.notna(prev_r[c]) and pd.notna(next_r[c]):
                        cur[c] = round(0.5 * (float(prev_r[c]) + float(next_r[c])), 6)
                # Пересчитываем chl_a_est и tsi_est согласованно из интерполированного NDCI
                if pd.notna(cur["ndci"]):
                    chl = _compute_mishra(float(cur["ndci"]))
                    cur["chl_a_est_ug_l"] = round(chl, 3)
                    cur["tsi_est"] = round(_compute_tsi(chl), 2)

        all_rows.extend(lake_rows)

    dec_df = pd.DataFrame(all_rows)
    qc_stats = {
        "raw_scenes_input": raw_count,
        "haze_b11_rejected": haze_rejected,
        "bounds_rejected": bounds_rejected,
        "clean_unique_day_scenes": int(len(df)),
        "glint_flagged_kept": int(df["sun_glint_flag"].sum()),
        "max_b11_haze_threshold": MAX_B11_HAZE_THRESHOLD,
    }
    return dec_df, qc_stats


def evaluate_bsu_2018_2019_matchup(
    scenes_df: pd.DataFrame, lab_df: pd.DataFrame
) -> dict[str, Any]:
    """Честная контрольная сверка 2018–2019 гг.: реальные сцены Sentinel-2 vs пробы БГУ (±10 дн)."""
    s2 = scenes_df[~scenes_df["sun_glint_flag"]].copy()
    s2 = s2[s2["b11_swir1"].isna() | (s2["b11_swir1"] <= MAX_B11_HAZE_THRESHOLD)]
    s2["dt"] = pd.to_datetime(s2["scene_date"])

    lab = lab_df.copy()
    lab["dt"] = pd.to_datetime(lab["sample_date"])

    matched: list[dict[str, Any]] = []
    for _, r in lab.iterrows():
        sub = s2[
            (s2["water_body_id"] == r["water_body_id"])
            & ((s2["dt"] - r["dt"]).abs() <= pd.Timedelta(days=10))
        ]
        if len(sub) > 0:
            matched.append(
                {
                    "lake": r["water_body_id"],
                    "chl_lab": float(r["chl_a_lab_ug_l"]),
                    "secchi": float(r["secchi_depth_m"]),
                    "b04_b02": float(sub["b04_b02_ratio"].median()),
                    "b03_b02": float(sub["b03_b02_ratio"].median()),
                    "ndti": float(sub["ndti"].median()),
                    "ndci": float(sub["ndci"].median()),
                    "chl_mishra": float(sub["chl_a_est_ug_l"].median()),
                }
            )
    if not matched:
        return {"matched_samples": 0, "total_lab_samples": len(lab)}

    mdf = pd.DataFrame(matched)
    return {
        "matched_samples": int(len(mdf)),
        "total_lab_samples": int(len(lab)),
        "match_rate_pct": round(100.0 * len(mdf) / len(lab), 1),
        "spearman_b04_b02_vs_lab_chl_a": round(
            float(mdf["chl_lab"].rank().corr(mdf["b04_b02"].rank())), 3
        ),
        "spearman_b03_b02_vs_lab_chl_a": round(
            float(mdf["chl_lab"].rank().corr(mdf["b03_b02"].rank())), 3
        ),
        "spearman_b04_b02_vs_secchi": round(
            float(mdf["secchi"].rank().corr(mdf["b04_b02"].rank())), 3
        ),
        "spearman_ndci_mishra_vs_lab_chl_a": round(
            float(mdf["chl_lab"].rank().corr(mdf["chl_mishra"].rank())), 3
        ),
        "mishra_mae_ug_l": round(
            float(np.mean(np.abs(mdf["chl_mishra"] - mdf["chl_lab"]))), 2
        ),
    }


def run_validation() -> tuple[Path, Path]:
    """Выполняет полную валидацию, строит satellite_decadal.parquet и data_quality_report.json."""
    cfg = load_config()
    active_lakes = cfg.get_lakes()
    active_ids = [wb.id for wb in active_lakes]

    scenes_df = pd.read_parquet(SCENES_PARQUET_PATH)
    meteo_df = pd.read_parquet(METEO_PARQUET_PATH)
    lab_df = pd.read_parquet(LAB_CLEAN_PARQUET_PATH)
    gloria_df = pd.read_parquet(GLORIA_PARQUET_PATH)

    dec_df, sat_qc = aggregate_to_decadal_grid(
        scenes_df=scenes_df,
        active_lake_ids=active_ids,
        start_date="2018-04-01",
        end_date=cfg.pipeline.end_date,
    )

    INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    dec_df.to_parquet(DECADAL_PARQUET_PATH, index=False)

    # Посчитаем статистику покрытия по каждому озеру (отдельно для безледового сезона IV–XI)
    dec_df["month"] = pd.to_datetime(dec_df["date"]).dt.month
    open_water = dec_df[~dec_df["month"].isin(ICE_MONTHS)]
    per_lake_report: dict[str, Any] = {}
    for lid in active_ids:
        sub_all = dec_df[dec_df["water_body_id"] == lid]
        sub_ow = open_water[open_water["water_body_id"] == lid]
        sc_sub = scenes_df[scenes_df["water_body_id"] == lid]
        counts_ow = sub_ow["obs_status"].value_counts().to_dict()
        n_ow = max(1, len(sub_ow))
        per_lake_report[lid] = {
            "total_valid_scenes": int(len(sc_sub)),
            "median_valid_water_pixels": int(round(sc_sub["valid_water_pixels"].median())) if len(sc_sub) else 0,
            "open_water_decades_total": int(len(sub_ow)),
            "open_water_valid_decades": int(counts_ow.get("valid", 0)),
            "open_water_valid_pct": round(100.0 * counts_ow.get("valid", 0) / n_ow, 1),
            "open_water_interpolated_1step": int(counts_ow.get("interpolated_1step", 0)),
            "open_water_cloudy_missing": int(counts_ow.get("cloudy_missing", 0)),
            "ice_cover_decades": int((sub_all["obs_status"] == "ice_cover").sum()),
        }

    bsu_matchup = evaluate_bsu_2018_2019_matchup(scenes_df, lab_df)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_range": {"start": "2018-04-01", "end": cfg.pipeline.end_date},
        "lakes_roster": {
            "active_count": len(active_ids),
            "active_ids": active_ids,
            "reserve_ids": [
                wb.id for wb in cfg.water_bodies if not wb.enabled and "Резерв" in (wb.exclusion_reason or "")
            ],
            "excluded_lakes": [
                {"id": wb.id, "area_km2": wb.area_km2, "reason": wb.exclusion_reason}
                for wb in cfg.water_bodies
                if not wb.enabled and "Резерв" not in (wb.exclusion_reason or "")
            ],
        },
        "meteo_quality": {
            "total_daily_rows": int(len(meteo_df)),
            "lakes_count": int(meteo_df["water_body_id"].nunique()),
            "date_min": str(meteo_df["date"].min()),
            "date_max": str(meteo_df["date"].max()),
            "missing_values_total": int(
                meteo_df[
                    [
                        "temp_mean_c",
                        "temp_max_c",
                        "temp_min_c",
                        "wind_speed_ms",
                        "precip_mm",
                        "solar_rad_mj",
                        "humidity_pct",
                    ]
                ].isna().sum().sum()
            ),
        },
        "satellite_quality": sat_qc,
        "decadal_coverage_by_lake": per_lake_report,
        "lab_and_gloria_summary": {
            "bsu_2018_2019_samples": int(len(lab_df)),
            "gloria_total_clean_pairs": int(len(gloria_df)),
            "gloria_inland_lakes_pairs": int(gloria_df["is_inland_lake"].sum()),
            "bsu_2018_2019_sentinel2_matchup": bsu_matchup,
        },
    }

    QUALITY_REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("=" * 102)
    print("ВАЛИДАЦИЯ КАЧЕСТВА И ДЕКАДНАЯ АГРЕГАЦИЯ (2018–2026) — ГОТОВО")
    print("=" * 102)
    print(
        f"• Сцены Sentinel-2: вход={sat_qc['raw_scenes_input']}, "
        f"отброшено дымки B11>{MAX_B11_HAZE_THRESHOLD}={sat_qc['haze_b11_rejected']}, "
        f"чистых уник. дней={sat_qc['clean_unique_day_scenes']}"
    )
    print(
        f"• Сверка БГУ 2018–2019 ↔ Sentinel-2 (±10д): {bsu_matchup['matched_samples']}/{bsu_matchup['total_lab_samples']} "
        f"| Spearman B04/B02 vs Lab Chl-a = {bsu_matchup.get('spearman_b04_b02_vs_lab_chl_a'):+.3f} "
        f"| Mishra (NDCI) = {bsu_matchup.get('spearman_ndci_mishra_vs_lab_chl_a'):+.3f}"
    )
    print(
        f"\n{'Озеро':<12} {'Сцен':>5} {'Декад IV-XI':>11} {'Valid':>7} {'Valid%':>7} "
        f"{'Interp1':>8} {'Cloudy':>7} {'Ice':>5}"
    )
    print("-" * 72)
    for lid, st in per_lake_report.items():
        print(
            f"{lid:<12} {st['total_valid_scenes']:>5d} {st['open_water_decades_total']:>11d} "
            f"{st['open_water_valid_decades']:>7d} {st['open_water_valid_pct']:>6.1f}% "
            f"{st['open_water_interpolated_1step']:>8d} {st['open_water_cloudy_missing']:>7d} "
            f"{st['ice_cover_decades']:>5d}"
        )
    print(f"\n• Сохранено: {DECADAL_PARQUET_PATH} ({len(dec_df)} декад)")
    print(f"• Отчёт качества: {QUALITY_REPORT_PATH}")
    return DECADAL_PARQUET_PATH, QUALITY_REPORT_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Валидация данных и построение 10-дневной декадной сетки Sentinel-2"
    )
    parser.parse_args()
    run_validation()


if __name__ == "__main__":
    main()
