"""
Переходники к РЕАЛЬНЫМ компонентам Частей 1-3. Интерфейсы такие же, как у заглушек в stubs.py.
Вся «догадливость» про чужие форматы сосредоточена здесь: если что-то не подойдёт,
правится только этот файл, а API и дашборд не трогаем.

Поддерживаются оба режима DATA_CONTRACT.md:
  features_satellite.parquet — спутниковый (chl_a_est_current)
  features_lab.parquet       — лабораторный (chl_a_lab_ug_l)
"""
from __future__ import annotations

import importlib
import inspect
import json
import math
from calendar import monthrange
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import joblib
import pandas as pd
import yaml

from src.api.stubs import AlertVerdict, enrich_verdict_with_stubs


class IntegrationError(RuntimeError):
    """Файл или компонент другой части не соответствует ожидаемому контракту."""


class ModeUnavailable(RuntimeError):
    """Для выбранного режима нет данных или модели (честно сообщаем, а не подставляем другое)."""


def _num(x) -> Optional[float]:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _int(x) -> Optional[int]:
    v = _num(x)
    return None if v is None else int(round(v))


# ------------------------------------------------------------------ Часть 1
def _first(d: dict, keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _norm_bbox(raw) -> Optional[dict]:
    """BBox как словарь или список из 4 чисел в любом порядке (широта 50-57, долгота 22-34)."""
    if isinstance(raw, dict):
        g = lambda ks: _num(_first(raw, ks))
        b = dict(min_lat=g(["min_lat", "south", "lat_min", "miny"]), min_lon=g(["min_lon", "west", "lon_min", "minx"]),
                 max_lat=g(["max_lat", "north", "lat_max", "maxy"]), max_lon=g(["max_lon", "east", "lon_max", "maxx"]))
        return b if all(v is not None for v in b.values()) else None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        nums = [_num(x) for x in raw]
        lats = [x for x in nums if x is not None and 50 <= x <= 57]
        lons = [x for x in nums if x is not None and 22 <= x <= 34]
        if len(lats) == 2 and len(lons) == 2:
            return dict(min_lat=min(lats), min_lon=min(lons), max_lat=max(lats), max_lon=max(lons))
    return None


def load_registry(path: Path) -> Dict[str, dict]:
    raw_root = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    default_thr = {}
    if isinstance(raw_root, dict) and isinstance(raw_root.get("pipeline"), dict):
        default_thr = raw_root["pipeline"].get("default_thresholds") or {}
    att_default = _num(default_thr.get("attention_ug_l")) or 15.0
    bloom_default = _num(default_thr.get("bloom_ug_l")) or 25.0

    raw = raw_root
    if isinstance(raw, dict) and "water_bodies" in raw:
        raw = raw["water_bodies"]
    if isinstance(raw, dict):
        raw = [({"id": k, **v} if isinstance(v, dict) else {"id": k}) for k, v in raw.items()]
    if not isinstance(raw, list):
        raise IntegrationError(f"{path}: не понял структуру реестра водоёмов")
    out: Dict[str, dict] = {}
    for e in raw:
        flat = dict(e)
        if flat.get("enabled") is False:
            continue
        for nested in ("morphometry", "coordinates", "location"):
            if isinstance(e.get(nested), dict):
                flat.update(e[nested])
        wb_id = _first(flat, ["id", "water_body_id", "code"])
        if wb_id is None:
            raise IntegrationError(f"{path}: у водоёма нет id: {list(flat)[:8]}")
        bbox = _norm_bbox(_first(flat, ["bbox", "BBox", "bounding_box"]))
        lat = _num(_first(flat, ["center_lat", "lat", "latitude"]))
        lon = _num(_first(flat, ["center_lon", "lon", "lng", "longitude"]))
        if (lat is None or lon is None) and bbox:
            lat, lon = (bbox["min_lat"] + bbox["max_lat"]) / 2, (bbox["min_lon"] + bbox["max_lon"]) / 2
        eco = flat.get("ecological_context") if isinstance(flat.get("ecological_context"), dict) else {}
        protected = _first(flat, ["protected_area", "oopt", "protection"])
        if protected is None and eco:
            protected = _first(eco, ["protected_area", "oopt", "status"])
        inflow = eco.get("cascade_inflow_from")
        if isinstance(inflow, str):
            inflow_list = [inflow]
        elif isinstance(inflow, list):
            inflow_list = [str(x) for x in inflow if x]
        else:
            inflow_list = []
        outflow = eco.get("cascade_outflow_to")
        chem_allowed = eco.get("chemical_treatment_allowed")
        strat = eco.get("stratification_type")
        area = _num(_first(flat, ["area_km2", "area"]))
        watershed = _num(_first(flat, ["watershed_km2", "catchment_km2", "watershed_area_km2"]))
        ratio = _num(_first(flat, ["watershed_ratio", "catchment_ratio"]))
        if ratio is None and watershed and area:
            ratio = round(watershed / area, 2)
        out[str(wb_id)] = dict(
            id=str(wb_id), name=str(_first(flat, ["name", "title", "name_ru"], wb_id)),
            type=str(_first(flat, ["type", "kind"], "lake")), lat=lat, lon=lon, bbox=bbox,
            group=str(flat["group"]) if flat.get("group") else None,
            basin=str(flat["basin"]) if flat.get("basin") else None,
            notes=str(flat["notes"]) if flat.get("notes") else None,
            area_km2=area,
            mean_depth_m=_num(_first(flat, ["mean_depth_m", "mean_depth", "depth_mean", "depth_m", "depth"])),
            max_depth_m=_num(_first(flat, ["max_depth_m", "max_depth", "depth_max"])),
            volume_mln_m3=_num(_first(flat, ["volume_mln_m3", "volume", "volume_mln"])),
            watershed_km2=watershed, watershed_ratio=ratio,
            protected_area=str(protected) if protected else None,
            has_lab_data=bool(flat["has_lab_data"]) if "has_lab_data" in flat else None,
            chemical_treatment_allowed=bool(chem_allowed) if chem_allowed is not None else None,
            stratification_type=str(strat) if strat else None,
            cascade_inflow_from=inflow_list,
            cascade_outflow_to=str(outflow) if outflow else None,
            attention_ug_l=_num(flat.get("attention_ug_l")) or att_default,
            bloom_ug_l=_num(flat.get("bloom_ug_l")) or bloom_default)
    return out


def _geom_bounds_and_center(geom: dict) -> Tuple[Optional[dict], Optional[float], Optional[float]]:
    """Вычислить bbox и приближённый центр полигона из координат GeoJSON, если их нет в YAML/properties."""
    coords = []
    gtype = geom.get("type")
    if gtype == "Polygon" and geom.get("coordinates"):
        coords = geom["coordinates"][0]
    elif gtype == "MultiPolygon" and geom.get("coordinates"):
        for poly in geom["coordinates"]:
            if poly:
                coords.extend(poly[0])
    pts = [(float(p[0]), float(p[1])) for p in coords if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not pts:
        return None, None, None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    bbox = dict(min_lat=round(min(lats), 6), min_lon=round(min(lons), 6),
                max_lat=round(max(lats), 6), max_lon=round(max(lons), 6))
    return bbox, round((bbox["min_lat"] + bbox["max_lat"]) / 2, 6), round((bbox["min_lon"] + bbox["max_lon"]) / 2, 6)


def load_geometry_details(path: Path) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """Полигоны и свойства озёр из GeoJSON (lakes.geojson)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    kind = raw.get("type")
    feats = raw.get("features", []) if kind == "FeatureCollection" else [raw] if kind == "Feature" else []
    geoms: Dict[str, dict] = {}
    props_by_id: Dict[str, dict] = {}
    for f in feats:
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        props = f.get("properties") or {}
        key = _first(props, ["id", "water_body_id", "lake_id", "code"], f.get("id"))
        if key is not None:
            k = str(key)
            geoms[k] = geom
            props_by_id[k] = props
    return geoms, props_by_id


def load_geometry(path: Path) -> Dict[str, dict]:
    """Полигоны озёр из GeoJSON (lakes.geojson): идентификатор -> геометрия Polygon/MultiPolygon."""
    geoms, _ = load_geometry_details(path)
    return geoms


def _round_coords(obj, ndigits: int = 5):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(x), ndigits) for x in obj]
        return [_round_coords(x, ndigits) for x in obj]
    return obj


def _compute_shore_buffer(geom: dict, buffer_m: float = 25.0) -> Optional[dict]:
    """Вычисляет реальный внутренний буфер −25 м (UTM 35N) — рабочую зону расчёта Sentinel-2 без берега и островов."""
    if not geom or buffer_m <= 0:
        return None
    try:
        import numpy as np
        import shapely
        from pyproj import Transformer
        from shapely.geometry import mapping, shape
    except ImportError:
        return None
    try:
        to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32635", always_xy=True)
        to_wgs = Transformer.from_crs("EPSG:32635", "EPSG:4326", always_xy=True)

        def _proj(coords, tr):
            arr = np.asarray(coords, dtype=float)
            xs, ys = tr.transform(arr[:, 0], arr[:, 1])
            return np.column_stack((xs, ys))

        g = shape(geom)
        if g.is_empty:
            return None
        g_utm = shapely.transform(g, lambda c: _proj(c, to_utm))
        buf_utm = g_utm.buffer(-float(buffer_m))
        if buf_utm.is_empty:
            return None
        buf_utm = buf_utm.simplify(5.0, preserve_topology=True)
        buf_wgs = shapely.transform(buf_utm, lambda c: _proj(c, to_wgs))
        res = mapping(buf_wgs)
        return {"type": res["type"], "coordinates": _round_coords(res["coordinates"], 5)}
    except Exception:  # noqa: BLE001
        return None


def hydrate_registry_geometry(registry: Dict[str, dict], geo_path: Path, shore_buffer_m: float = 25.0) -> None:
    """Прикрепить полигоны, 25м внутренний буфер и дозаполнить center_lat/center_lon/bbox из GeoJSON."""
    geoms, props_by_id = load_geometry_details(geo_path)
    for wb_id, wb in registry.items():
        geom = geoms.get(wb_id)
        wb["geometry"] = geom
        wb["buffer_geometry"] = _compute_shore_buffer(geom, shore_buffer_m) if geom else None
        props = props_by_id.get(wb_id) or {}
        if wb.get("bbox") is None and props.get("bbox") is not None:
            wb["bbox"] = _norm_bbox(props.get("bbox"))
        if wb.get("lat") is None:
            wb["lat"] = _num(_first(props, ["center_lat", "lat", "latitude"]))
        if wb.get("lon") is None:
            wb["lon"] = _num(_first(props, ["center_lon", "lon", "lng", "longitude"]))
        if geom and (wb.get("bbox") is None or wb.get("lat") is None or wb.get("lon") is None):
            gb, glat, glon = _geom_bounds_and_center(geom)
            if wb.get("bbox") is None:
                wb["bbox"] = gb
            if wb.get("lat") is None:
                wb["lat"] = glat
            if wb.get("lon") is None:
                wb["lon"] = glon


def load_decadal_frame(path: Path, registry: Dict[str, dict]) -> pd.DataFrame:
    """Читает полный декадный ряд satellite_decadal.parquet (включая лёд, облака и 1-шаговую интерполяцию)."""
    path = Path(path)
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    if "water_body_id" not in df.columns or "date" not in df.columns:
        raise IntegrationError(f"В {path.name} нет столбцов 'water_body_id' и 'date'")
    df = df.copy()
    df["water_body_id"] = df["water_body_id"].astype(str)
    df = df[df["water_body_id"].isin(set(registry))]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.sort_values(["water_body_id", "date"]).reset_index(drop=True)


CHL_COLUMNS = ["chl_a_est_current", "chl_a_est_ug_l", "chl_a", "chl_a_current", "chl_a_mean", "chl_a_now", "chlorophyll_a", "chl"]
LAB_CHL_COLUMNS = ["chl_a_lab_ug_l", "chl_a_lab", "chl_a_measured", "chl_a_lab_current",
                   "chl_a_lab_mean", "lab_chl_a", "chl_a", "chl"]
DATE_COLUMNS = ["date", "sample_date", "obs_date", "date_used"]

MODE_SOURCE = {
    "satellite": "Sentinel-2 L2A (ESA/Copernicus)",
    "lab": "Лабораторная проба",
}


class RealDataProvider:
    """Читает витрину признаков (.parquet или .csv). Ключ строки: (water_body_id, date)."""

    def __init__(self, path: Path, registry: Dict[str, dict], chl_column: Optional[str] = None,
                 mode: str = "satellite"):
        path = Path(path)
        df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
        self.mode = mode
        date_col = next((c for c in DATE_COLUMNS if c in df.columns), None)
        if "water_body_id" not in df.columns or date_col is None:
            raise IntegrationError(f"В {path.name} нет столбцов 'water_body_id' и/или {DATE_COLUMNS}. "
                                   f"Есть: {list(df.columns)}")
        if date_col != "date":
            df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values(["water_body_id", "date"]).reset_index(drop=True)
        self.by_wb = {str(k): g.reset_index(drop=True) for k, g in df.groupby("water_body_id")}
        unknown = set(self.by_wb) - set(registry)
        if unknown:
            raise IntegrationError(f"В витрине есть водоёмы, которых нет в реестре: {sorted(unknown)}")
        candidates = LAB_CHL_COLUMNS if mode == "lab" else CHL_COLUMNS
        self.chl_col = chl_column or next((c for c in candidates if c in df.columns), None)
        if self.chl_col is None:
            raise IntegrationError(f"Не нашёл столбец хлорофилла-a среди {candidates}. Есть: {list(df.columns)}. "
                                   f"Укажите его в переменной WATER_CHL_COLUMN.")
        self.columns = list(df.columns)
        self.path = path

    def supports(self, wb_id: str) -> bool:
        return wb_id in self.by_wb

    def available_dates(self, wb_id: str) -> List[date]:
        g = self.by_wb.get(wb_id)
        return [] if g is None else list(g["date"])

    def date_range(self, wb_id: str) -> Tuple[date, date]:
        ds = self.available_dates(wb_id)
        return ds[0], ds[-1]

    @staticmethod
    def _status(row) -> Optional[str]:
        status = row.get("obs_status")
        return None if status is None or pd.isna(status) else str(status)

    def _is_interpolated(self, row, status: Optional[str]) -> bool:
        interp = row.get("is_interpolated")
        return (status == "interpolated_1step") if interp is None or pd.isna(interp) else bool(interp)

    def _source(self, row) -> str:
        return MODE_SOURCE[self.mode]

    @staticmethod
    def _str_first(row, keys) -> Optional[str]:
        for k in keys:
            v = row.get(k)
            if v is not None and not (isinstance(v, float) and math.isnan(v)) and not pd.isna(v):
                s = str(v).strip()
                if s:
                    return s
        return None

    @staticmethod
    def _num_first(row, keys) -> Optional[float]:
        for k in keys:
            v = _num(row.get(k))
            if v is not None:
                return v
        return None

    def row_meta(self, row) -> dict:
        status = self._status(row)
        return dict(
            date=row["date"], obs_status=status, is_interpolated=self._is_interpolated(row, status),
            chl_a_current=_num(row[self.chl_col]),
            tsi_current=self._num_first(row, ("tsi_current", "tsi_est")),
            ndci_current=self._num_first(row, ("ndci_current", "ndci", "s2_ndci")),
            b04_b02_ratio=self._num_first(row, ("b04_b02_ratio_current", "b04_b02_ratio", "s2_b04_b02_ratio")),
            temp_mean_14d=_num(row.get("temp_mean_14d")),
            calm_fraction_7d=_num(row.get("calm_fraction_7d")),
            precip_sum_14d=_num(row.get("precip_sum_14d")),
            wind_mean_7d=_num(row.get("wind_mean_7d")),
            radiation_mean_7d=_num(row.get("radiation_mean_7d")),
            secchi_depth_m=_num(row.get("secchi_depth_m")),
            total_phosphorus_ug_l=_num(row.get("total_phosphorus_ug_l")),
            water_coverage_pct=_num(row.get("water_coverage_pct")),
            scenes_in_decade=_int(row.get("scenes_in_decade")),
            valid_water_pixels=_int(row.get("valid_water_pixels")),
            scene_id=self._str_first(row, ("scene_id", "last_scene_id")),
            source_note=self._str_first(row, ("source_note",)),
            source=self._source(row))

    def get_features(self, wb_id: str, d: date) -> Optional[dict]:
        g = self.by_wb.get(wb_id)
        if g is None:
            return None
        dates = list(g["date"])
        if d < dates[0] or d > dates[-1]:
            if min(abs((d - dates[0]).days), abs((d - dates[-1]).days)) > 10:
                return None
        i = min(range(len(dates)), key=lambda k: abs((dates[k] - d).days))
        row = g.iloc[i]
        meta = self.row_meta(row)
        meta.update(date_used=meta["date"],
                    _wb=wb_id, _doy=meta["date"].timetuple().tm_yday, _mode=self.mode,
                    _frame=g.iloc[[i]])
        return meta

    def history_frame(self, wb_id: str, end: date, limit: int = 120) -> pd.DataFrame:
        """Последние `limit` декад до `end` включительно (для графика и batch-прогноза)."""
        g = self.by_wb.get(wb_id)
        if g is None:
            return pd.DataFrame(columns=self.columns)
        part = g[g["date"] <= end].tail(limit).reset_index(drop=True)
        return part


# ------------------------------------------------------------------ Часть 2
def _feature_names(model) -> List[str]:
    for attr in ("feature_names_in_", "feature_name_"):
        v = getattr(model, attr, None)
        if v is not None:
            return list(v)
    booster = getattr(model, "booster_", None)
    if booster is not None:
        return list(booster.feature_name())
    raise IntegrationError(f"У модели {type(model).__name__} нет списка признаков (обучать нужно на DataFrame).")


class RealPredictor:
    """Модели Части 2 (sklearn-интерфейс). Опционально — отдельный классификатор внимания (>=15 мкг/л)."""

    def __init__(self, reg_path: Path, clf_path: Path, attention_path: Optional[Path] = None):
        self.reg, self.clf = joblib.load(reg_path), joblib.load(clf_path)
        self.attention = joblib.load(attention_path) if attention_path else None
        self.reg_cols, self.clf_cols = _feature_names(self.reg), _feature_names(self.clf)
        self.attention_cols = _feature_names(self.attention) if self.attention is not None else None
        self.names = (Path(reg_path).name, Path(clf_path).name)

    @staticmethod
    def _X(frame: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
        missing = [c for c in cols if c not in frame.columns]
        if missing:
            raise IntegrationError(f"Модели нужны признаки, которых нет в витрине: {missing}")
        return frame[cols]

    @staticmethod
    def _positive_proba(model, X) -> float:
        proba = model.predict_proba(X)[0]
        classes = list(model.classes_)
        return float(proba[classes.index(1)]) if 1 in classes else float(proba[-1])

    def predict(self, f: dict) -> Tuple[float, float, Optional[float]]:
        frame = f["_frame"]
        chl = float(self.reg.predict(self._X(frame, self.reg_cols))[0])
        prob = self._positive_proba(self.clf, self._X(frame, self.clf_cols))
        att = (self._positive_proba(self.attention, self._X(frame, self.attention_cols))
               if self.attention is not None else None)
        return (round(max(chl, 0.0), 2), round(min(max(prob, 0.0), 1.0), 3),
                None if att is None else round(min(max(att, 0.0), 1.0), 3))

    def predict_many(self, frame: pd.DataFrame) -> List[Tuple[float, float, Optional[float]]]:
        """Векторный прогноз для истории: один вызов модели на весь ряд (быстро)."""
        if frame.empty:
            return []
        chl = self.reg.predict(self._X(frame, self.reg_cols))
        proba = self.clf.predict_proba(self._X(frame, self.clf_cols))
        classes = list(self.clf.classes_)
        idx = classes.index(1) if 1 in classes else -1
        att = None
        if self.attention is not None:
            pa = self.attention.predict_proba(self._X(frame, self.attention_cols))
            cls_a = list(self.attention.classes_)
            att = pa[:, cls_a.index(1) if 1 in cls_a else -1]
        return [(round(max(float(c), 0.0), 2), round(min(max(float(p), 0.0), 1.0), 3),
                 None if att is None else round(min(max(float(a), 0.0), 1.0), 3))
                for c, p, a in zip(chl, proba[:, idx], att if att is not None else [None] * len(chl))]


# ------------------------------------------------------------------ Часть 3
SYNONYMS = {
    "water_body_id": ["water_body_id", "wb_id", "lake_id", "water_body", "waterbody_id", "reservoir_id"],
    "water_body_name": ["water_body_name", "wb_name", "lake_name", "name"],
    "date": ["date", "forecast_date", "obs_date", "as_of", "timestamp"],
    "chl": ["chl_forecast", "chl_pred", "chl_a_pred", "chl_hat", "predicted_chl", "chl_a_forecast", "chl_a",
            "chl", "forecast_chl", "chla_pred", "c_hat", "chl_a_hat", "chl_a_pred_10d"],
    "prob": ["bloom_prob", "bloom_probability", "p_bloom", "prob", "probability", "p_hat", "bloom_proba",
             "pred_bloom_prob", "bloom_prob_pred", "p_bloom_pred"],
    "attention_prob": ["attention_prob", "attention_probability", "p_attention", "att_prob"],
    "temp": ["temp_mean_14d", "t14", "temp_14d", "temperature_14d", "temp_mean_14"],
    "calm": ["calm_fraction_7d", "calm_frac_7d", "calm_fraction", "calm_share_7d", "calm_7d"],
    "lake_meta": ["lake_meta", "water_body_meta", "meta"],
}
ENGINE_METHODS = ("assess", "evaluate", "evaluate_alert", "classify", "predict_alert", "get_verdict", "run")

ALERT_LEVEL_MAP = {
    "GREEN": "GREEN",
    "BELOW_THRESHOLD": "GREEN",
    "OK": "GREEN",
    "NORMAL": "GREEN",
    "RED": "RED",
    "WARNING": "RED",
    "ALERT": "RED",
}


def _get(o, keys):
    for k in keys:
        v = o.get(k) if isinstance(o, dict) else getattr(o, k, None)
        if v is not None:
            return v
    return None


def _as_list(v) -> List[str]:
    if v is None:
        return []
    return [str(v)] if isinstance(v, str) else [str(x) for x in v]


def _normalize_decade_start(d: date) -> date:
    """Приводит произвольную дату (в т.ч. лабораторной пробы) к началу календарной декады (1/11/21),
    как того требует Часть 3 (graph_cli.parse_period_start / insert_record)."""
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    if d.day <= 10:
        return d.replace(day=1)
    if d.day <= 20:
        return d.replace(day=11)
    return d.replace(day=21)


def to_verdict(o) -> AlertVerdict:
    raw_level = _get(o, ["alert_level", "alertStatus", "alert_status", "level", "alert", "status"])
    norm = str(getattr(raw_level, "name", raw_level)).strip().upper().split(".")[-1]
    level = ALERT_LEVEL_MAP.get(norm)
    if level is None:
        raise IntegrationError(f"Уровень тревоги '{norm}' не из GREEN/RED (warning/below_threshold; объект: {o!r:.200})")
    tsi = _num(_get(o, ["trophic_index", "tsi", "carlson_index", "tsi_carson", "tsi_value"]))
    return AlertVerdict(
        alert_level=level, trophic_index=round(tsi, 1) if tsi is not None else 0.0,
        trophic_class=str(_get(o, ["trophic_class", "trophic_state", "trophic_level", "trophic_category"]) or "н/д"),
        reasons=_as_list(_get(o, ["reasons", "causes", "explanations", "reason"])),
        recommendations=_as_list(_get(o, ["recommendations", "measures", "actions", "advice"])))


class RealGraphEngine:
    """Обёртка над WaterKnowledgeGraphEngine из Части 3 (пользовательский класс)."""

    threshold_ug_l: float = 25.0

    def __init__(self, module: str, ontology_path: Optional[Path] = None):
        mod = importlib.import_module(module)
        cls = getattr(mod, "WaterKnowledgeGraphEngine", None)
        if cls is None:
            raise IntegrationError(f"В модуле {module} нет класса WaterKnowledgeGraphEngine")
        try:
            self.engine = cls(str(ontology_path)) if ontology_path else cls()
        except TypeError:
            self.engine = cls()
        self.method = next((m for m in ENGINE_METHODS if callable(getattr(self.engine, m, None))), None)
        if self.method is None:
            pub = [m for m in dir(self.engine) if not m.startswith("_")]
            raise IntegrationError(f"Не нашёл метод оценки у движка. Публичные методы: {pub}")

    def describe(self) -> str:
        return f"{self.method}{inspect.signature(getattr(self.engine, self.method))}"

    def assess(
        self,
        wb_id,
        wb_name,
        d,
        chl,
        prob,
        temp14,
        calm7,
        *,
        attention_prob=None,
        lake_meta=None,
        registry=None,
    ) -> AlertVerdict:
        fn = getattr(self.engine, self.method)
        values = dict(
            water_body_id=wb_id, water_body_name=wb_name, date=d, chl=chl, prob=prob,
            attention_prob=attention_prob, temp=temp14, calm=calm7, lake_meta=lake_meta,
        )
        kwargs = {}
        for pname, p in inspect.signature(fn).parameters.items():
            key = next((k for k, syn in SYNONYMS.items() if pname in syn), None)
            if key:
                kwargs[pname] = values[key]
            elif p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                raise IntegrationError(f"Не знаю, что передать в параметр '{pname}': {self.describe()}")
        return to_verdict(fn(**kwargs))


class GraphCliEngine:
    """Адаптер к реальной Части 3 (src/knowledge_graph/graph_cli.py + data/knowledge_graph/graph.ttl + Oxigraph).

    Выполняет настоящие SPARQL-правила Части 3 (CREATE_ALERTS и HISTORY) поверх RDF-графа:
      - alertStatus == "warning"         -> GREEN/RED: "RED"   (chl >= eutro:thresholdUgL)
      - alertStatus == "below_threshold" -> GREEN/RED: "GREEN" (chl <  eutro:thresholdUgL)
    Если задан OXIGRAPH_URL и Oxigraph доступен — сначала проверяет сохранённый прогноз в Oxigraph,
    иначе выполняет те же SPARQL-правила локально в памяти через rdflib без записи в graph.ttl на диске.

    Расширенный контекст (ООПТ, запрет хим. альгицидов, каскад, глубина/стратификация, порог внимания
    15 мкг/л, метеофакторы, вероятности, индекс Карлсона TSI и рекомендации) сохраняется через
    модульные заглушки enrich_verdict_with_stubs() до переноса этих правил в онтологию Части 3.
    """

    def __init__(
        self,
        module: str = "src.knowledge_graph.graph_cli",
        ontology_path: Optional[Path] = None,
        oxigraph_url: Optional[str] = None,
    ):
        try:
            from rdflib import Graph, Literal, Namespace, RDF, URIRef, XSD
            from rdflib.plugins.sparql import prepareQuery, prepareUpdate
        except ImportError as exc:
            raise IntegrationError("Для подключения Части 3 (graph_cli) требуется пакет rdflib") from exc

        self._Graph = Graph
        self._Literal = Literal
        self._RDF = RDF
        self._URIRef = URIRef
        self._XSD = XSD

        self.mod = importlib.import_module(module)
        required = ("load_graph", "CREATE_ALERTS", "HISTORY", "PREFIXES", "BASE_IRI", "ALERT_RULE_ID", "SEED_RULE", "read_history")
        missing = [name for name in required if not hasattr(self.mod, name)]
        if missing:
            raise IntegrationError(f"В модуле {module} отсутствуют атрибуты Части 3: {missing}")

        self.ontology_path = Path(ontology_path) if ontology_path else None
        self.oxigraph_url = oxigraph_url.rstrip("/") if oxigraph_url else None
        self._eutro = Namespace(self.mod.BASE_IRI)
        self._rule_uri = self._eutro[self.mod.ALERT_RULE_ID]

        try:
            if self.ontology_path is not None and self.ontology_path.is_file():
                base_graph = self.mod.load_graph(self.ontology_path)
            else:
                base_graph = Graph()
                base_graph.bind("eutro", self._eutro, replace=True)
        except Exception as exc:  # noqa: BLE001
            raise IntegrationError(f"Не удалось прочитать RDF-граф Части 3 ({self.ontology_path}): {exc}") from exc

        # Если правило порога ещё не задано в графе — сеем дефолт Части 3 (25 мкг/л), не перетирая кастомный порог.
        if not list(base_graph.objects(self._rule_uri, self._eutro.thresholdUgL)):
            base_graph.update(self.mod.PREFIXES + self.mod.SEED_RULE)

        # Отделяем статические триплы (правила, WaterBody, будущие свойства ООПТ/каскада) от ранее
        # записанных Forecast/Observation/Alert/Period, чтобы вызовы assess() не конфликтовали друг с другом.
        dynamic_types = (self._eutro.Forecast, self._eutro.Observation, self._eutro.Alert)
        dynamic_subjects = {
            s for dtype in dynamic_types for s in base_graph.subjects(RDF.type, dtype)
        }
        period_subjects = set(base_graph.subjects(self._eutro.start, None))
        skip_subjects = dynamic_subjects | period_subjects
        self._base_triples = [
            tr for tr in base_graph
            if tr[0] not in skip_subjects and tr[2] not in dynamic_subjects
        ]

        # Считываем порог непосредственно из RDF-графа (eutro:chlorophyll-alert eutro:thresholdUgL ?t)
        thresholds = [
            _num(obj) for obj in base_graph.objects(self._rule_uri, self._eutro.thresholdUgL)
        ]
        valid_thr = [t for t in thresholds if t is not None and t > 0]
        self.threshold_ug_l = float(valid_thr[0]) if valid_thr else 25.0

        # Индекс зарегистрированных в graph.ttl водоёмов (waterBodyId -> URIRef)
        self._registered_waters: Dict[str, object] = {}
        for subj, obj in base_graph.subject_objects(self._eutro.waterBodyId):
            self._registered_waters[str(obj)] = subj

        # Предкомпилируем точные SPARQL-правила Части 3 (ускоряет вызов с ~160 мс до ~3.5 мс)
        self._create_alerts = prepareUpdate(self.mod.PREFIXES + self.mod.CREATE_ALERTS)
        self._history_query = prepareQuery(
            self.mod.PREFIXES + self.mod.HISTORY.substitute(period_values="", filters="")
        )

    def describe(self) -> str:
        src = self.ontology_path.name if self.ontology_path else "memory"
        oxi = f", Oxigraph={self.oxigraph_url}" if self.oxigraph_url else ""
        return f"{src}, порог {self.threshold_ug_l:g} мкг/л, 2 уровня GREEN/RED{oxi}"

    def _try_oxigraph(self, wb_id: str, period_start: date, chl: float) -> Optional[Tuple[str, float]]:
        if not self.oxigraph_url:
            return None
        try:
            rows = self.mod.read_history(None, wb_id, endpoint=self.oxigraph_url, period_start=period_start)
        except (OSError, ValueError):
            return None
        for row in rows:
            pred = _num(row.get("predictedUgL"))
            status = row.get("alertStatus")
            if pred is not None and abs(pred - float(chl)) <= 0.1 and status in ("warning", "below_threshold"):
                thr = _num(row.get("thresholdUgL")) or self.threshold_ug_l
                return str(status), float(thr)
        return None

    def _evaluate_rdflib(self, wb_id: str, wb_name: Optional[str], period_start: date, chl: float) -> Tuple[str, float]:
        Graph, Literal, RDF, URIRef, XSD = self._Graph, self._Literal, self._RDF, self._URIRef, self._XSD
        eutro = self._eutro

        work = Graph()
        for triple in self._base_triples:
            work.add(triple)

        water_segment = quote(str(wb_id), safe="")
        water = self._registered_waters.get(str(wb_id)) or URIRef(f"{self.mod.BASE_IRI}water/{water_segment}")
        last_day = monthrange(period_start.year, period_start.month)[1]
        period_end = period_start.replace(day=last_day if period_start.day == 21 else period_start.day + 9)
        period = URIRef(f"{self.mod.BASE_IRI}period/{period_start.isoformat()}")
        record = URIRef(f"{self.mod.BASE_IRI}forecast/{water_segment}/{period_start.isoformat()}")

        work.add((water, RDF.type, eutro.WaterBody))
        work.add((water, eutro.waterBodyId, Literal(str(wb_id))))
        if wb_name and not list(work.objects(water, eutro.name)):
            work.add((water, eutro.name, Literal(str(wb_name))))
        work.add((period, eutro.start, Literal(period_start.isoformat(), datatype=XSD.date)))
        work.add((period, eutro.end, Literal(period_end.isoformat(), datatype=XSD.date)))
        work.add((record, RDF.type, eutro.Forecast))
        work.add((record, eutro.waterBody, water))
        work.add((record, eutro.period, period))
        work.add((record, eutro.chlorophyllUgL, Literal(float(chl))))
        work.add((record, eutro.usesRule, self._rule_uri))

        work.update(self._create_alerts)
        rows = list(work.query(self._history_query))
        for row in rows:
            if str(row.waterBodyId) == str(wb_id) and str(row.periodStart) == period_start.isoformat():
                status = str(row.alertStatus) if row.alertStatus is not None else "below_threshold"
                thr = _num(row.thresholdUgL) or self.threshold_ug_l
                return status, float(thr)
        raise IntegrationError(f"SPARQL-запрос Части 3 (HISTORY) не вернул запись для {wb_id}/{period_start}")

    def assess(
        self,
        wb_id: str,
        wb_name: str,
        d: date,
        chl: float,
        prob: Optional[float],
        temp14: Optional[float],
        calm7: Optional[float],
        *,
        attention_prob: Optional[float] = None,
        lake_meta: Optional[dict] = None,
        registry: Optional[Dict[str, dict]] = None,
    ) -> AlertVerdict:
        period_start = _normalize_decade_start(d)
        oxi_hit = self._try_oxigraph(wb_id, period_start, chl)
        if oxi_hit is not None:
            alert_status, threshold = oxi_hit
            backend = "Oxigraph"
        else:
            alert_status, threshold = self._evaluate_rdflib(wb_id, wb_name, period_start, chl)
            backend = f"rdflib/{self.ontology_path.name}" if self.ontology_path else "rdflib"

        level = ALERT_LEVEL_MAP.get(alert_status.upper())
        if level is None:
            raise IntegrationError(f"Неожиданный alertStatus из Части 3: {alert_status!r}")

        cmp_op = "≥" if level == "RED" else "<"
        base_reasons = [
            f"Граф знаний (Часть 3, {backend}, правило {self.mod.ALERT_RULE_ID}): "
            f"прогноз Chl-a {float(chl):.1f} мкг/л {cmp_op} порога {threshold:g} мкг/л → {alert_status} ({level})"
        ]
        return enrich_verdict_with_stubs(
            level,
            float(chl),
            threshold_ug_l=threshold,
            base_reasons=base_reasons,
            bloom_prob=prob,
            attention_prob=attention_prob,
            temp14=temp14,
            calm7=calm7,
            lake_meta=lake_meta,
            registry=registry,
        )


def load_graph_engine(
    module: str,
    ontology_path: Optional[Path] = None,
    oxigraph_url: Optional[str] = None,
):
    """Автоматически выбирает адаптер Части 3: класс WaterKnowledgeGraphEngine или модуль graph_cli."""
    mod = importlib.import_module(module)
    if getattr(mod, "WaterKnowledgeGraphEngine", None) is not None:
        return RealGraphEngine(module, ontology_path)
    if hasattr(mod, "CREATE_ALERTS") and hasattr(mod, "HISTORY"):
        return GraphCliEngine(module, ontology_path, oxigraph_url)
    raise IntegrationError(
        f"В модуле {module} нет ни класса WaterKnowledgeGraphEngine, ни SPARQL-правил graph_cli (CREATE_ALERTS/HISTORY)"
    )


def load_metrics(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_quality(path: Optional[Path]) -> Optional[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None
