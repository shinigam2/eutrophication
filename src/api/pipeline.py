"""Общий конвейер Части 4: срез признаков -> модели -> граф знаний -> словарь ответа.
Используется API (main.py), скриптами проверки и тестами, поэтому здесь нет FastAPI.

build_components(): для каждой части, если её файлы найдены, подключается РЕАЛЬНЫЙ компонент
(ошибка в найденном файле не скрывается!), если файлов нет — заглушка из stubs.py.
refresh_part1(): перечитывает реестр, витрины и полигоны при изменении файлов (добавлено озеро).
Поддерживаются оба режима DATA_CONTRACT.md: satellite (features_satellite) и lab (features_lab).
"""
import importlib.util
import os
import sys
import threading
from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.api import adapters, config, stubs

HORIZON_DAYS = 10
MODES = config.MODES
PART_NAMES = {"part1": "Часть 1 (данные)", "part2": "Часть 2 (модели)", "part3": "Часть 3 (граф знаний)"}

# Декады без надёжного наблюдения: прогноз не строится, значения не придумываются.
NO_OBSERVATION = {"ice_cover", "cloudy_missing", "insufficient_pixels"}
NO_OBSERVATION_TEXT = {
    "ice_cover": "Ледяной покров: наблюдения нет, прогноз не строится",
    "cloudy_missing": "Облачность: чистых наблюдений за декаду нет, прогноз не строится",
    "insufficient_pixels": "Недостаточно чистых пикселей воды: прогноз не строится",
}
DEMO_SOURCE = "Демонстрационные данные (заглушка): реальные Части 1–3 не подключены"
DEMO_NOTE = "Значения не являются реальными измерениями и результатом обученных моделей."


class ModeUnavailable(adapters.ModeUnavailable):
    """Режим недоступен для этого водоёма/проекта."""


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _missing(what: str) -> str:
    if config.strict():
        raise FileNotFoundError(f"WATER_STRICT=1, но не найдено: {what}")
    return f"заглушка (нет: {what})"


def _is_real(c: dict, part: str) -> bool:
    return str(c["status"].get(part, "")).startswith("реальная")


def _load_part1():
    """Реестр водоёмов (+ полигоны и декады) и провайдеры данных для каждого режима. Ошибки не скрываются."""
    reg_path = config.find_registry()
    feat_paths = {mode: config.find_features(mode) for mode in MODES}
    if not reg_path or not any(feat_paths.values()):
        raise FileNotFoundError("не найдены water_bodies.yaml и/или витрины признаков")
    registry = adapters.load_registry(reg_path)
    providers: Dict[str, adapters.RealDataProvider] = {}
    paths: Dict[str, Optional[str]] = {}
    for mode, feat_path in feat_paths.items():
        paths[mode] = feat_path.name if feat_path else None
        if feat_path:
            chl_override = os.environ.get("WATER_CHL_COLUMN_LAB" if mode == "lab" else "WATER_CHL_COLUMN")
            providers[mode] = adapters.RealDataProvider(feat_path, registry, chl_override, mode)
    geo_path = config.find_geometry()
    if geo_path:
        adapters.hydrate_registry_geometry(registry, geo_path)
    decadal_path = config.find_decadal()
    decadal_df = adapters.load_decadal_frame(decadal_path, registry) if decadal_path else None
    return registry, providers, paths, decadal_df


def _load_predictors() -> Dict[str, dict]:
    """Модели Части 2 по режимам (режим без моделей просто не попадает в словарь)."""
    out: Dict[str, dict] = {}
    for mode in MODES:
        models = config.find_models(mode)
        if models:
            out[mode] = dict(predictor=adapters.RealPredictor(*models, config.find_attention_model(mode)),
                             names=tuple(p.name for p in models), real=True)
    return out


