"""Загрузка и очистка реальных суточных метеоданных NASA POWER (2018–2026).

Источник данных: открытый NASA POWER Agroclimatology Daily Point API
(`https://power.larc.nasa.gov/api/temporal/daily/point`, без API-ключей).

Какие 7 показателей скачиваются по координатам центра каждого озера из lakes.geojson:
  - T2M               -> temp_mean_c    (среднесуточная температура воздуха на 2 м, °C)
  - T2M_MAX           -> temp_max_c     (максимальная температура за сутки, °C)
  - T2M_MIN           -> temp_min_c     (минимальная температура за сутки, °C)
  - WS2M              -> wind_speed_ms  (средняя скорость ветра на 2 м, м/с)
  - PRECTOTCORR       -> precip_mm      (суточные осадки, мм/сут)
  - ALLSKY_SFC_SW_DWN -> solar_rad_mj   (суммарная солнечная радиация, МДж/м²·сут)
  - RH2M              -> humidity_pct   (относительная влажность воздуха, %)

Контроль качества:
  - Все служебные коды пропусков NASA POWER (значения <= -900, например -999.0)
    честно переводятся в NaN (ни одного выдуманного числа!).
  - Проверяются физические границы для Беларуси.
  - Сырой JSON кэшируется в data/raw/meteo/, итог сохраняется в data/interim/meteo_clean.parquet.

Поддерживает инкрементальное добавление одного озера:
  python -m src.etl.meteo_fetcher --lake rossokhi
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from src.etl.config import INTERIM_DIR, RAW_DIR, load_config
from src.etl.osm_geometry import load_lakes_geometries

NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
USER_AGENT = "BelarusLakeEutrophicationETL/1.0 (BSUIR/BSU coursework research)"

METEO_RAW_DIR = RAW_DIR / "meteo"
METEO_PARQUET_PATH = INTERIM_DIR / "meteo_clean.parquet"

# Соответствие имён параметров NASA POWER -> колонки DATA_CONTRACT.md
PARAM_MAP: dict[str, str] = {
    "T2M": "temp_mean_c",
    "T2M_MAX": "temp_max_c",
    "T2M_MIN": "temp_min_c",
    "WS2M": "wind_speed_ms",
    "PRECTOTCORR": "precip_mm",
    "ALLSKY_SFC_SW_DWN": "solar_rad_mj",
    "RH2M": "humidity_pct",
}

# Физические границы для контроля сбоев (вне диапазона -> NaN с предупреждением)
PHYSICAL_BOUNDS: dict[str, tuple[float, float]] = {
    "temp_mean_c": (-45.0, 45.0),
    "temp_max_c": (-40.0, 50.0),
    "temp_min_c": (-50.0, 40.0),
    "wind_speed_ms": (0.0, 40.0),
    "precip_mm": (0.0, 300.0),
    "solar_rad_mj": (0.0, 40.0),
    "humidity_pct": (0.0, 100.0),
}


def fetch_nasa_power_json(
    lake_id: str,
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Скачивает (или читает из кэша) суточный JSON NASA POWER для одной точки."""
    METEO_RAW_DIR.mkdir(parents=True, exist_ok=True)
    start_ymd = start_date.replace("-", "")
    end_ymd = end_date.replace("-", "")
    cache_path = METEO_RAW_DIR / f"{lake_id}_{start_ymd}_{end_ymd}.json"

    if cache_path.exists() and not force_refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    params = {
        "parameters": ",".join(PARAM_MAP.keys()),
        "community": "AG",
        "longitude": round(lon, 5),
        "latitude": round(lat, 5),
        "start": start_ymd,
        "end": end_ymd,
        "format": "JSON",
    }

    last_err: Exception | None = None
    for attempt in range(1, 5):
        try:
            resp = requests.get(
                NASA_POWER_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
            if "properties" not in payload or "parameter" not in payload["properties"]:
                raise ValueError(f"Неожиданный ответ NASA POWER: {list(payload.keys())}")
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            time.sleep(0.5)
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            wait_s = attempt * 2.5
            print(
                f"  [METEO] Ошибка для {lake_id} (попытка {attempt}/4): {exc}. "
                f"Повтор через {wait_s:.1f} с..."
            )
            time.sleep(wait_s)

    raise RuntimeError(f"Не удалось скачать погоду NASA POWER для {lake_id}: {last_err}")


def parse_nasa_power_payload(lake_id: str, payload: dict[str, Any]) -> pd.DataFrame:
    """Преобразует JSON NASA POWER в чистую суточную таблицу по DATA_CONTRACT.md."""
    param_data = payload["properties"]["parameter"]
    all_dates = sorted(
        {dt for p in PARAM_MAP if p in param_data for dt in param_data[p].keys()}
    )

    rows: list[dict[str, Any]] = []
    for ymd in all_dates:
        row: dict[str, Any] = {
            "water_body_id": lake_id,
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}",
        }
        for nasa_key, col_name in PARAM_MAP.items():
            raw_val = param_data.get(nasa_key, {}).get(ymd)
            if raw_val is None or float(raw_val) <= -900.0:
                row[col_name] = np.nan
            else:
                val = float(raw_val)
                lo, hi = PHYSICAL_BOUNDS[col_name]
                if lo <= val <= hi:
                    row[col_name] = round(val, 3)
                else:
                    row[col_name] = np.nan
        rows.append(row)

    df = pd.DataFrame(rows)
    return df


