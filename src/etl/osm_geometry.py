"""Загрузка реальных береговых контуров озёр из OpenStreetMap API v0.6.

Что делает этот модуль:
1. Читает список водоёмов из configs/water_bodies.yaml.
2. Для каждого озера скачивает (и кэширует в data/raw/osm/) полный граф узлов
   и линий объекта из OpenStreetMap API v0.6 (/api/0.6/{way|relation}/{id}/full.json).
3. Сшивает внешнюю береговую линию и вырезает внутренние острова (inner holes)
   с помощью shapely.ops.polygonize, гарантируя корректность геометрии.
4. Вычисляет реальную геодезическую площадь по эллипсоиду WGS84 и сверяет её
   со справочной площадью из configs/water_bodies.yaml (защита от неверного OSM ID!).
5. Вычисляет гарантированно внутреннюю точку озера (representative_point) и
   сохраняет итоговый картографический файл data/reference/lakes.geojson.

Поддерживает инкрементальное добавление одного озера:
    python -m src.etl.osm_geometry --lake naroch
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests
from pyproj import Geod
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping, shape
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

from src.etl.config import RAW_DIR, REFERENCE_DIR, WaterBodyConfig, load_config

OSM_API_BASE = "https://api.openstreetmap.org/api/0.6"
USER_AGENT = "BelarusLakeEutrophicationETL/1.0 (BSUIR/BSU coursework research)"
WGS84_GEOD = Geod(ellps="WGS84")

OSM_RAW_DIR = RAW_DIR / "osm"
LAKES_GEOJSON_PATH = REFERENCE_DIR / "lakes.geojson"


def fetch_osm_full_json(
    wb: WaterBodyConfig,
    force_refresh: bool = False,
    timeout: int = 45,
) -> dict[str, Any]:
    """Скачивает (или берёт из кэша) полный JSON объекта из OSM API v0.6."""
    OSM_RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = OSM_RAW_DIR / f"{wb.id}_{wb.osm_type}_{wb.osm_id}.json"

    if cache_path.exists() and not force_refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    url = f"{OSM_API_BASE}/{wb.osm_type}/{wb.osm_id}/full.json"
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            # Уважаем лимиты публичного OSM API между живыми запросами
            time.sleep(0.4)
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            wait_s = attempt * 2.0
            print(
                f"  [OSM] Ошибка запроса для {wb.id} (попытка {attempt}/3): {exc}. "
                f"Повтор через {wait_s:.0f} с..."
            )
            time.sleep(wait_s)

    raise RuntimeError(
        f"Не удалось загрузить контур OSM для {wb.id} ({url}): {last_error}"
    )


def _extract_polygonal(geom: Any) -> Polygon | MultiPolygon:
    """Оставляет только полигональную часть после make_valid (отбрасывая вырожденные линии/точки)."""
    if geom.is_empty:
        raise ValueError("Получена пустая геометрия после обработки OSM-контура.")
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, (Polygon, MultiPolygon))]
    if not polys:
        raise ValueError(f"Геометрия не содержит полигонов: {geom.geom_type}")
    merged = unary_union(polys)
    if not isinstance(merged, (Polygon, MultiPolygon)):
        raise ValueError(f"Неожиданный тип после объединения полигонов: {merged.geom_type}")
    return merged


def _build_way_linestring(
    way: dict[str, Any], nodes_by_id: dict[int, tuple[float, float]]
) -> LineString | None:
    """Собирает LineString в координатах (lon, lat) из списка ID узлов линии OSM."""
    coords = [nodes_by_id[nid] for nid in way.get("nodes", []) if nid in nodes_by_id]
    if len(coords) < 2:
        return None
    return LineString(coords)


def build_polygon_from_osm(
    wb: WaterBodyConfig, payload: dict[str, Any]
) -> tuple[Polygon | MultiPolygon, dict[str, str]]:
    """Сшивает замкнутый полигон озера (с учётом островов) из ответа OSM /full.json."""
    elements = payload.get("elements", [])
    nodes_by_id: dict[int, tuple[float, float]] = {}
    ways_by_id: dict[int, dict[str, Any]] = {}
    target_relation: dict[str, Any] | None = None
    target_way: dict[str, Any] | None = None

    for el in elements:
        el_type = el.get("type")
        el_id = el.get("id")
        if el_type == "node":
            nodes_by_id[el_id] = (float(el["lon"]), float(el["lat"]))
        elif el_type == "way":
            ways_by_id[el_id] = el
            if wb.osm_type == "way" and el_id == wb.osm_id:
                target_way = el
        elif el_type == "relation" and wb.osm_type == "relation" and el_id == wb.osm_id:
            target_relation = el

    if wb.osm_type == "way":
        if target_way is None:
            raise ValueError(f"В ответе OSM не найдена линия way/{wb.osm_id}")
        coords = [
            nodes_by_id[nid] for nid in target_way.get("nodes", []) if nid in nodes_by_id
        ]
        if len(coords) < 4:
            raise ValueError(f"Слишком мало узлов ({len(coords)}) в way/{wb.osm_id}")
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        geom = _extract_polygonal(make_valid(Polygon(coords)))
        return geom, dict(target_way.get("tags", {}))

    # Случай relation (мультиполигон, часто с островами role="inner")
    if target_relation is None:
        raise ValueError(f"В ответе OSM не найдено отношение relation/{wb.osm_id}")

    outer_lines: list[LineString] = []
    inner_lines: list[LineString] = []
    for member in target_relation.get("members", []):
        if member.get("type") != "way":
            continue
        way = ways_by_id.get(member["ref"])
        if way is None:
            continue
        line = _build_way_linestring(way, nodes_by_id)
        if line is None:
            continue
        role = (member.get("role") or "outer").strip().lower()
        if role == "inner":
            inner_lines.append(line)
        else:
            outer_lines.append(line)

    # polygonize автоматически сшивает разорванные отрезки берега в замкнутые кольца
    outer_polys = [make_valid(p) for p in polygonize(outer_lines)]
    if not outer_polys:
        raise ValueError(
            f"Не удалось сшить внешнее кольцо для relation/{wb.osm_id} "
            f"(outer линий: {len(outer_lines)})"
        )
    outer_union = unary_union(outer_polys)

    inner_polys = [make_valid(p) for p in polygonize(inner_lines)]
    if inner_polys:
        inner_union = unary_union(inner_polys)
        geom = _extract_polygonal(make_valid(outer_union.difference(inner_union)))
    else:
        geom = _extract_polygonal(make_valid(outer_union))

    return geom, dict(target_relation.get("tags", {}))


def geodetic_area_km2(geom: Polygon | MultiPolygon) -> float:
    """Вычисляет точную площадь полигона (км²) на эллипсоиде WGS84 с учётом вырезов островов."""
    area_m2, _ = WGS84_GEOD.geometry_area_perimeter(geom)
    return abs(area_m2) / 1_000_000.0


def count_islands(geom: Polygon | MultiPolygon) -> int:
    """Считает число внутренних вырезов (островов) в полигоне озера."""
    if isinstance(geom, Polygon):
        return len(geom.interiors)
    return sum(len(p.interiors) for p in geom.geoms)


def build_feature(wb: WaterBodyConfig, force_refresh: bool = False) -> dict[str, Any]:
    """Загружает и собирает GeoJSON Feature с проверенными атрибутами для одного озера."""
    payload = fetch_osm_full_json(wb, force_refresh=force_refresh)
    geom, osm_tags = build_polygon_from_osm(wb, payload)

    osm_area_km2 = geodetic_area_km2(geom)
    # Гарантированно внутренняя точка озера (важно для озёр сложной формы с островами)
    rep_point = geom.representative_point()
    minx, miny, maxx, maxy = geom.bounds
    n_islands = count_islands(geom)

    rel_diff_pct = (
        abs(osm_area_km2 - wb.area_km2) / wb.area_km2 * 100.0 if wb.area_km2 > 0 else 0.0
    )
    if rel_diff_pct > 40.0:
        print(
            f"  [ВНИМАНИЕ] {wb.id}: расхождение площади OSM ({osm_area_km2:.4f} км²) "
            f"и справочника ({wb.area_km2:.4f} км²) = {rel_diff_pct:.1f}% — проверьте OSM ID!"
        )

    properties = {
        "id": wb.id,
        "name_ru": wb.name_ru,
        "name_be": wb.name_be,
        "group": wb.group,
        "basin": wb.basin,
        "osm_type": wb.osm_type,
        "osm_id": wb.osm_id,
        "osm_name": osm_tags.get("name") or osm_tags.get("name:ru") or "",
        "enabled": wb.enabled,
        "exclusion_reason": wb.exclusion_reason,
        "has_lab_data": wb.has_lab_data,
        "area_km2_ref": wb.area_km2,
        "area_km2_osm": round(osm_area_km2, 5),
        "area_diff_pct": round(rel_diff_pct, 2),
        "islands_count": n_islands,
        "max_depth_m": wb.max_depth_m,
        "mean_depth_m": wb.mean_depth_m,
        "volume_mln_m3": wb.volume_mln_m3,
        "watershed_km2": wb.watershed_km2,
        "watershed_ratio": wb.watershed_ratio,
        "center_lon": round(float(rep_point.x), 6),
        "center_lat": round(float(rep_point.y), 6),
        "bbox": [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)],
        "attention_ug_l": wb.thresholds.attention_ug_l,
        "bloom_ug_l": wb.thresholds.bloom_ug_l,
        "protected_area": wb.ecological_context.protected_area,
        "chemical_treatment_allowed": wb.ecological_context.chemical_treatment_allowed,
        "stratification_type": wb.ecological_context.stratification_type,
        "cascade_inflow_from": list(wb.ecological_context.cascade_inflow_from),
        "cascade_outflow_to": wb.ecological_context.cascade_outflow_to,
        "morphometry_source": wb.morphometry_source,
        "notes": wb.notes,
    }

    return {
        "type": "Feature",
        "id": wb.id,
        "properties": properties,
        "geometry": mapping(geom),
    }


def load_lakes_geometries(
    geojson_path: Path | None = None,
    lake_ids: list[str] | None = None,
    only_enabled: bool = True,
) -> dict[str, tuple[dict[str, Any], Polygon | MultiPolygon]]:
    """Читает data/reference/lakes.geojson и возвращает словарь {lake_id: (props, shapely_geom)}."""
    path = geojson_path or LAKES_GEOJSON_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Файл контуров не найден: {path}. Сначала выполните: python -m src.etl.osm_geometry"
        )
    fc = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, tuple[dict[str, Any], Polygon | MultiPolygon]] = {}
    for feat in fc.get("features", []):
        props = feat["properties"]
        lid = props["id"]
        if lake_ids and lid not in lake_ids:
            continue
        if only_enabled and not props.get("enabled", True):
            continue
        result[lid] = (props, shape(feat["geometry"]))
    return result


def sync_lakes_geojson(
    lake_ids: list[str] | None = None,
    include_disabled: bool = False,
    force_refresh: bool = False,
    config_path: Path | None = None,
) -> Path:
    """Обновляет data/reference/lakes.geojson (целиком или инкрементально для выбранных --lake)."""
    cfg = load_config(config_path)
    target_lakes = cfg.get_lakes(lake_ids=lake_ids, include_disabled=include_disabled)

    # При инкрементальном запуске (--lake <id>) сохраняем уже существующие озёра в GeoJSON
    existing_features: dict[str, dict[str, Any]] = {}
    if lake_ids and LAKES_GEOJSON_PATH.exists():
        old_fc = json.loads(LAKES_GEOJSON_PATH.read_text(encoding="utf-8"))
        for feat in old_fc.get("features", []):
            existing_features[feat["properties"]["id"]] = feat

    print(
        f"[OSM] Загрузка и проверка контуров для {len(target_lakes)} водоёмов "
        f"(кэш: {OSM_RAW_DIR})..."
    )
    print(
        f"{'ID':<15} {'OSM объект':<19} {'Спр., км²':>9} {'OSM, км²':>9} "
        f"{'Расх.%':>7} {'Остр.':>5} {'Центр (lat, lon)':<22} {'OSM name'}"
    )
    print("-" * 108)

    for wb in target_lakes:
        feat = build_feature(wb, force_refresh=force_refresh)
        existing_features[wb.id] = feat
        p = feat["properties"]
        osm_ref = f"{wb.osm_type}/{wb.osm_id}"
        center_str = f"{p['center_lat']:.5f}, {p['center_lon']:.5f}"
        print(
            f"{wb.id:<15} {osm_ref:<19} {p['area_km2_ref']:>9.3f} {p['area_km2_osm']:>9.3f} "
            f"{p['area_diff_pct']:>6.1f}% {p['islands_count']:>5d} {center_str:<22} {p['osm_name']}"
        )

    # Сохраняем порядок озёр строго как в configs/water_bodies.yaml
    ordered_ids = [wb.id for wb in cfg.water_bodies if wb.id in existing_features]
    features = [existing_features[lid] for lid in ordered_ids]

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    fc = {
        "type": "FeatureCollection",
        "name": "belarus_eutrophication_lakes",
        "crs_note": "WGS84 (EPSG:4326)",
        "features": features,
    }
    LAKES_GEOJSON_PATH.write_text(
        json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[OSM] Сохранено {len(features)} контуров в {LAKES_GEOJSON_PATH}")
    return LAKES_GEOJSON_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Скачать реальные контуры озёр из OpenStreetMap и собрать lakes.geojson"
    )
    parser.add_argument(
        "--lake",
        action="append",
        dest="lakes",
        help="Код конкретного озера (можно указать несколько раз: --lake naroch --lake myadel)",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Включить также отключённые (enabled: false) озёра из конфигурации",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Перезагрузить JSON из OSM API, игнорируя локальный кэш",
    )
    args = parser.parse_args()
    sync_lakes_geojson(
        lake_ids=args.lakes,
        include_disabled=args.include_disabled,
        force_refresh=args.force_refresh,
    )


if __name__ == "__main__":
    main()