def build_components() -> dict:
    c, status = {"lock": threading.Lock(), "reload_error": None, "timeline_cache": {}}, {}

    # Часть 1: реестр водоёмов + полигоны + витрины признаков
    try:
        c["registry"], c["providers"], c["paths"], c["decadal_df"] = _load_part1()
        status["part1"] = "реальная: " + ", ".join(
            x for x in [config.find_registry().name] + [v for v in c["paths"].values() if v] if x)
    except FileNotFoundError:
        c["registry"], c["providers"], c["paths"], c["decadal_df"] = stubs.WATER_BODIES, \
            {mode: stubs.StubDataProvider(mode) for mode in MODES}, {mode: None for mode in MODES}, None
        status["part1"] = _missing("water_bodies.yaml и витрина признаков (features_satellite / features_lab)")
    c["provider"] = c["providers"].get("satellite") or next(iter(c["providers"].values()), None)
    c["fp1"] = config.part1_fingerprint()
    c["status"] = status

    # Часть 2: модели (нужны реальные данные режима, чтобы было на чём предсказывать)
    real_pred = _load_predictors() if _is_real(c, "part1") else {}
    c["predictors"] = {}
    for mode in MODES:
        if mode in real_pred and mode in c["providers"]:
            c["predictors"][mode] = real_pred[mode]
        else:
            c["predictors"][mode] = dict(predictor=stubs.StubPredictor(mode), names=("заглушка",), real=False)
    sat = c["predictors"]["satellite"]
    if sat["real"]:
        status["part2"] = "реальная: " + ", ".join(sat["names"])
    elif real_pred:
        status["part2"] = "заглушка (модели есть, но нет реальной витрины нужного режима)"
    else:
        status["part2"] = _missing("модели .joblib")
    c["predictor"] = sat["predictor"]

    # метрики и отчёт качества
    metrics_path = config.find_metrics()
    c["metrics"] = adapters.load_metrics(metrics_path) if metrics_path else stubs.STUB_METRICS
    c["metrics_path"] = metrics_path
    c["quality"] = adapters.load_quality(config.find_quality())

    # Часть 3: граф знаний (WaterKnowledgeGraphEngine или реальный graph_cli + graph.ttl / Oxigraph)
    ontology_path = config.find_ontology()
    oxi_url = config.oxigraph_url()
    explicit_engine = os.environ.get("WATER_ENGINE_MODULE")
    if explicit_engine and _module_available(explicit_engine):
        c["engine"] = adapters.load_graph_engine(explicit_engine, ontology_path, oxi_url)
        status["part3"] = f"реальная: {explicit_engine} ({c['engine'].describe()})"
    elif _module_available(config.ENGINE_MODULE):
        c["engine"] = adapters.RealGraphEngine(config.ENGINE_MODULE, ontology_path)
        status["part3"] = f"реальная: {config.ENGINE_MODULE}.{c['engine'].describe()}"
    elif _module_available(config.GRAPH_CLI_MODULE) and (ontology_path is not None or oxi_url is not None):
        c["engine"] = adapters.GraphCliEngine(config.GRAPH_CLI_MODULE, ontology_path, oxi_url)
        status["part3"] = f"реальная: {config.GRAPH_CLI_MODULE} ({c['engine'].describe()}; доп. контекст — заглушки)"
    else:
        c["engine"] = stubs.StubGraphEngine()
        status["part3"] = _missing(f"{config.GRAPH_CLI_MODULE} + data/knowledge_graph/graph.ttl")

    c["status"] = status
    c["stub_parts"] = [PART_NAMES[k] for k in PART_NAMES if str(status[k]).startswith("заглушка")]
    return c


def refresh_part1(c: dict) -> None:
    """Если файлы реестра/витрин/полигонов изменились (добавлено озеро), перечитывает их без перезапуска.
    Модели и граф знаний не перезагружаются. Ошибка чтения не ломает работу: остаётся прежний набор,
    причина попадает в c['reload_error'] (её показывает маршрут «/»)."""
    if not _is_real(c, "part1"):
        return
    fp = config.part1_fingerprint()
    if fp == c["fp1"]:
        return
    with c["lock"]:
        if fp == c["fp1"]:
            return
        c["fp1"] = fp                      # не повторяем попытку на каждом запросе, пока файлы не изменятся
        try:
            c["registry"], c["providers"], c["paths"], c["decadal_df"] = _load_part1()
            c["provider"] = c["providers"].get("satellite") or next(iter(c["providers"].values()), None)
            c["timeline_cache"] = {}
            c["reload_error"] = None
        except Exception as e:             # noqa: BLE001 - сообщаем причину, прежние данные остаются
            c["reload_error"] = f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------ вспомогательное
