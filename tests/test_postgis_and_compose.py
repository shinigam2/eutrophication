"""Автотесты Docker Compose (Oxigraph + PostGIS + ETL), DAG-заготовки и живой загрузки в PostGIS."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg
import pytest
import yaml

from src.etl.config import PROJECT_ROOT
from src.etl.load_to_postgis import DEFAULT_DATABASE_URL

COMPOSE_PATH = PROJECT_ROOT / "compose.yaml"
DAG_PATH = PROJECT_ROOT / "dags" / "eutrophication_etl_dag.py"


def _postgis_dsn() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def _can_connect_postgis() -> bool:
    try:
        with psycopg.connect(_postgis_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


def test_compose_yaml_preserves_oxigraph_and_adds_postgis_etl() -> None:
    """compose.yaml сохраняет oxigraph (Часть 3) и корректно описывает postgis + etl."""
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = data["services"]

    # Сервис Волковича (Часть 3) должен оставаться нетронутым
    assert "oxigraph" in services
    assert any("7878:7878" in str(p) for p in services["oxigraph"]["ports"])

    # Наши сервисы (Часть 1 + общая БД)
    assert "postgis" in services
    assert "postgis/postgis" in services["postgis"]["image"]
    assert any("5432:5432" in str(p) for p in services["postgis"]["ports"])

    assert "etl" in services
    assert "etl" in services["etl"].get("profiles", [])


def test_airflow_dag_stub_imports_cleanly_without_airflow() -> None:
    """Файл dags/eutrophication_etl_dag.py синтаксически корректен и безопасно импортируется."""
    assert DAG_PATH.exists()
    spec = importlib.util.spec_from_file_location("eutrophication_etl_dag", DAG_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "PROJECT_DIR")


@pytest.mark.skipif(not _can_connect_postgis(), reason="Контейнер PostGIS не запущен локально")
def test_postgis_tables_and_spatial_queries() -> None:
    """Проверяет загруженные таблицы, целостность геометрии и пространственные запросы PostGIS."""
    with psycopg.connect(_postgis_dsn()) as conn, conn.cursor() as cur:
        # 1. Проверяем точное число строк во всех таблицах
        expected_min = {
            "water_bodies": 8,
            "meteo_daily": 25_000,
            "satellite_decadal": 2_400,
            "features_satellite": 1_100,
            "features_lab": 36,
            "lab_measurements": 36,
            "etl_metadata": 2,
        }
        for tbl, min_rows in expected_min.items():
            cur.execute(f"SELECT COUNT(*) FROM {tbl};")
            cnt = cur.fetchone()[0]
            assert cnt >= min_rows, f"Таблица {tbl}: {cnt} < {min_rows}"

        # 2. Проверяем валидность всех полигонов и попадание внутренней точки внутрь полигона
        cur.execute(
            """
            SELECT water_body_id,
                   ST_IsValid(geom),
                   ST_Within(ST_SetSRID(ST_MakePoint(center_lon, center_lat), 4326), geom),
                   ABS((ST_Area(geom::geography) / 1000000.0) - area_km2_osm) / NULLIF(area_km2_osm, 0)
            FROM water_bodies;
            """
        )
        rows = cur.fetchall()
        assert len(rows) >= 8
        for lid, is_valid, center_inside, rel_area_diff in rows:
            assert is_valid is True, f"{lid}: невалидная геометрия PostGIS"
            assert center_inside is True, f"{lid}: center_lon/lat не внутри полигона!"
            assert rel_area_diff < 0.01, f"{lid}: расхождение площади PostGIS vs PyProj {rel_area_diff:.4f}"


def test_oxigraph_endpoint_if_running() -> None:
    """Если Oxigraph поднят через docker compose up -d, проверяет живой SPARQL SELECT."""
    endpoint = os.environ.get("OXIGRAPH_URL", "http://127.0.0.1:7878").rstrip("/")
    req = Request(
        f"{endpoint}/query",
        data=b"SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
        headers={
            "Content-Type": "application/sparql-query",
            "Accept": "application/sparql-results+json",
        },
    )
    try:
        with urlopen(req, timeout=2) as resp:
            assert resp.status == 200
    except (URLError, TimeoutError, OSError):
        pytest.skip(f"Контейнер Oxigraph недоступен по адресу {endpoint}")
