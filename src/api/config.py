"""Где искать файлы других частей. Всё можно переопределить переменными окружения.
Кладёте файлы в любое из мест ниже (или задаёте путь переменной) — API найдёт сам.

Часть 1 по DATA_CONTRACT.md отдаёт две витрины:
  data/processed/features_satellite.parquet  — спутниковый режим
  data/processed/features_lab.parquet        — лабораторный режим
Старое имя features_dataset.* поддерживается как запасной вариант.
"""
import os
from datetime import date
from pathlib import Path

TEST_START = date(2025, 1, 1)          # честный Out-of-Time: Часть 2 учит 2018–2024, тест 2025–2026
DEFAULT_DATE = date.fromisoformat(os.environ.get("WATER_DEFAULT_DATE", "2026-08-01"))

MODES = ("satellite", "lab")

REGISTRY_CANDIDATES = ["configs/water_bodies.yaml", "water_bodies.yaml", "config/water_bodies.yaml"]


def _feature_candidates(stem: str):
    return [f"{d}{stem}.{e}" for d in ("data/processed/", "data/", "", "data/features/") for e in ("parquet", "csv")]


SATELLITE_CANDIDATES = (_feature_candidates("features_satellite")
                        + _feature_candidates("features_dataset"))      # запасное старое имя
LAB_CANDIDATES = (_feature_candidates("features_lab")
                  + _feature_candidates("features_dataset_lab"))

MODEL_DIRS = ["models/", "artifacts/", "src/models/saved_models/", "src/models/", "data/models/", ""]
MODEL_FILES = {
    "satellite": ("lgb_regressor_chla.joblib", "lgb_classifier_bloom.joblib"),
    "lab": ("lgb_regressor_chla_lab.joblib", "lgb_classifier_bloom_lab.joblib"),
}
TWO_STAGE_MODEL_FILES = {
    "satellite": ("regressor.joblib", "forecaster.joblib"),
}
MODEL_ENV = {
    "satellite": ("WATER_REG_MODEL", "WATER_CLF_MODEL", "WATER_MODELS_DIR"),
    "lab": ("WATER_REG_MODEL_LAB", "WATER_CLF_MODEL_LAB", "WATER_MODELS_LAB_DIR"),
}
ATTENTION_FILES = {
    "satellite": "lgb_classifier_attention.joblib",
    "lab": "lgb_classifier_attention_lab.joblib",
}
# для обратной совместимости со старыми скриптами
REG_MODEL, CLF_MODEL = MODEL_FILES["satellite"]

METRICS_CANDIDATES = [f"{d}model_metrics.json"
                      for d in ("data/processed/", "models/", "artifacts/", "reports/", "data/", "")]
QUALITY_CANDIDATES = ["data/interim/data_quality_report.json", "data/processed/data_quality_report.json",
                      "data/data_quality_report.json", "data_quality_report.json"]
GEOMETRY_CANDIDATES = ["data/reference/lakes.geojson", "lakes.geojson", "data/lakes.geojson"]
DECADAL_CANDIDATES = [
    "data/interim/satellite_decadal.parquet",
    "data/interim/satellite_decadal.csv",
    "data/processed/satellite_decadal.parquet",
]
ONTOLOGY_CANDIDATES = [
    "data/knowledge_graph/graph.ttl",
    "src/knowledge_graph/ontology.ttl",
    "ontology.ttl",
    "data/ontology.ttl",
]
LAB_CSV_CANDIDATES = ["data/lab/lab_measurements.csv", "lab_measurements.csv", "data/lab_measurements.csv"]
ENGINE_MODULE = os.environ.get("WATER_ENGINE_MODULE", "src.knowledge_graph.graph_engine")
GRAPH_CLI_MODULE = "src.knowledge_graph.graph_cli"


def oxigraph_url():
    """Необязательный URL запущенного Oxigraph (OXIGRAPH_URL или WATER_OXIGRAPH_URL)."""
    url = os.environ.get("OXIGRAPH_URL") or os.environ.get("WATER_OXIGRAPH_URL")
    return url.strip() if url and url.strip() else None


def root() -> Path:
    return Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[2]))


