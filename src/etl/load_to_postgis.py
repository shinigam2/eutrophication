"""Загрузка готовых витрин и геометрии Части 1 в PostgreSQL + PostGIS.

Идемпотентный загрузчик: безопасно запускается повторно после обновления витрин
или добавления нового озера (`--lake rossokhi`).

Создаёт и наполняет 7 таблиц:
  1. `water_bodies`       — паспорта озёр + точные полигоны GEOMETRY(MultiPolygon, 4326) + GIST-индекс
  2. `meteo_daily`        — суточная погода NASA POWER (25 928+ строк)
  3. `satellite_decadal`  — полная 10-дневная сетка Sentinel-2 (2 488+ строк, со статусами облачности/льда)
  4. `features_satellite` — главная витрина признаков (1 146+ валидных срезов, 54 столбца)
  5. `features_lab`       — лабораторная витрина БГУ (36 проб со спутником и погодой)
  6. `lab_measurements`   — таблица лабораторных проб (предзаполнена БГУ, готова к `POST /lab/upload`)
  7. `etl_metadata`       — JSONB-хранилище паспорта витрин и отчёта качества данных

Запуск:
  python -m src.etl.load_to_postgis
  # или с явной строкой подключения:
  DATABASE_URL=postgresql://eutro:eutro@127.0.0.1:5432/eutrophication python -m src.etl.load_to_postgis
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
from psycopg import sql

from src.etl.config import PROJECT_ROOT

DEFAULT_DATABASE_URL = "postgresql://eutro:eutro@127.0.0.1:5432/eutrophication"

LAKES_GEOJSON_PATH = PROJECT_ROOT / "data" / "reference" / "lakes.geojson"
METEO_PARQUET_PATH = PROJECT_ROOT / "data" / "interim" / "meteo_clean.parquet"
DECADAL_PARQUET_PATH = PROJECT_ROOT / "data" / "interim" / "satellite_decadal.parquet"
QUALITY_REPORT_PATH = PROJECT_ROOT / "data" / "interim" / "data_quality_report.json"
FEATURES_SAT_PATH = PROJECT_ROOT / "data" / "processed" / "features_satellite.parquet"
FEATURES_LAB_PATH = PROJECT_ROOT / "data" / "processed" / "features_lab.parquet"
PASSPORT_PATH = PROJECT_ROOT / "data" / "processed" / "dataset_passport.json"
LAB_CSV_PATH = PROJECT_ROOT / "data" / "lab" / "lab_measurements.csv"


SCHEMA_DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS water_bodies (
    water_body_id               TEXT PRIMARY KEY,
    name_ru                     TEXT NOT NULL,
    name_be                     TEXT,
    lake_group                  TEXT,
    basin                       TEXT,
    osm_type                    TEXT,
    osm_id                      BIGINT,
    enabled                     BOOLEAN NOT NULL DEFAULT TRUE,
    has_lab_data                BOOLEAN NOT NULL DEFAULT FALSE,
    area_km2_ref                DOUBLE PRECISION,
    area_km2_osm                DOUBLE PRECISION,
    area_diff_pct               DOUBLE PRECISION,
    islands_count               INTEGER,
    max_depth_m                 DOUBLE PRECISION,
    mean_depth_m                DOUBLE PRECISION,
    volume_mln_m3               DOUBLE PRECISION,
    watershed_km2               DOUBLE PRECISION,
    watershed_ratio             DOUBLE PRECISION,
    center_lon                  DOUBLE PRECISION,
    center_lat                  DOUBLE PRECISION,
    attention_ug_l              DOUBLE PRECISION,
    bloom_ug_l                  DOUBLE PRECISION,
    protected_area              TEXT,
    chemical_treatment_allowed  BOOLEAN,
    stratification_type         TEXT,
    cascade_inflow_from         TEXT[],
    cascade_outflow_to          TEXT,
    morphometry_source          TEXT,
    notes                       TEXT,
    geom                        GEOMETRY(MultiPolygon, 4326) NOT NULL,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_water_bodies_geom ON water_bodies USING GIST (geom);

CREATE TABLE IF NOT EXISTS meteo_daily (
    water_body_id   TEXT NOT NULL REFERENCES water_bodies(water_body_id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    temp_mean_c     DOUBLE PRECISION,
    temp_max_c      DOUBLE PRECISION,
    temp_min_c      DOUBLE PRECISION,
    wind_speed_ms   DOUBLE PRECISION,
    precip_mm       DOUBLE PRECISION,
    solar_rad_mj    DOUBLE PRECISION,
    humidity_pct    DOUBLE PRECISION,
    PRIMARY KEY (water_body_id, date)
);
CREATE INDEX IF NOT EXISTS idx_meteo_daily_date ON meteo_daily (date);

CREATE TABLE IF NOT EXISTS satellite_decadal (
    water_body_id       TEXT NOT NULL REFERENCES water_bodies(water_body_id) ON DELETE CASCADE,
    date                DATE NOT NULL,
    obs_status          TEXT NOT NULL,
    scenes_in_decade    INTEGER,
    valid_water_pixels  DOUBLE PRECISION,
    water_coverage_pct  DOUBLE PRECISION,
    sun_glint_flag      BOOLEAN,
    last_scene_id       TEXT,
    last_scene_date     DATE,
    b02_blue            DOUBLE PRECISION,
    b03_green           DOUBLE PRECISION,
    b04_red             DOUBLE PRECISION,
    b05_rededge1        DOUBLE PRECISION,
    b06_rededge2        DOUBLE PRECISION,
    b08_nir             DOUBLE PRECISION,
    b11_swir1           DOUBLE PRECISION,
    ndci                DOUBLE PRECISION,
    ndti                DOUBLE PRECISION,
    b03_b02_ratio       DOUBLE PRECISION,
    b04_b02_ratio       DOUBLE PRECISION,
    b05_b04_ratio       DOUBLE PRECISION,
    chl_a_est_ug_l      DOUBLE PRECISION,
    tsi_est             DOUBLE PRECISION,
    PRIMARY KEY (water_body_id, date)
);
CREATE INDEX IF NOT EXISTS idx_satellite_decadal_status ON satellite_decadal (obs_status, date);

CREATE TABLE IF NOT EXISTS features_satellite (
    water_body_id                   TEXT NOT NULL REFERENCES water_bodies(water_body_id) ON DELETE CASCADE,
    water_body_name                 TEXT,
    date                            DATE NOT NULL,
    obs_status                      TEXT NOT NULL,
    valid_water_pixels              DOUBLE PRECISION,
    water_coverage_pct              DOUBLE PRECISION,
    scenes_in_decade                INTEGER,
    sun_glint_flag                  BOOLEAN,
    last_scene_id                   TEXT,
    last_scene_date                 DATE,
    is_trainable_target             BOOLEAN NOT NULL,
    target_date_next_10d            DATE,
    month                           INTEGER,
    day_of_year                     INTEGER,
    sin_doy                         DOUBLE PRECISION,
    cos_doy                         DOUBLE PRECISION,
    area_km2                        DOUBLE PRECISION,
    mean_depth_m                    DOUBLE PRECISION,
    max_depth_m                     DOUBLE PRECISION,
    watershed_ratio                 DOUBLE PRECISION,
    b02_blue                        DOUBLE PRECISION,
    b03_green                       DOUBLE PRECISION,
    b04_red                         DOUBLE PRECISION,
    b05_rededge1                    DOUBLE PRECISION,
    b06_rededge2                    DOUBLE PRECISION,
    b08_nir                         DOUBLE PRECISION,
    b11_swir1                       DOUBLE PRECISION,
    ndci_current                    DOUBLE PRECISION,
    ndci_lag10d                     DOUBLE PRECISION,
    ndci_growth_10d                 DOUBLE PRECISION,
    ndti_current                    DOUBLE PRECISION,
    b03_b02_ratio_current           DOUBLE PRECISION,
    b04_b02_ratio_current           DOUBLE PRECISION,
    b04_b02_ratio_lag10d            DOUBLE PRECISION,
    b04_b02_growth_10d              DOUBLE PRECISION,
    b05_b04_ratio_current           DOUBLE PRECISION,
    chl_a_est_current               DOUBLE PRECISION,
    chl_a_est_lag10d                DOUBLE PRECISION,
    tsi_current                     DOUBLE PRECISION,
    temp_mean_7d                    DOUBLE PRECISION,
    temp_mean_14d                   DOUBLE PRECISION,
    temp_mean_21d                   DOUBLE PRECISION,
    temp_max_7d                     DOUBLE PRECISION,
    hot_days_14d                    DOUBLE PRECISION,
    precip_sum_7d                   DOUBLE PRECISION,
    precip_sum_14d                  DOUBLE PRECISION,
    radiation_mean_7d               DOUBLE PRECISION,
    wind_mean_7d                    DOUBLE PRECISION,
    calm_fraction_7d                DOUBLE PRECISION,
    target_ndci_next_10d            DOUBLE PRECISION,
    target_b04_b02_next_10d         DOUBLE PRECISION,
    target_chl_a_est_next_10d       DOUBLE PRECISION,
    target_attention_risk_next_10d  DOUBLE PRECISION,
    target_bloom_risk_next_10d      DOUBLE PRECISION,
    PRIMARY KEY (water_body_id, date)
);
CREATE INDEX IF NOT EXISTS idx_features_sat_trainable ON features_satellite (is_trainable_target, date);

CREATE TABLE IF NOT EXISTS features_lab (
    water_body_id               TEXT NOT NULL REFERENCES water_bodies(water_body_id) ON DELETE CASCADE,
    date                        DATE NOT NULL,
    chl_a_lab_ug_l              DOUBLE PRECISION,
    chl_a_filter_1_5_um_ug_l    DOUBLE PRECISION,
    secchi_depth_m              DOUBLE PRECISION,
    total_phosphorus_ug_l       DOUBLE PRECISION,
    sample_type                 TEXT,
    date_precision              TEXT,
    source_note                 TEXT,
    matched_s2_scenes_pm10d     INTEGER,
    s2_b02_blue                 DOUBLE PRECISION,
    s2_b03_green                DOUBLE PRECISION,
    s2_b04_red                  DOUBLE PRECISION,
    s2_b05_rededge1             DOUBLE PRECISION,
    s2_b06_rededge2             DOUBLE PRECISION,
    s2_b08_nir                  DOUBLE PRECISION,
    s2_b11_swir1                DOUBLE PRECISION,
    s2_ndci                     DOUBLE PRECISION,
    s2_ndti                     DOUBLE PRECISION,
    s2_b03_b02_ratio            DOUBLE PRECISION,
    s2_b04_b02_ratio            DOUBLE PRECISION,
    s2_b05_b04_ratio            DOUBLE PRECISION,
    s2_chl_a_est_ug_l           DOUBLE PRECISION,
    temp_mean_7d                DOUBLE PRECISION,
    temp_mean_14d               DOUBLE PRECISION,
    temp_mean_21d               DOUBLE PRECISION,
    temp_max_7d                 DOUBLE PRECISION,
    hot_days_14d                DOUBLE PRECISION,
    precip_sum_7d               DOUBLE PRECISION,
    precip_sum_14d              DOUBLE PRECISION,
    radiation_mean_7d           DOUBLE PRECISION,
    wind_mean_7d                DOUBLE PRECISION,
    calm_fraction_7d            DOUBLE PRECISION,
    month                       INTEGER,
    day_of_year                 INTEGER,
    sin_doy                     DOUBLE PRECISION,
    cos_doy                     DOUBLE PRECISION,
    water_body_name             TEXT,
    area_km2                    DOUBLE PRECISION,
    mean_depth_m                DOUBLE PRECISION,
    max_depth_m                 DOUBLE PRECISION,
    watershed_ratio             DOUBLE PRECISION,
    chl_a_lab_prev_month        DOUBLE PRECISION,
    target_chl_a_lab_next_month DOUBLE PRECISION,
    PRIMARY KEY (water_body_id, date)
);

CREATE TABLE IF NOT EXISTS lab_measurements (
    id                          SERIAL PRIMARY KEY,
    water_body_id               TEXT NOT NULL REFERENCES water_bodies(water_body_id) ON DELETE CASCADE,
    sample_date                 DATE NOT NULL,
    chl_a_lab_ug_l              DOUBLE PRECISION NOT NULL,
    chl_a_filter_1_5_um_ug_l    DOUBLE PRECISION,
    secchi_depth_m              DOUBLE PRECISION,
    total_phosphorus_ug_l       DOUBLE PRECISION,
    sample_type                 TEXT NOT NULL DEFAULT 'integral',
    date_precision              TEXT NOT NULL DEFAULT 'day',
    source_note                 TEXT,
    uploaded_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (water_body_id, sample_date, sample_type)
);

CREATE TABLE IF NOT EXISTS etl_metadata (
    key         TEXT PRIMARY KEY,
    payload     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _clean_val(val: Any) -> Any:
    """Преобразует pandas NaN/NaT/пустые строки в None для PostgreSQL."""
    if val is None:
        return None
    if isinstance(val, str):
        stripped = val.strip()
        return None if stripped == "" or stripped.lower() in {"nan", "nat", "none"} else stripped
    if pd.isna(val):
        return None
    if hasattr(val, "item"):
        return val.item()
    return val


def _copy_dataframe(
    cur: psycopg.Cursor[Any],
    table_name: str,
    df: pd.DataFrame,
) -> int:
    """Быстро перезагружает таблицу через PostgreSQL COPY (внутри общей транзакции)."""
    cols = list(df.columns)
    cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(table_name)))
    copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
    )
    rows_written = 0
    with cur.copy(copy_stmt) as copy:
        for row in df.itertuples(index=False, name=None):
            copy.write_row(tuple(_clean_val(v) for v in row))
            rows_written += 1
    return rows_written


def _load_water_bodies(cur: psycopg.Cursor[Any], geojson_path: Path) -> int:
    fc = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = fc.get("features", [])
    upsert_sql = """
    INSERT INTO water_bodies (
        water_body_id, name_ru, name_be, lake_group, basin, osm_type, osm_id,
        enabled, has_lab_data, area_km2_ref, area_km2_osm, area_diff_pct,
        islands_count, max_depth_m, mean_depth_m, volume_mln_m3,
        watershed_km2, watershed_ratio, center_lon, center_lat,
        attention_ug_l, bloom_ug_l, protected_area, chemical_treatment_allowed,
        stratification_type, cascade_inflow_from, cascade_outflow_to,
        morphometry_source, notes, geom, updated_at
    ) VALUES (
        %(id)s, %(name_ru)s, %(name_be)s, %(group)s, %(basin)s, %(osm_type)s, %(osm_id)s,
        %(enabled)s, %(has_lab_data)s, %(area_km2_ref)s, %(area_km2_osm)s, %(area_diff_pct)s,
        %(islands_count)s, %(max_depth_m)s, %(mean_depth_m)s, %(volume_mln_m3)s,
        %(watershed_km2)s, %(watershed_ratio)s, %(center_lon)s, %(center_lat)s,
        %(attention_ug_l)s, %(bloom_ug_l)s, %(protected_area)s, %(chemical_treatment_allowed)s,
        %(stratification_type)s, %(cascade_inflow_from)s, %(cascade_outflow_to)s,
        %(morphometry_source)s, %(notes)s,
        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%(geom_json)s), 4326)),
        NOW()
    )
    ON CONFLICT (water_body_id) DO UPDATE SET
        name_ru = EXCLUDED.name_ru,
        name_be = EXCLUDED.name_be,
        lake_group = EXCLUDED.lake_group,
        basin = EXCLUDED.basin,
        osm_type = EXCLUDED.osm_type,
        osm_id = EXCLUDED.osm_id,
        enabled = EXCLUDED.enabled,
        has_lab_data = EXCLUDED.has_lab_data,
        area_km2_ref = EXCLUDED.area_km2_ref,
        area_km2_osm = EXCLUDED.area_km2_osm,
        area_diff_pct = EXCLUDED.area_diff_pct,
        islands_count = EXCLUDED.islands_count,
        max_depth_m = EXCLUDED.max_depth_m,
        mean_depth_m = EXCLUDED.mean_depth_m,
        volume_mln_m3 = EXCLUDED.volume_mln_m3,
        watershed_km2 = EXCLUDED.watershed_km2,
        watershed_ratio = EXCLUDED.watershed_ratio,
        center_lon = EXCLUDED.center_lon,
        center_lat = EXCLUDED.center_lat,
        attention_ug_l = EXCLUDED.attention_ug_l,
        bloom_ug_l = EXCLUDED.bloom_ug_l,
        protected_area = EXCLUDED.protected_area,
        chemical_treatment_allowed = EXCLUDED.chemical_treatment_allowed,
        stratification_type = EXCLUDED.stratification_type,
        cascade_inflow_from = EXCLUDED.cascade_inflow_from,
        cascade_outflow_to = EXCLUDED.cascade_outflow_to,
        morphometry_source = EXCLUDED.morphometry_source,
        notes = EXCLUDED.notes,
        geom = EXCLUDED.geom,
        updated_at = NOW();
    """
    count = 0
    for feat in features:
        p = dict(feat["properties"])
        p["geom_json"] = json.dumps(feat["geometry"])
        p.setdefault("cascade_inflow_from", [])
        cur.execute(upsert_sql, p)
        count += 1
    return count


def _upsert_lab_measurements(cur: psycopg.Cursor[Any], csv_path: Path) -> int:
    df = pd.read_csv(csv_path)
    upsert_sql = """
    INSERT INTO lab_measurements (
        water_body_id, sample_date, chl_a_lab_ug_l, chl_a_filter_1_5_um_ug_l,
        secchi_depth_m, total_phosphorus_ug_l, sample_type, date_precision, source_note
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (water_body_id, sample_date, sample_type) DO UPDATE SET
        chl_a_lab_ug_l = EXCLUDED.chl_a_lab_ug_l,
        chl_a_filter_1_5_um_ug_l = EXCLUDED.chl_a_filter_1_5_um_ug_l,
        secchi_depth_m = EXCLUDED.secchi_depth_m,
        total_phosphorus_ug_l = EXCLUDED.total_phosphorus_ug_l,
        date_precision = EXCLUDED.date_precision,
        source_note = EXCLUDED.source_note;
    """
    count = 0
    for row in df.itertuples(index=False):
        cur.execute(
            upsert_sql,
            (
                _clean_val(row.water_body_id),
                _clean_val(row.sample_date),
                _clean_val(row.chl_a_lab_ug_l),
                _clean_val(row.chl_a_filter_1_5_um_ug_l),
                _clean_val(row.secchi_depth_m),
                _clean_val(row.total_phosphorus_ug_l),
                _clean_val(row.sample_type) or "integral",
                _clean_val(row.date_precision) or "month_mid",
                _clean_val(row.source_note),
            ),
        )
        count += 1
    return count


def _upsert_json_metadata(cur: psycopg.Cursor[Any], key: str, json_path: Path) -> None:
    if not json_path.exists():
        return
    raw_text = json_path.read_text(encoding="utf-8")
    cur.execute(
        """
        INSERT INTO etl_metadata (key, payload, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW();
        """,
        (key, raw_text),
    )


def load_all_to_postgis(database_url: str | None = None) -> dict[str, int]:
    """Создаёт схему PostGIS и атомарно загружает все готовые слои Части 1."""
    dsn = database_url or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL

    for req_path in (
        LAKES_GEOJSON_PATH,
        METEO_PARQUET_PATH,
        DECADAL_PARQUET_PATH,
        FEATURES_SAT_PATH,
        FEATURES_LAB_PATH,
        LAB_CSV_PATH,
    ):
        if not req_path.exists():
            raise FileNotFoundError(
                f"Не найден файл Части 1: {req_path}. Сначала выполните: python -m src.etl.run_pipeline"
            )

    meteo_df = pd.read_parquet(METEO_PARQUET_PATH)
    decadal_df = pd.read_parquet(DECADAL_PARQUET_PATH)
    feat_sat_df = pd.read_parquet(FEATURES_SAT_PATH)
    feat_lab_df = pd.read_parquet(FEATURES_LAB_PATH)

    counts: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)
            counts["water_bodies"] = _load_water_bodies(cur, LAKES_GEOJSON_PATH)
            counts["meteo_daily"] = _copy_dataframe(cur, "meteo_daily", meteo_df)
            counts["satellite_decadal"] = _copy_dataframe(cur, "satellite_decadal", decadal_df)
            counts["features_satellite"] = _copy_dataframe(cur, "features_satellite", feat_sat_df)
            counts["features_lab"] = _copy_dataframe(cur, "features_lab", feat_lab_df)
            counts["lab_measurements"] = _upsert_lab_measurements(cur, LAB_CSV_PATH)
            _upsert_json_metadata(cur, "dataset_passport", PASSPORT_PATH)
            _upsert_json_metadata(cur, "data_quality_report", QUALITY_REPORT_PATH)

            # Геопространственная самопроверка прямо внутри PostGIS
            cur.execute(
                """
                SELECT water_body_id,
                       ROUND((ST_Area(geom::geography) / 1000000.0)::numeric, 3) AS postgis_km2,
                       ST_IsValid(geom) AS geom_valid
                FROM water_bodies
                ORDER BY postgis_km2 DESC;
                """
            )
            geo_check = cur.fetchall()
        conn.commit()

    print("=" * 90)
    print("ЗАГРУЗКА ЧАСТИ 1 В POSTGRESQL + POSTGIS — ГОТОВО")
    print("=" * 90)
    for tbl, cnt in counts.items():
        print(f"  • {tbl:<20}: {cnt:>6} строк")
    print("\nПроверка геометрии PostGIS (ST_Area на геодезическом сфероиде + ST_IsValid):")
    for lid, area_km2, valid in geo_check:
        print(f"  • {lid:<12}: {float(area_km2):>7.3f} км² | ST_IsValid={valid}")
    print("=" * 90)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Загрузить витрины и контуры озёр Части 1 в PostgreSQL + PostGIS"
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Строка подключения PostgreSQL (по умолчанию env DATABASE_URL или localhost:5432)",
    )
    args = parser.parse_args()
    load_all_to_postgis(database_url=args.database_url)


if __name__ == "__main__":
    main()
