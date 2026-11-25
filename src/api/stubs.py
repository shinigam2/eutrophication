"""
ЗАГЛУШКИ вместо Частей 1-3. Интерфейсы должны совпасть с настоящими компонентами,
тогда в pipeline.build_components достаточно подставить реальные объекты.
Значения водоёмов (ID, bbox) — ПРЕДПОЛОЖИТЕЛЬНЫЕ, брать из water_bodies.yaml (Часть 1).
Заглушки включаются только когда реальных файлов нет; панель показывает жёлтый баннер.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

# ---------- Часть 1: реестр водоёмов и витрина признаков ----------
WATER_BODIES: Dict[str, dict] = {
    "zaslavskoe": dict(
        id="zaslavskoe", name="Заславское водохранилище (Минское море)", type="reservoir",
        lat=54.00, lon=27.27,
        bbox=dict(min_lat=53.97, min_lon=27.22, max_lat=54.03, max_lon=27.33),
        area_km2=31.0, mean_depth_m=3.5, max_depth_m=8.0, volume_mln_m3=108.0,
        watershed_km2=562.0, watershed_ratio=18.1, protected_area=None,
        lab_base=3.0, lab_amp=12.0, base=3.0, amp=42.0),
    "naroch": dict(
        id="naroch", name="Озеро Нарочь", type="lake",
        lat=54.85, lon=26.77,
        bbox=dict(min_lat=54.77, min_lon=26.68, max_lat=54.92, max_lon=26.84),
        area_km2=79.6, mean_depth_m=8.9, max_depth_m=24.8, volume_mln_m3=710.0,
        watershed_km2=199.0, watershed_ratio=2.5,
        protected_area="ГПУ «Национальный парк „Нарочанский“»",
        lab_base=1.0, lab_amp=3.0, base=2.0, amp=15.0),
}

DECADE_DAYS = (1, 11, 21)


def _polygon(b: dict, n: int = 14) -> dict:
    """Условный контур внутри BBox (эллипс) — ТОЛЬКО для заглушки, не настоящая береговая линия."""
    cy, cx = (b["min_lat"] + b["max_lat"]) / 2, (b["min_lon"] + b["max_lon"]) / 2
    ry = (b["max_lat"] - b["min_lat"]) * 0.45
    rx = (b["max_lon"] - b["min_lon"]) * 0.45
    ring = [[round(cx + rx * math.cos(2 * math.pi * k / n), 5), round(cy + ry * math.sin(2 * math.pi * k / n), 5)] for k in range(n)]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


for _wb in WATER_BODIES.values():
    _wb["geometry"] = _polygon(_wb["bbox"])
DATA_START, DATA_END = date(2020, 1, 1), date(2026, 11, 21)
DEFAULT_DATE = date(2026, 8, 1)


def snap_to_decade(d: date) -> date:
    """Данные декадные (1, 11, 21) — берём ближайшую декаду."""
    cands = []
    for dm in (-1, 0, 1):
        y, m = d.year, d.month + dm
        if m == 0:
            y, m = y - 1, 12
        elif m == 13:
            y, m = y + 1, 1
        cands += [date(y, m, day) for day in DECADE_DAYS]
    return min(cands, key=lambda c: abs((c - d).days))


def _season(doy: int, base: float, amp: float) -> float:
    return base + amp * math.exp(-(((doy - 215) / 45.0) ** 2))


def decade_dates(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.day in DECADE_DAYS:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def _status(used: date) -> str:
    """Детерминированные статусы для демонстрации всех веток."""
    if used.month in (12, 1, 2, 3):
        return "ice_cover"
    if (used.month, used.day) == (6, 21):
        return "cloudy_missing"
    if (used.month, used.day) == (9, 1):
        return "interpolated_1step"
    return "valid"


class StubDataProvider:
    """Имитация чтения features_satellite.parquet / features_lab.parquet."""

    def __init__(self, mode: str = "satellite"):
        self.mode = mode
        self.columns = (["water_body_id", "date", "obs_status", "chl_a_est_current", "temp_mean_14d",
                         "calm_fraction_7d"] if mode == "satellite"
                        else ["water_body_id", "sample_date", "chl_a_lab_ug_l", "temp_mean_14d",
                              "calm_fraction_7d"])
        self.chl_col = "chl_a_est_current" if mode == "satellite" else "chl_a_lab_ug_l"

    def available_dates(self, wb_id: str):
        return decade_dates(DATA_START, DATA_END) if wb_id in WATER_BODIES else []

    def date_range(self, wb_id: str):
        return DATA_START, DATA_END

    def supports(self, wb_id: str) -> bool:
        return wb_id in WATER_BODIES

    def get_features(self, wb_id: str, d: date) -> Optional[dict]:
        wb = WATER_BODIES.get(wb_id)
        used = snap_to_decade(d)
        if wb is None or not (DATA_START <= used <= DATA_END):
            return None
        doy = used.timetuple().tm_yday
        rnd = random.Random(f"{wb_id}-{used.isoformat()}")
        temp14 = 10 + 14 * math.sin(2 * math.pi * (doy - 110) / 365) + rnd.uniform(-1.5, 1.5)
        calm7 = min(1.0, max(0.0, 0.2 + 0.25 * (temp14 > 18) + rnd.uniform(-0.1, 0.15)))
        if self.mode == "satellite":
            chl_now = max(0.3, _season(doy, wb["base"], wb["amp"]) * rnd.uniform(0.85, 1.15))
        else:
            chl_now = max(0.2, _season(doy, wb["lab_base"], wb["lab_amp"]) * rnd.uniform(0.85, 1.15))
        status = _status(used)
        return dict(date_used=used, obs_status=status, is_interpolated=status == "interpolated_1step",
                    chl_a_current=None if status in ("ice_cover", "cloudy_missing") else round(chl_now, 2),
                    temp_mean_14d=round(temp14, 1), calm_fraction_7d=round(calm7, 2),
                    scenes_in_decade=0 if status in ("ice_cover", "cloudy_missing") else 2,
                    valid_water_pixels=0 if status in ("ice_cover", "cloudy_missing") else 1200,
                    source="Sentinel-2 L2A (ESA/Copernicus)" if self.mode == "satellite" else "Лабораторная проба (демо)",
                    scene_id=None, source_note=None,
                    _doy=doy, _wb=wb_id, _mode=self.mode)

    def row_meta(self, row) -> dict:
        """Метаданные строки — тот же интерфейс, что у RealDataProvider."""
        def g(key, default=None):
            v = row.get(key, default)
            return None if v is None or (isinstance(v, float) and math.isnan(v)) else v
        status = g("obs_status")
        return dict(date=row["date"], obs_status=status,
                    is_interpolated=bool(g("is_interpolated", status == "interpolated_1step")),
                    chl_a_current=g(self.chl_col),
                    temp_mean_14d=g("temp_mean_14d"), calm_fraction_7d=g("calm_fraction_7d"),
                    scenes_in_decade=g("scenes_in_decade"), valid_water_pixels=g("valid_water_pixels"),
                    scene_id=g("scene_id"), source_note=g("source_note"),
                    source=g("source") or ("Sentinel-2 L2A (ESA/Copernicus)" if self.mode == "satellite"
                                           else "Лабораторная проба (демо)"))

    def history(self, wb_id: str, end: date, limit: int = 120) -> List[dict]:
        """Декадный ряд до end включительно (для графика)."""
        if wb_id not in WATER_BODIES:
            return []
        end = snap_to_decade(end)
        dates = [d for d in decade_dates(DATA_START, end)]
        return [self.get_features(wb_id, d) for d in dates[-limit:]]

    def history_frame(self, wb_id: str, end: date, limit: int = 120):
        """Тот же ряд, но таблицей — единый интерфейс с RealDataProvider."""
        import pandas as pd
        if wb_id not in WATER_BODIES:
            return pd.DataFrame()
        end = snap_to_decade(end)
        rows = []
        for f in (self.get_features(wb_id, d) for d in decade_dates(DATA_START, end)):
            if f is None:
                continue
            row = dict(water_body_id=wb_id, date=f["date_used"], obs_status=f["obs_status"],
                       is_interpolated=f["is_interpolated"], temp_mean_14d=f["temp_mean_14d"],
                       calm_fraction_7d=f["calm_fraction_7d"], scenes_in_decade=f["scenes_in_decade"],
                       valid_water_pixels=f["valid_water_pixels"], source=f["source"],
                       scene_id=None, source_note=None, _wb=wb_id, _doy=f["_doy"],
                       chl_a_current=f["chl_a_current"])
            row[self.chl_col] = f["chl_a_current"]
            rows.append(row)
        return pd.DataFrame(rows[-limit:]).reset_index(drop=True)


# ---------- Часть 2: модели ----------
class StubPredictor:
    """Имитация LGBMRegressor + LGBMClassifier."""

    def __init__(self, mode: str = "satellite"):
        self.mode = mode

    def predict(self, f: dict) -> Tuple[float, float]:
        wb = WATER_BODIES[f["_wb"]]
        key_base, key_amp = ("base", "amp") if self.mode == "satellite" else ("lab_base", "lab_amp")
        now_season = max(_season(f["_doy"], wb[key_base], wb[key_amp]), 0.1)
        chl = _season(f["_doy"] + 10, wb[key_base], wb[key_amp]) * ((f["chl_a_current"] or now_season) / now_season)
        chl = round(max(0.2, chl), 1)
        threshold = 25 if self.mode == "satellite" else 10
        prob = round(1 / (1 + math.exp(-(chl - threshold) / 4)), 2)
        return chl, prob

    def predict_frame(self, frame) -> List[Tuple[float, float]]:
        out = []
        for _, row in frame.iterrows():
            out.append(self.predict(dict(row)))
        return out

    def predict_many(self, frame) -> List[Tuple[float, float]]:
        """Пакетный прогноз для истории — как у RealPredictor."""
        return self.predict_frame(frame)


# ---------- Часть 3: граф знаний и правила ----------
@dataclass
class AlertVerdict:
    alert_level: str
    trophic_index: float
    trophic_class: str
    reasons: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


def carlson_tsi(chl: float) -> float:
    return round(9.81 * math.log(max(chl, 0.1)) + 30.6, 1)


def trophic_class(tsi: float) -> str:
    if tsi < 40: return "олиготрофный"
    if tsi < 50: return "мезотрофный"
    if tsi < 70: return "эвтрофный"
    return "гиперэвтрофный"


class StubGraphEngine:
    """Имитация WaterKnowledgeGraphEngine.assess(...)."""

    def assess(self, wb_id: str, wb_name: str, d: date, chl: float, prob: float,
               temp14: float, calm7: float) -> AlertVerdict:
        reasons, recs = [], []
        hot_calm = temp14 >= 20.0 and calm7 >= 0.30
        if chl >= 25 or prob >= 0.5:
            level = "RED"
            if chl >= 25: reasons.append(f"Прогноз хлорофилла-a {chl} мкг/л превышает порог 25 мкг/л")
            if prob >= 0.5: reasons.append(f"Вероятность цветения {prob:.0%} (порог 50%)")
        elif 15 <= chl < 25 or hot_calm:
            level = "YELLOW"
            if 15 <= chl < 25: reasons.append(f"Прогноз хлорофилла-a {chl} мкг/л в диапазоне 15-25 мкг/л")
            if hot_calm: reasons.append(f"Тепло-штилевой режим: T14={temp14} °C, доля штиля {calm7:.0%}")
        else:
            level = "GREEN"
            reasons.append("Показатели в пределах нормы")
        if level == "RED":
            recs = ["Усилить отбор проб воды", "Проверить цианотоксины", "Уведомить Минприроды",
                    "Рассмотреть ограничение купания"]
        elif level == "YELLOW":
            recs = ["Усилить наблюдение", "Запланировать внеочередной отбор проб"]
        else:
            recs = ["Плановый мониторинг"]
        tsi = carlson_tsi(chl)
        return AlertVerdict(level, tsi, trophic_class(tsi), reasons, recs)


STUB_METRICS = {"note": "заглушка", "regression": {"rmse": None, "mae": None, "r2": None},
                "classification": {"f1": None, "roc_auc": None}}
