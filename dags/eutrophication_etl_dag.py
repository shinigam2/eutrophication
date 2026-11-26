"""Заготовка DAG для Apache Airflow (архитектурная совместимость с Разделом 5 ТЗ).

В основном Docker Compose мы намеренно НЕ поднимаем 5-контейнерный кластер Airflow
(он требует 3–4 ГБ RAM ради 1 запуска раз в 10 дней), но наш модульный ETL-конвейер
Части 1 (`src/etl/`) уже полностью готов к оркестрации по расписанию:
1-го, 11-го и 21-го числа каждого месяца (апрель–ноябрь) в 03:00 UTC.

Как подключить в продакшене:
  Скопируйте этот файл в `$AIRFLOW_HOME/dags/` и укажите `PROJECT_DIR` к репозиторию.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]

try:
    from airflow import DAG
    from airflow.operators.bash import BashOperator
except ImportError:
    # Позволяет импортировать/проверять файл без установленного apache-airflow локально
    DAG = None  # type: ignore[assignment]
    BashOperator = None  # type: ignore[assignment]


if DAG is not None and BashOperator is not None:
    default_args = {
        "owner": "eutrophication-team",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    }

    with DAG(
        dag_id="belarus_eutrophication_decadal_etl",
        description="Регулярный 10-дневный сбор Sentinel-2 + NASA POWER + сборка витрин + PostGIS + Oxigraph",
        default_args=default_args,
        start_date=datetime(2026, 4, 1),
        schedule="0 3 1,11,21 4-11 *",
        catchup=False,
        tags=["eutrophication", "etl", "sentinel2", "postgis", "oxigraph"],
    ) as dag:
        sync_osm = BashOperator(
            task_id="sync_osm_geometries",
            bash_command=f"cd {PROJECT_DIR} && python -m src.etl.osm_geometry",
        )

        fetch_meteo = BashOperator(
            task_id="fetch_nasa_power_meteo",
            bash_command=f"cd {PROJECT_DIR} && python -m src.etl.meteo_fetcher",
        )

        fetch_sentinel = BashOperator(
            task_id="fetch_sentinel2_l2a",
            bash_command=f"cd {PROJECT_DIR} && python -m src.etl.sentinel_fetcher --months 4,5,6,7,8,9,10,11",
        )

        validate_and_features = BashOperator(
            task_id="validate_and_build_feature_marts",
            bash_command=f"cd {PROJECT_DIR} && python -m src.etl.run_pipeline --skip-fetch --skip-gloria-lab",
        )

        load_postgis = BashOperator(
            task_id="load_marts_to_postgis",
            bash_command=f"cd {PROJECT_DIR} && python -m src.etl.load_to_postgis",
        )

        sync_oxigraph = BashOperator(
            task_id="sync_lakes_to_oxigraph",
            bash_command=(
                f"cd {PROJECT_DIR} && "
                "python src/knowledge_graph/graph_cli.py sync-lakes "
                "--endpoint ${OXIGRAPH_URL:-http://oxigraph:7878}"
            ),
        )

        sync_osm >> [fetch_meteo, fetch_sentinel] >> validate_and_features >> [load_postgis, sync_oxigraph]
