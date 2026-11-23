"""Проверки Этапа 1: корректность справочника озёр и геометрии OpenStreetMap."""

from __future__ import annotations

import json

from src.etl.config import load_config
from src.etl.osm_geometry import LAKES_GEOJSON_PATH, geodetic_area_km2, load_lakes_geometries


def test_config_loads_and_has_unique_ids() -> None:
    cfg = load_config()
    assert len(cfg.water_bodies) >= 3
    ids = [wb.id for wb in cfg.water_bodies]
    assert len(ids) == len(set(ids))
    assert cfg.pipeline.min_valid_water_pixels >= 20
    assert cfg.pipeline.shore_buffer_m > 0


def test_lakes_geojson_geometries_valid_and_areas_match() -> None:
    assert LAKES_GEOJSON_PATH.exists(), "lakes.geojson должен быть создан Этапом 1"
    raw = json.loads(LAKES_GEOJSON_PATH.read_text(encoding="utf-8"))
    assert raw["type"] == "FeatureCollection"
    assert len(raw["features"]) >= 3

    geoms = load_lakes_geometries(only_enabled=False)
    for lid, (props, geom) in geoms.items():
        assert geom.is_valid, f"Невалидная геометрия для {lid}"
        assert not geom.is_empty, f"Пустая геометрия для {lid}"
        area_osm = geodetic_area_km2(geom)
        area_ref = float(props["area_km2_ref"])
        # Проверка, что OSM ID указывает на правильное озеро (расхождение площади < 15%)
        rel_diff = abs(area_osm - area_ref) / area_ref
        assert rel_diff < 0.15, (
            f"Озеро {lid}: площадь OSM ({area_osm:.3f}) сильно отличается от справочника ({area_ref:.3f})"
        )

    # Проверяем, что острова реально вырезаны из полигонов Мяделя и Нарочи
    assert geoms["myadel"][0]["islands_count"] >= 5
    assert geoms["naroch"][0]["islands_count"] >= 1


def test_ecological_context_present_for_part3() -> None:
    cfg = load_config()
    active = cfg.get_lakes()
    assert len(active) == 8  # 8 активных + 1 резерв (rossokhi)
    by_id = {wb.id: wb for wb in cfg.water_bodies}
    assert by_id["rossokhi"].enabled is False
    for wb in active:
        assert "Нарочанский" in wb.ecological_context.protected_area
        assert wb.ecological_context.chemical_treatment_allowed is False
    assert "batorino" in by_id["myastro"].ecological_context.cascade_inflow_from
    assert by_id["batorino"].ecological_context.cascade_outflow_to == "myastro"