def _find(env: str, candidates):
    if os.environ.get(env):
        return Path(os.environ[env])
    for c in candidates:
        p = root() / c
        if p.is_file():
            return p
    return None


def find_registry():  return _find("WATER_REGISTRY", REGISTRY_CANDIDATES)


def find_features(mode: str = "satellite"):
    """Витрина признаков нужного режима. Старое имя features_dataset.* — запасной вариант."""
    env = {"satellite": "WATER_FEATURES", "lab": "WATER_FEATURES_LAB"}[mode]
    return _find(env, SATELLITE_CANDIDATES if mode == "satellite" else LAB_CANDIDATES)


def find_metrics():   return _find("WATER_METRICS", METRICS_CANDIDATES)
def find_quality():   return _find("WATER_QUALITY", QUALITY_CANDIDATES)
def find_ontology():  return _find("WATER_ONTOLOGY", ONTOLOGY_CANDIDATES)
def find_geometry():  return _find("WATER_GEOMETRY", GEOMETRY_CANDIDATES)
def find_decadal():   return _find("WATER_DECADAL", DECADAL_CANDIDATES)


def find_lab_csv(for_write: bool = False):
    """Таблица лабораторных проб. Для записи — существующий файл или путь по умолчанию."""
    p = _find("WATER_LAB_CSV", LAB_CSV_CANDIDATES)
    if p or not for_write:
        return p
    return root() / LAB_CSV_CANDIDATES[0]


def part1_fingerprint():
    """(путь, время изменения) реестра, витрин, полигонов, декад и отчёта качества:
    по ним определяем, что добавлено озеро или обновились данные."""
    paths = (find_registry(), find_features("satellite"), find_features("lab"),
             find_geometry(), find_decadal(), find_quality())
    return tuple((str(p), p.stat().st_mtime_ns) if p else None for p in paths)


def find_models(mode: str = "satellite"):
    """Возвращает (путь регрессора, путь классификатора) или None, если нет обеих."""
    env_reg, env_clf, env_dir = MODEL_ENV[mode]
    reg_name = os.environ.get(env_reg) or MODEL_FILES[mode][0]
    clf_name = os.environ.get(env_clf) or MODEL_FILES[mode][1]
    base = os.environ.get(env_dir)
    if base and os.environ.get(env_reg):
        r = Path(reg_name)
        c = Path(clf_name)
        return (r, c) if r.is_file() and c.is_file() else None
    dirs = [Path(base)] if base else [root() / d for d in MODEL_DIRS]
    for d in dirs:
        r, c = d / reg_name, d / clf_name
        if r.is_file() and c.is_file():
            return r, c
    return None


def find_two_stage_models(mode: str = "satellite"):
    """Возвращает (regressor.joblib, forecaster.joblib) двухэтапной Части 2 или None."""
    pair = TWO_STAGE_MODEL_FILES.get(mode)
    if not pair:
        return None
    env_reg, env_clf, env_dir = MODEL_ENV[mode]
    if os.environ.get(env_reg) or os.environ.get(env_clf):
        return None
    base = os.environ.get(env_dir)
    dirs = [Path(base)] if base else [root() / d for d in MODEL_DIRS]
    for d in dirs:
        r, f = d / pair[0], d / pair[1]
        if r.is_file() and f.is_file():
            return r, f
    return None


def find_attention_model(mode: str = "satellite"):
    """Необязательный третий классификатор — вероятность уровня внимания (>=15 мкг/л)."""
    base = os.environ.get("WATER_ATTENTION_MODEL" if mode == "satellite" else "WATER_ATTENTION_MODEL_LAB")
    if base:
        return Path(base) if Path(base).is_file() else None
    dirs = [Path(os.environ[MODEL_ENV[mode][2]])] if os.environ.get(MODEL_ENV[mode][2]) \
        else [root() / d for d in MODEL_DIRS]
    for d in dirs:
        p = d / ATTENTION_FILES[mode]
        if p.is_file():
            return p
    return None


def strict() -> bool:
    """WATER_STRICT=1: не подставлять заглушки, а падать, если файла нет."""
    return os.environ.get("WATER_STRICT") == "1"
