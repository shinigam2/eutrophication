"""Единая точка запуска всей подсистемы сбора данных и витрины признаков (Часть 1).

Связывает в один воспроизводимый конвейер все модули Части 1:
  1. `osm_geometry.py`     — контуры озёр OpenStreetMap -> data/reference/lakes.geojson
  2. `meteo_fetcher.py`    — суточная погода NASA POWER -> data/interim/meteo_clean.parquet
  3. `sentinel_fetcher.py` — реальные сцены Sentinel-2 L2A -> data/interim/satellite_scenes.parquet
  4. `lab_loader.py`       — реальные таблицы БГУ -> data/lab/lab_measurements.csv + lab_clean.parquet
  5. `gloria_loader.py`    — мировой эталон GLORIA -> data/interim/gloria_sentinel2_pairs.parquet
  6. `validate_data.py`    — валидация, 10-дневная сетка -> satellite_decadal.parquet + data_quality_report.json
  7. `build_features.py`   — витрины признаков -> features_satellite.parquet, features_lab.parquet, dataset_passport.json

Сценарии запуска:
  Полный пересчёт (быстрый, если сырые кэши уже скачаны):
    python -m src.etl.run_pipeline

  Живое добавление нового озера на защите/опроцентовке (например, резервного `rossokhi`
  после переключения `enabled: true` в configs/water_bodies.yaml):
    python -m src.etl.run_pipeline --lake rossokhi

  Быстрая пересборка только витрин и отчёта качества (без сетевых запросов):
    python -m src.etl.run_pipeline --skip-fetch
"""

from __future__ import annotations

import argparse
import time

from src.etl.build_features import build_all_features
from src.etl.config import load_config
from src.etl.gloria_loader import build_gloria_sentinel2_dataset
from src.etl.lab_loader import validate_and_save_lab_data
from src.etl.meteo_fetcher import fetch_all_meteo
from src.etl.osm_geometry import sync_lakes_geojson
from src.etl.sentinel_fetcher import fetch_all_scenes
from src.etl.validate_data import run_validation

DEFAULT_OPEN_WATER_MONTHS = {4, 5, 6, 7, 8, 9, 10, 11}


def run_etl_pipeline(
    lake_ids: list[str] | None = None,
    skip_fetch: bool = False,
    skip_gloria_lab: bool = False,
    workers: int = 4,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = False,
    to_postgis: bool = False,
) -> None:
    """Выполняет полный или инкрементальный (--lake) цикл Части 1."""
    t_total = time.time()
    cfg = load_config()
    active_lakes = cfg.get_lakes(lake_ids=lake_ids)

    mode_str = (
        f"ИНКРЕМЕНТАЛЬНОЕ ДОБАВЛЕНИЕ ({', '.join(wb.id for wb in active_lakes)})"
        if lake_ids
        else f"ПОЛНЫЙ КОНВЕЙЕР ({len(active_lakes)} активных озёр)"
    )
    print("=" * 100)
    print(f"ЗАПУСК ЧАСТИ 1 (ETL & FEATURE MART) — {mode_str}")
    print("=" * 100)

    if not skip_fetch:
        print("\n[ШАГ 1/7] Синхронизация контуров OpenStreetMap...")
        sync_lakes_geojson(lake_ids=lake_ids, force_refresh=force_refresh)

        print("\n[ШАГ 2/7] Загрузка суточной погоды NASA POWER (2018–2026)...")
        fetch_all_meteo(
            lake_ids=lake_ids,
            start_date=start_date,
            end_date=end_date,
            force_refresh=force_refresh,
        )

        print("\n[ШАГ 3/7] Извлечение реальных снимков Sentinel-2 L2A (IV–XI)...")
        fetch_all_scenes(
            cfg=cfg,
            start_date=start_date or "2018-04-01",
            end_date=end_date or cfg.pipeline.end_date,
            lake_ids=lake_ids,
            months=DEFAULT_OPEN_WATER_MONTHS,
            workers=workers,
            force_refresh=force_refresh,
        )
    else:
        print("\n[ШАГИ 1–3/7] Пропущены (--skip-fetch): используем локальные кэши и Parquet.")

    if not skip_gloria_lab and not lake_ids:
        print("\n[ШАГ 4/7] Проверка лабораторных таблиц БГУ...")
        validate_and_save_lab_data(rebuild_from_pdfs=force_refresh)

        print("\n[ШАГ 5/7] Проверка мирового эталона GLORIA...")
        build_gloria_sentinel2_dataset(force_refresh=force_refresh)
    else:
        print("\n[ШАГИ 4–5/7] Лаборатория БГУ и GLORIA уже подготовлены (пропуск при --lake / --skip-gloria-lab).")

    print("\n[ШАГ 6/7] Валидация качества и построение 10-дневной сетки...")
    run_validation()

    print("\n[ШАГ 7/7] Инженерия признаков и сборка витрин + dataset_passport.json...")
    build_all_features()

    if to_postgis:
        print("\n[ШАГ 8/8] Загрузка контуров и витрин в PostgreSQL + PostGIS...")
        from src.etl.load_to_postgis import load_all_to_postgis

        load_all_to_postgis()

    print("\n" + "=" * 100)
    print(f"ЧАСТЬ 1 УСПЕШНО ЗАВЕРШЕНА ЗА {time.time() - t_total:.1f} С!")
    print("=" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Единый запуск Части 1 (ETL, контроль качества и витрины признаков)"
    )
    parser.add_argument(
        "--lake",
        action="append",
        dest="lakes",
        help="Код озера для инкрементального добавления (например, --lake rossokhi)",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Не обращаться в сеть (пересобрать только валидацию и витрины из локальных данных)",
    )
    parser.add_argument(
        "--skip-gloria-lab",
        action="store_true",
        help="Пропустить пересборку GLORIA и таблиц БГУ",
    )
    parser.add_argument(
        "--to-postgis",
        action="store_true",
        help="После сборки витрин дополнительно загрузить все таблицы и контуры в PostgreSQL + PostGIS",
    )
    parser.add_argument("--workers", type=int, default=4, help="Число процессов для Sentinel-2")
    parser.add_argument("--start", default=None, help="Дата начала ГГГГ-ММ-ДД")
    parser.add_argument("--end", default=None, help="Дата конца ГГГГ-ММ-ДД")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()

    run_etl_pipeline(
        lake_ids=args.lakes,
        skip_fetch=args.skip_fetch,
        skip_gloria_lab=args.skip_gloria_lab,
        workers=args.workers,
        start_date=args.start,
        end_date=args.end,
        force_refresh=args.force_refresh,
        to_postgis=args.to_postgis,
    )


if __name__ == "__main__":
    main()