def modes_for(c: dict, wb_id: str) -> List[str]:
    """Режимы, в которых для озера есть данные (приоритетно — с моделью)."""
    out = []
    for mode in MODES:
        prov = c["providers"].get(mode)
        if prov is not None and prov.supports(wb_id) and c["predictors"][mode]["real"]:
            out.append(mode)
    if not out:
        out = [m for m in MODES if c["providers"].get(m) is not None and c["providers"][m].supports(wb_id)]
    return out or ["satellite"]


def _walk_numbers(obj, path: Tuple[str, ...] = ()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_numbers(v, path + (str(k),))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield path, float(obj)


def model_mae(c: dict, mode: str) -> Optional[float]:
    """±MAE регрессии из model_metrics.json (структура произвольная — ищем по имени ключа)."""
    hits = [(p, v) for p, v in _walk_numbers(c.get("metrics") or {}) if p and p[-1].lower() == "mae" and v > 0]
    if not hits:
        return None
    mode_words = ("lab", "laboratory", "пробир") if mode == "lab" else ("sat", "space", "спутник")
    weighted = [h for h in hits if any(w in " ".join(h[0]).lower() for w in mode_words)]
    chl_like = [h for h in (weighted or hits) if any(w in " ".join(h[0]).lower() for w in ("chl", "chlor", "10d", "regress"))]
    return (chl_like or weighted or hits)[0][1]


def _base_sources(c: dict, mode: str, f: dict) -> List[str]:
    if not _is_real(c, "part1"):
        return [DEMO_SOURCE]
    src = [f"{f.get('source') or ('Sentinel-2 L2A (ESA/Copernicus)' if mode == 'satellite' else 'Лабораторная проба')}"]
    if f.get("temp_mean_14d") is not None or f.get("calm_fraction_7d") is not None:
        src.append("NASA POWER — суточная погода, окна 7/14/21 день")
    if c["predictors"][mode]["real"]:
        src.append("Модели Части 2: " + ", ".join(c["predictors"][mode]["names"]))
    if _is_real(c, "part3"):
        if isinstance(c.get("engine"), adapters.GraphCliEngine):
            src.append("Граф знаний RDF/SPARQL (Часть 3: graph_cli / graph.ttl, 2 уровня GREEN/RED)")
        else:
            src.append("Граф знаний W3C SOSA/SSN (Часть 3)")
    if f.get("source_note"):
        src.append(f"Примечание к пробе: {f['source_note']}")
    return src


def _base_notes(c: dict, mode: str, f: dict) -> List[str]:
    if not _is_real(c, "part1"):
        return [DEMO_NOTE]
    if not c["predictors"][mode]["real"]:
        return ["Модели прогноза (+10 дней, Часть 2) не подключены: показаны только фактические наблюдения Части 1."]
    return []


def _observation(f: dict, demo: bool) -> dict:
    return dict(date=f["date_used"], obs_status=f.get("obs_status"),
                is_interpolated=bool(f.get("is_interpolated")),
                scenes_in_decade=None if demo else f.get("scenes_in_decade"),
                valid_water_pixels=None if demo else f.get("valid_water_pixels"),
                water_coverage_pct=None if demo else f.get("water_coverage_pct"),
                source=(DEMO_SOURCE if demo else f.get("source")),
                scene_id=None if demo else f.get("scene_id"))


def _decadal_row_for(c: dict, wb_id: str, requested: date) -> Optional[dict]:
    """Поиск ближайшей декады в полном ряде satellite_decadal.parquet (включая лёд/облака/интерполяцию)."""
    dec = c.get("decadal_df")
    if dec is None or dec.empty:
        return None
    sub = dec[dec["water_body_id"] == wb_id]
    if sub.empty:
        return None
    dates = list(sub["date"])
    if requested < dates[0] or requested > dates[-1]:
        if min(abs((requested - dates[0]).days), abs((requested - dates[-1]).days)) > 10:
            return None
    i = min(range(len(dates)), key=lambda k: abs((dates[k] - requested).days))
    row = sub.iloc[i]
    d_used = row["date"]
    if abs((d_used - requested).days) > 5:
        return None
    status = None if pd.isna(row.get("obs_status")) else str(row.get("obs_status"))
    chl = adapters._num(row.get("chl_a_est_ug_l"))
    tsi = adapters._num(row.get("tsi_est"))
    scene = row.get("last_scene_id")
    return dict(
        date_used=d_used,
        obs_status=status,
        is_interpolated=(status == "interpolated_1step"),
        chl_a_current=chl,
        tsi_current=tsi,
        ndci_current=adapters._num(row.get("ndci")),
        b04_b02_ratio=adapters._num(row.get("b04_b02_ratio")),
        temp_mean_14d=None,
        calm_fraction_7d=None,
        precip_sum_14d=None,
        wind_mean_7d=None,
        radiation_mean_7d=None,
        secchi_depth_m=None,
        total_phosphorus_ug_l=None,
        water_coverage_pct=adapters._num(row.get("water_coverage_pct")),
        scenes_in_decade=adapters._int(row.get("scenes_in_decade")),
        valid_water_pixels=adapters._int(row.get("valid_water_pixels")),
        scene_id=None if scene is None or pd.isna(scene) else str(scene),
        source_note=None,
        source="Sentinel-2 L2A (ESA/Copernicus)",
    )


def _obs_tsi_fields(chl: Optional[float], tsi_raw: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
    if chl is None and tsi_raw is None:
        return None, None
    tsi = round(tsi_raw, 1) if tsi_raw is not None else stubs.carlson_tsi(chl)
    return tsi, stubs.trophic_class(tsi)


# ------------------------------------------------------------------ основные сценарии
def run_pipeline(c: dict, wb_id: str, requested: date, mode: str = "satellite") -> Optional[dict]:
    """Возвращает словарь полей AlertResponse или None, если на дату нет данных.
    ModeUnavailable — если для режима нет витрины/модели/данных по этому озеру."""
    if mode not in MODES:
        raise ValueError(f"Неизвестный режим '{mode}'. Допустимо: {', '.join(MODES)}")
    wb = c["registry"][wb_id]
    provider = c["providers"].get(mode)
    if provider is None or not provider.supports(wb_id):
        raise ModeUnavailable(f"Режим '{mode}' недоступен для водоёма '{wb_id}': нет данных этого режима")

    demo = not _is_real(c, "part1")
    pred = c["predictors"][mode]
    f = provider.get_features(wb_id, requested)

    # Если есть полная декадная таблица (satellite_decadal), проверяем точный статус декады (лёд/облака/интерполяция),
    # чтобы зимняя дата (например, январь) честно вернула ice_cover, а не притягивалась к апрелю!
    dec_row = _decadal_row_for(c, wb_id, requested) if (mode == "satellite" and not demo) else None
    if dec_row is not None and (f is None or dec_row["date_used"] != f["date_used"]):
        if dec_row["obs_status"] in NO_OBSERVATION or dec_row["obs_status"] == "interpolated_1step":
            f = dec_row
    if f is None:
        return None

    status = f.get("obs_status")
    obs_tsi, obs_trophic = _obs_tsi_fields(f.get("chl_a_current"), f.get("tsi_current"))
    result = dict(
        water_body_id=wb_id, water_body_name=wb["name"], mode=mode, requested_date=requested,
        date_used=f["date_used"], horizon_days=HORIZON_DAYS, obs_status=status,
        is_interpolated=bool(f.get("is_interpolated")),
        observed_tsi=obs_tsi, observed_trophic_class=obs_trophic,
        temp_mean_14d=f.get("temp_mean_14d"), calm_fraction_7d=f.get("calm_fraction_7d"),
        precip_sum_14d=f.get("precip_sum_14d"), wind_mean_7d=f.get("wind_mean_7d"),
        radiation_mean_7d=f.get("radiation_mean_7d"),
        ndci_current=f.get("ndci_current"), b04_b02_ratio=f.get("b04_b02_ratio"),
        secchi_depth_m=f.get("secchi_depth_m"), total_phosphorus_ug_l=f.get("total_phosphorus_ug_l"),
        observation=_observation(f, demo), sources=_base_sources(c, mode, f),
        notes=_base_notes(c, mode, f))

    if status in NO_OBSERVATION:                                   # наблюдения нет: ничего не придумываем
        return {**result, "chl_a_current": None, "observed_tsi": None, "observed_trophic_class": None,
                "chl_a_forecast": None,
                "pred_low_ug_l": None, "pred_high_ug_l": None, "uncertainty_ug_l": None,
                "bloom_probability": None, "attention_probability": None,
                "trophic_index": None, "trophic_class": None, "alert_level": None,
                "reasons": [NO_OBSERVATION_TEXT.get(status, f"Нет наблюдения ({status}): прогноз не строится")],
                "recommendations": []}

    if (not pred["real"] and not demo) or ("_frame" not in f and not demo):
        reason = (f"Прогноз недоступен: модель режима '{mode}' не подключена (Часть 2)"
                  if not pred["real"]
                  else "Декада восстановлена 1-шаговой интерполяцией: отображён фактический ряд, прогноз не строится")
        return {**result, "chl_a_current": f["chl_a_current"], "chl_a_forecast": None,
                "pred_low_ug_l": None, "pred_high_ug_l": None, "uncertainty_ug_l": None,
                "bloom_probability": None, "attention_probability": None,
                "trophic_index": None, "trophic_class": None, "alert_level": None,
                "reasons": [reason],
                "recommendations": []}

    chl, prob, att = _predict_one(pred["predictor"], f)            # Часть 2
    mae = model_mae(c, mode)
    low = high = None
    notes = list(result["notes"])
    if mae is not None:
        low, high = round(max(chl - mae, 0.0), 2), round(chl + mae, 2)
    else:
        notes.append("В model_metrics.json нет MAE — доверительный интервал не рассчитан.")
    v = _assess_verdict(                                           # Часть 3
        c["engine"], wb_id, wb, f["date_used"], chl, prob,
        f["temp_mean_14d"], f["calm_fraction_7d"], att, c["registry"],
    )
    if isinstance(c.get("engine"), adapters.GraphCliEngine):
        thr = getattr(c["engine"], "threshold_ug_l", 25.0)
        notes.append(
            f"Граф знаний Части 3 (graph_cli) выдаёт 2-уровневый статус (GREEN/RED, порог {thr:g} мкг/л); "
            "расширенный контекст (ООПТ, запрет хим. альгицидов, каскад, стратификация, порог 15 мкг/л, "
            "метео, вероятности, TSI и рекомендации) работает через заглушки Части 4."
        )
    return {**result, "chl_a_current": f["chl_a_current"], "chl_a_forecast": chl,
            "pred_low_ug_l": low, "pred_high_ug_l": high, "uncertainty_ug_l": mae,
            "bloom_probability": prob, "attention_probability": att,
            "trophic_index": v.trophic_index, "trophic_class": v.trophic_class, "alert_level": v.alert_level,
            "reasons": v.reasons, "recommendations": v.recommendations, "notes": notes}


def _assess_verdict(engine, wb_id, wb, date_used, chl, prob, temp14, calm7, att, registry):
    try:
        return engine.assess(
            wb_id, wb["name"], date_used, chl, prob, temp14, calm7,
            attention_prob=att, lake_meta=wb, registry=registry,
        )
    except TypeError:
        return engine.assess(wb_id, wb["name"], date_used, chl, prob, temp14, calm7)


def _predict_one(predictor, f: dict):
    out = predictor.predict(f)
    return out if len(out) == 3 else (out[0], out[1], None)


def history(c: dict, wb_id: str, mode: str = "satellite", end: Optional[date] = None,
            limit: int = 120) -> dict:
    """Ряд наблюдений + прогноз модели на +10 дней от каждой декады (для графика)."""
    if mode not in MODES:
        raise ValueError(f"Неизвестный режим '{mode}'. Допустимо: {', '.join(MODES)}")
    provider = c["providers"].get(mode)
    if provider is None or not provider.supports(wb_id):
        raise ModeUnavailable(f"Режим '{mode}' недоступен для водоёма '{wb_id}': нет данных этого режима")
    demo = not _is_real(c, "part1")
    end = end or config.DEFAULT_DATE
    frame = provider.history_frame(wb_id, end, limit=limit)
    notes = []
    if demo:
        notes.append(DEMO_NOTE)
    elif not c["predictors"][mode]["real"]:
        notes.append("Модель режима не подключена: на графике только наблюдения, прогноз не строится.")
    preds = []
    if not frame.empty and (c["predictors"][mode]["real"] or demo):
        try:
            preds = c["predictors"][mode]["predictor"].predict_many(frame)
        except Exception as e:             # noqa: BLE001 — историю без прогноза показываем честно
            notes.append(f"Прогноз для истории недоступен: {type(e).__name__}: {e}")
    mae = model_mae(c, mode) if preds else None

    pred_by_date: Dict[date, Tuple[Optional[float], Optional[float], Optional[float]]] = {}
    obs_by_date: Dict[date, dict] = {}
    for i, (_, row) in enumerate(frame.iterrows()):
        m = provider.row_meta(row)
        has_obs = m["obs_status"] not in NO_OBSERVATION and m["chl_a_current"] is not None
        out = preds[i] if i < len(preds) else None
        forecast = out[0] if (out and has_obs) else None
        pred_by_date[m["date"]] = (
            forecast,
            None if forecast is None or mae is None else round(max(forecast - mae, 0), 2),
            None if forecast is None or mae is None else round(forecast + mae, 2),
        )
        obs_by_date[m["date"]] = m

    # В реальном спутниковом режиме с satellite_decadal строим честную регулярную декадную сетку
    # с разрывами линии на лёд/облака и точками интерполяции.
    dec = c.get("decadal_df") if (mode == "satellite" and not demo) else None
    points = []
    if dec is not None and not dec.empty:
        sub = dec[(dec["water_body_id"] == wb_id) & (dec["date"] <= end)].tail(limit)
        for _, row in sub.iterrows():
            d = row["date"]
            status = None if pd.isna(row.get("obs_status")) else str(row.get("obs_status"))
            is_interp = status == "interpolated_1step"
            chl = None if status in NO_OBSERVATION else adapters._num(row.get("chl_a_est_ug_l"))
            fc, fc_lo, fc_hi = pred_by_date.get(d, (None, None, None))
            points.append(dict(date=d, observed=None if chl is None else round(chl, 2),
                               obs_status=status, is_interpolated=is_interp,
                               forecast=fc, forecast_low=fc_lo, forecast_high=fc_hi))
    else:
        for i, (_, row) in enumerate(frame.iterrows()):
            m = provider.row_meta(row)
            fc, fc_lo, fc_hi = pred_by_date.get(m["date"], (None, None, None))
            points.append(dict(date=m["date"], observed=m["chl_a_current"], obs_status=m["obs_status"],
                               is_interpolated=m["is_interpolated"], forecast=fc,
                               forecast_low=fc_lo, forecast_high=fc_hi))

    src = _base_sources(c, mode, dict(source=None, temp_mean_14d=1, calm_fraction_7d=None)) if not demo \
        else [DEMO_SOURCE]
    return dict(water_body_id=wb_id, mode=mode, horizon_days=HORIZON_DAYS, points=points,
                sources=src, notes=notes)


def _cascade_links(registry: Dict[str, dict]) -> List[dict]:
    """Построить каскадные связи между озёрами автоматически из ecological_context.cascade_outflow_to."""
    labels = {
        ("batorino", "myastro"): "р. Дробня",
        ("myastro", "naroch"): "протока Скема",
        ("volchino", "myadel"): "каскадный сток",
    }
    links = []
    for wb_id, wb in registry.items():
        target = wb.get("cascade_outflow_to")
        if target and target in registry:
            src_lon, src_lat = wb.get("lon"), wb.get("lat")
            dst_lon, dst_lat = registry[target].get("lon"), registry[target].get("lat")
            if None not in (src_lon, src_lat, dst_lon, dst_lat):
                links.append({
                    "from": wb_id,
                    "to": target,
                    "label": labels.get((wb_id, target), f"{wb['name']} → {registry[target]['name']}"),
                    "coordinates": [[src_lon, src_lat], [dst_lon, dst_lat]],
                })
    return links


def _lab_points_by_lake(c: dict) -> Dict[str, List[dict]]:
    """Реальные лабораторные пробы БГУ по озёрам для наложения на спутниковый график."""
    if not _is_real(c, "part1"):
        return {}
    lab_prov = c["providers"].get("lab")
    if lab_prov is None:
        return {}
    out: Dict[str, List[dict]] = {}
    for wb_id, df in getattr(lab_prov, "by_wb", {}).items():
        pts = []
        for _, row in df.iterrows():
            m = lab_prov.row_meta(row)
            if m["chl_a_current"] is None:
                continue
            pts.append({
                "date": m["date"].isoformat(),
                "chl": round(m["chl_a_current"], 2),
                "secchi": None if m["secchi_depth_m"] is None else round(m["secchi_depth_m"], 2),
                "tp": None if m["total_phosphorus_ug_l"] is None else round(m["total_phosphorus_ug_l"], 1),
                "note": m["source_note"],
            })
        if pts:
            out[wb_id] = pts
    return out


def timeline(c: dict, mode: str = "satellite") -> dict:
    """Компактный снимок всех декад (2018–2026) по всем водоёмам для плавной линии времени (60 FPS)."""
    if mode not in MODES:
        raise ValueError(f"Неизвестный режим '{mode}'. Допустимо: {', '.join(MODES)}")
    cache = c.setdefault("timeline_cache", {})
    if mode in cache:
        return cache[mode]

    demo = not _is_real(c, "part1")
    pred = c["predictors"][mode]
    has_forecast = bool(pred["real"] or demo)
    prov = c["providers"].get(mode)
    if prov is None:
        raise ModeUnavailable(f"Режим '{mode}' недоступен: нет данных этого режима")

    snapshots: Dict[str, Dict[str, dict]] = {}
    # Заранее считаем метео и прогнозы из витрины признаков (один векторный проход на озеро)
    feat_lookup: Dict[Tuple[str, str], dict] = {}
    for wb_id in c["registry"]:
        if not prov.supports(wb_id):
            continue
        end_d = prov.date_range(wb_id)[1]
        frame = prov.history_frame(wb_id, end_d, limit=1000)
        if frame.empty:
            continue
        preds = []
        if has_forecast:
            try:
                preds = pred["predictor"].predict_many(frame)
            except Exception:  # noqa: BLE001
                preds = []
        for idx, (_, row) in enumerate(frame.iterrows()):
            m = prov.row_meta(row)
            ds = m["date"].isoformat()
            status = m["obs_status"] or "valid"
            chl = None if status in NO_OBSERVATION else m["chl_a_current"]
            obs_tsi, _ = _obs_tsi_fields(chl, m.get("tsi_current"))
            fc_val = prob_val = alert_val = None
            if idx < len(preds) and chl is not None:
                p_tuple = preds[idx]
                fc_val = round(float(p_tuple[0]), 2)
                prob_val = round(float(p_tuple[1]), 3)
                thr = float(getattr(c.get("engine"), "threshold_ug_l", 25.0))
                alert_val = "RED" if fc_val >= thr else "GREEN"
            cov = m.get("water_coverage_pct")
            feat_lookup[(wb_id, ds)] = {
                "s": status,
                "c": None if chl is None else round(chl, 2),
                "t": obs_tsi,
                "f": fc_val,
                "p": prob_val,
                "a": alert_val,
                "t14": None if m.get("temp_mean_14d") is None else round(m["temp_mean_14d"], 1),
                "clm": None if m.get("calm_fraction_7d") is None else round(m["calm_fraction_7d"], 2),
                "cov": None if cov is None else round(cov, 0),
                "dc": None,
            }

    dec = c.get("decadal_df") if (mode == "satellite" and not demo) else None
    if dec is not None and not dec.empty:
        for _, row in dec.iterrows():
            wb_id = str(row["water_body_id"])
            ds = row["date"].isoformat()
            status = None if pd.isna(row.get("obs_status")) else str(row.get("obs_status"))
            status = status or "valid"
            cov = adapters._num(row.get("water_coverage_pct"))
            base = feat_lookup.get((wb_id, ds))
            if base is not None:
                snap = dict(base)
                snap["s"] = status
                if snap.get("cov") is None and cov is not None:
                    snap["cov"] = round(cov, 0)
            else:
                chl = None if status in NO_OBSERVATION else adapters._num(row.get("chl_a_est_ug_l"))
                tsi_raw = None if status in NO_OBSERVATION else adapters._num(row.get("tsi_est"))
                obs_tsi, _ = _obs_tsi_fields(chl, tsi_raw)
                snap = {
                    "s": status,
                    "c": None if chl is None else round(chl, 2),
                    "t": obs_tsi,
                    "f": None,
                    "p": None,
                    "a": None,
                    "t14": None,
                    "clm": None,
                    "cov": None if cov is None else round(cov, 0),
                    "dc": None,
                }
            snapshots.setdefault(ds, {})[wb_id] = snap
    else:
        for (wb_id, ds), snap in feat_lookup.items():
            snapshots.setdefault(ds, {})[wb_id] = snap

    dates = sorted(snapshots.keys())
    # Рассчитываем честную дельту (dc) относительно предыдущего валидного наблюдения для каждого озера
    prev_chl: Dict[str, float] = {}
    for ds in dates:
        day_map = snapshots[ds]
        for wb_id, snap in day_map.items():
            c_val = snap.get("c")
            if c_val is not None:
                if wb_id in prev_chl:
                    snap["dc"] = round(c_val - prev_chl[wb_id], 1)
                prev_chl[wb_id] = c_val

    years = sorted({int(d[:4]) for d in dates})
    default_iso = config.DEFAULT_DATE.isoformat()
    if default_iso in snapshots:
        default_date = default_iso
    else:
        valid_dates = [
            d for d in dates
            if any(v.get("s") in ("valid", "interpolated_1step") and v.get("c") is not None for v in snapshots[d].values())
        ]
        default_date = valid_dates[-1] if valid_dates else (dates[-1] if dates else default_iso)

    payload = {
        "mode": mode,
        "horizon_days": HORIZON_DAYS,
        "has_forecast": has_forecast,
        "dates": dates,
        "years": years,
        "default_date": default_date,
        "cascade_links": _cascade_links(c["registry"]),
        "snapshots": snapshots,
        "lab_points": _lab_points_by_lake(c),
    }
    cache[mode] = payload
    return payload


def mode_catalog(c: dict) -> dict:
    """Что реально доступно по каждому режиму (для /metrics и панели)."""
    out = {}
    for mode in MODES:
        prov = c["providers"].get(mode)
        pred = c["predictors"][mode]
        lakes = [w for w in c["registry"] if prov is not None and prov.supports(w)]
        reason = None
        if prov is None:
            reason = "нет витрины признаков режима"
        elif not pred["real"] and _is_real(c, "part1"):
            reason = "нет обученных моделей режима"
        out[mode] = dict(available=bool(lakes) and (pred["real"] or not _is_real(c, "part1")),
                         data_file=c["paths"].get(mode), models=list(pred["names"]) if pred["real"] else None,
                         lakes=lakes, reason=reason)
    return out
