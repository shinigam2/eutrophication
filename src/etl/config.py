"""Загрузка и проверка единого справочника водоёмов (configs/water_bodies.yaml).

Этот модуль используется всеми частями ETL (и может импортироваться Частями 2–4),
чтобы имена файлов, пути папок и параметры озёр никогда не дублировались в коде.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Корневые пути проекта вычисляются относительно расположения этого файла
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "water_bodies.yaml"

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"
REFERENCE_DIR = DATA_DIR / "reference"
LAB_DIR = DATA_DIR / "lab"


@dataclass(frozen=True)
class Thresholds:
    """Санитарные пороги тревоги по концентрации хлорофилла-а (мкг/л)."""

    attention_ug_l: float = 15.0
    bloom_ug_l: float = 25.0


@dataclass(frozen=True)
class PipelineConfig:
    """Общие параметры конвейера сбора данных."""

    start_date: str
    end_date: str
    stac_api_url: str
    stac_collection: str
    max_scene_cloud_cover_pct: float
    shore_buffer_m: float
    min_valid_water_pixels: int
    min_water_coverage_pct: float
    default_thresholds: Thresholds


@dataclass(frozen=True)
class EcologicalContext:
    """Экологический и природоохранный контекст озера для Графа знаний (Часть 3)."""

    protected_area: str = ""
    chemical_treatment_allowed: bool = False
    stratification_type: str = "unknown"
    cascade_inflow_from: tuple[str, ...] = ()
    cascade_outflow_to: str = ""


@dataclass(frozen=True)
class WaterBodyConfig:
    """Паспорт одного водоёма из configs/water_bodies.yaml."""

    id: str
    name_ru: str
    name_be: str
    group: str
    basin: str
    osm_type: str
    osm_id: int
    area_km2: float
    max_depth_m: float
    mean_depth_m: float
    volume_mln_m3: float
    watershed_km2: float
    has_lab_data: bool = False
    enabled: bool = True
    exclusion_reason: str | None = None
    morphometry_source: str = ""
    notes: str = ""
    thresholds: Thresholds = field(default_factory=Thresholds)
    ecological_context: EcologicalContext = field(default_factory=EcologicalContext)

    @property
    def watershed_ratio(self) -> float:
        """Удельный водосбор: во сколько раз площадь водосбора больше зеркала озера."""
        return round(self.watershed_km2 / self.area_km2, 3)


@dataclass(frozen=True)
class ProjectConfig:
    """Полная конфигурация проекта: настройки конвейера + список водоёмов."""

    pipeline: PipelineConfig
    water_bodies: tuple[WaterBodyConfig, ...]

    def get_lakes(
        self,
        lake_ids: list[str] | None = None,
        include_disabled: bool = False,
    ) -> list[WaterBodyConfig]:
        """Возвращает отфильтрованный список озёр по списку ID и флагу enabled."""
        by_id = {wb.id: wb for wb in self.water_bodies}
        if lake_ids:
            missing = [lid for lid in lake_ids if lid not in by_id]
            if missing:
                available = ", ".join(sorted(by_id))
                raise KeyError(
                    f"Неизвестный код озера: {missing}. Доступные коды: {available}"
                )
            selected = [by_id[lid] for lid in lake_ids]
            if not include_disabled:
                disabled = [wb.id for wb in selected if not wb.enabled]
                if disabled:
                    raise ValueError(
                        f"Озёра {disabled} отключены в configs/water_bodies.yaml. "
                        f"Передайте --include-disabled, если хотите обработать их принудительно."
                    )
            return selected

        if include_disabled:
            return list(self.water_bodies)
        return [wb for wb in self.water_bodies if wb.enabled]


def load_config(config_path: Path | None = None) -> ProjectConfig:
    """Читает и валидирует configs/water_bodies.yaml."""
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Некорректный формат YAML в {path}")

    pipe_raw = raw["pipeline"]
    def_thr_raw = pipe_raw.get("default_thresholds", {})
    default_thresholds = Thresholds(
        attention_ug_l=float(def_thr_raw.get("attention_ug_l", 15.0)),
        bloom_ug_l=float(def_thr_raw.get("bloom_ug_l", 25.0)),
    )

    pipeline = PipelineConfig(
        start_date=str(pipe_raw["start_date"]),
        end_date=str(pipe_raw["end_date"]),
        stac_api_url=str(pipe_raw["stac_api_url"]),
        stac_collection=str(pipe_raw["stac_collection"]),
        max_scene_cloud_cover_pct=float(pipe_raw["max_scene_cloud_cover_pct"]),
        shore_buffer_m=float(pipe_raw["shore_buffer_m"]),
        min_valid_water_pixels=int(pipe_raw["min_valid_water_pixels"]),
        min_water_coverage_pct=float(pipe_raw["min_water_coverage_pct"]),
        default_thresholds=default_thresholds,
    )

    lakes: list[WaterBodyConfig] = []
    seen_ids: set[str] = set()
    for item in raw.get("water_bodies", []):
        wb_id = str(item["id"]).strip()
        if not wb_id or wb_id in seen_ids:
            raise ValueError(f"Пустой или дублирующийся id водоёма: {wb_id!r}")
        seen_ids.add(wb_id)

        osm_type = str(item["osm_type"]).strip().lower()
        if osm_type not in {"relation", "way"}:
            raise ValueError(
                f"Озеро {wb_id}: osm_type должен быть 'relation' или 'way', получено {osm_type!r}"
            )

        thr_raw = item.get("thresholds")
        if thr_raw:
            thr = Thresholds(
                attention_ug_l=float(
                    thr_raw.get("attention_ug_l", default_thresholds.attention_ug_l)
                ),
                bloom_ug_l=float(
                    thr_raw.get("bloom_ug_l", default_thresholds.bloom_ug_l)
                ),
            )
        else:
            thr = default_thresholds

        eco_raw = item.get("ecological_context") or {}
        eco = EcologicalContext(
            protected_area=str(eco_raw.get("protected_area", "")),
            chemical_treatment_allowed=bool(
                eco_raw.get("chemical_treatment_allowed", False)
            ),
            stratification_type=str(eco_raw.get("stratification_type", "unknown")),
            cascade_inflow_from=tuple(
                str(x) for x in (eco_raw.get("cascade_inflow_from") or [])
            ),
            cascade_outflow_to=str(eco_raw.get("cascade_outflow_to", "")),
        )

        lakes.append(
            WaterBodyConfig(
                id=wb_id,
                name_ru=str(item["name_ru"]),
                name_be=str(item.get("name_be", "")),
                group=str(item.get("group", "")),
                basin=str(item.get("basin", "")),
                osm_type=osm_type,
                osm_id=int(item["osm_id"]),
                area_km2=float(item["area_km2"]),
                max_depth_m=float(item["max_depth_m"]),
                mean_depth_m=float(item["mean_depth_m"]),
                volume_mln_m3=float(item["volume_mln_m3"]),
                watershed_km2=float(item["watershed_km2"]),
                has_lab_data=bool(item.get("has_lab_data", False)),
                enabled=bool(item.get("enabled", True)),
                exclusion_reason=item.get("exclusion_reason"),
                morphometry_source=str(item.get("morphometry_source", "")),
                notes=str(item.get("notes", "")),
                thresholds=thr,
                ecological_context=eco,
            )
        )

    return ProjectConfig(pipeline=pipeline, water_bodies=tuple(lakes))