def fetch_all_meteo(
    lake_ids: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = False,
) -> Path:
    """Загружает и сохраняет чистую суточную погоду NASA POWER для активных (или указанных) озёр."""
    cfg = load_config()
    start = start_date or cfg.pipeline.start_date
    end = end_date or cfg.pipeline.end_date
    lakes_geoms = load_lakes_geometries(lake_ids=lake_ids, only_enabled=lake_ids is None)
    if not lakes_geoms:
        raise ValueError("Нет озёр для загрузки метеоданных.")

    print(
        f"[METEO] Загрузка суточных данных NASA POWER ({start} .. {end}) "
        f"для {len(lakes_geoms)} озёр..."
    )
    dfs: list[pd.DataFrame] = []
    for lid, (props, _) in lakes_geoms.items():
        lat = float(props["center_lat"])
        lon = float(props["center_lon"])
        payload = fetch_nasa_power_json(
            lake_id=lid,
            lat=lat,
            lon=lon,
            start_date=start,
            end_date=end,
            force_refresh=force_refresh,
        )
        df_lake = parse_nasa_power_payload(lid, payload)
        dfs.append(df_lake)

        nan_counts = {
            c: int(df_lake[c].isna().sum())
            for c in PARAM_MAP.values()
            if df_lake[c].isna().any()
        }
        nan_str = f"пропуски={nan_counts}" if nan_counts else "0 пропусков"
        print(
            f"  • {lid:<12} ({lat:.4f}, {lon:.4f}): {len(df_lake)} сут. "
            f"[{df_lake['date'].min()} .. {df_lake['date'].max()}] | "
            f"Tср={df_lake['temp_mean_c'].mean():.1f}°C | {nan_str}"
        )

    new_df = pd.concat(dfs, ignore_index=True)

    # При инкрементальном добавлении (--lake) объединяем с уже собранным meteo_clean.parquet
    if lake_ids and METEO_PARQUET_PATH.exists():
        old_df = pd.read_parquet(METEO_PARQUET_PATH)
        old_df = old_df[~old_df["water_body_id"].isin(set(lakes_geoms.keys()))]
        final_df = pd.concat([old_df, new_df], ignore_index=True)
    else:
        final_df = new_df

    final_df = (
        final_df.drop_duplicates(subset=["water_body_id", "date"])
        .sort_values(["water_body_id", "date"])
        .reset_index(drop=True)
    )

    INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    final_df.to_parquet(METEO_PARQUET_PATH, index=False)
    print(
        f"[METEO] Сохранено {len(final_df)} суточных записей ({final_df['water_body_id'].nunique()} озёр) "
        f"в {METEO_PARQUET_PATH}"
    )
    return METEO_PARQUET_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Скачивание и очистка реальных суточных метеоданных NASA POWER"
    )
    parser.add_argument(
        "--lake",
        action="append",
        dest="lakes",
        help="Код конкретного озера (можно указать несколько раз)",
    )
    parser.add_argument("--start", default=None, help="Дата начала ГГГГ-ММ-ДД")
    parser.add_argument("--end", default=None, help="Дата конца ГГГГ-ММ-ДД")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    fetch_all_meteo(
        lake_ids=args.lakes,
        start_date=args.start,
        end_date=args.end,
        force_refresh=args.force_refresh,
    )


if __name__ == "__main__":
    main()
