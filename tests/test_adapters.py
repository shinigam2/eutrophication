"""Проверка адаптеров на ИСКУССТВЕННЫХ «чужих» файлах (yaml, csv, joblib, фейковый движок).
Запуск: python tests/test_adapters.py   (или pytest)"""
import dataclasses, enum, json, os, sys, tempfile, types
from datetime import date
from pathlib import Path
sys.path.insert(0, ".")

import joblib, numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge


def make_project(root: Path):
    (root / "configs").mkdir(); (root / "models").mkdir(); (root / "data").mkdir()
    (root / "configs/water_bodies.yaml").write_text(
        "water_bodies:\n"
        "  - id: WB_01\n    name: Тестовое водохранилище\n    type: reservoir\n    coordinates: {lat: 54.0, lon: 27.3}\n"
        "    bbox: [27.2, 53.9, 27.4, 54.1]\n    morphometry: {area_km2: 31, mean_depth_m: 3.5}\n"
        "  - id: WB_02\n    name: Тестовое озеро\n    bbox: {min_lat: 54.7, min_lon: 26.6, max_lat: 54.9, max_lon: 26.9}\n", encoding="utf-8")
    rng = np.random.default_rng(0); rows = []
    for wb in ("WB_01", "WB_02"):
        for d in pd.date_range("2020-01-01", "2025-12-31", freq="D"):
            if d.day in (1, 11, 21):
                doy = d.dayofyear
                rows.append(dict(water_body_id=wb, date=d.strftime("%Y%m%d") if False else d.date().isoformat(),
                                 sin_doy=np.sin(2*np.pi*doy/365), cos_doy=np.cos(2*np.pi*doy/365),
                                 temp_mean_14d=10+14*np.sin(2*np.pi*(doy-110)/365), calm_fraction_7d=rng.uniform(0, .6),
                                 chl_a=max(0.5, 3+30*np.exp(-((doy-215)/45)**2)+rng.normal(0, 1))))
    df = pd.DataFrame(rows); df.to_csv(root / "data/features_dataset.csv", index=False)
    X = df[["sin_doy", "cos_doy", "temp_mean_14d", "calm_fraction_7d"]]
    joblib.dump(Ridge().fit(X, df.chl_a), root / "models/lgb_regressor_chla.joblib")
    joblib.dump(LogisticRegression().fit(X, df.chl_a >= 25), root / "models/lgb_classifier_bloom.joblib")
    (root / "models/model_metrics.json").write_text(
        '{"regression": {"naive": {"rmse": 5.1, "mae": 3.0, "r2": 0.4}, "lightgbm": {"rmse": 3.2, "mae": 2.1, "r2": 0.8}},'
        ' "classification": {"lightgbm": {"f1": 0.7, "roc_auc": 0.9}}}', encoding="utf-8")


def fake_engine_module():
    class Lvl(enum.Enum):
        GREEN = 1; YELLOW = 2; RED = 3

    @dataclasses.dataclass
    class V:                                   # другие имена полей, чем у нас — адаптер должен справиться
        level: Lvl; tsi: float; trophic_state: str; causes: list; measures: list

    class WaterKnowledgeGraphEngine:
        def __init__(self, ontology_path=None): pass
        def evaluate(self, water_body_id, chl_pred, bloom_prob, temp_mean_14d, calm_fraction_7d):
            lvl = Lvl.RED if (chl_pred >= 25 or bloom_prob >= .5) else Lvl.GREEN
            return V(lvl, 55.123, "эвтрофный", ["причина"], ["мера 1", "мера 2"])

    m = types.ModuleType("src.knowledge_graph.graph_engine"); m.WaterKnowledgeGraphEngine = WaterKnowledgeGraphEngine
    sys.modules["src.knowledge_graph.graph_engine"] = m


def test_all():
    from src.api import adapters, pipeline
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); os.environ["PROJECT_ROOT"] = tmp
        for k in ("WATER_STRICT", "WATER_CHL_COLUMN"): os.environ.pop(k, None)

        # 1) пустой проект -> всё на заглушках, strict -> ошибка
        sys.modules.pop("src.knowledge_graph.graph_engine", None)
        c = pipeline.build_components()
        assert len(c["stub_parts"]) == 3, c["status"]
        os.environ["WATER_STRICT"] = "1"
        try: pipeline.build_components(); raise AssertionError("strict должен падать")
        except FileNotFoundError: pass
        os.environ.pop("WATER_STRICT")

        # 2) реальные файлы
        make_project(root); fake_engine_module()
        c = pipeline.build_components()
        assert c["stub_parts"] == [], c["status"]
        reg = c["registry"]
        assert reg["WB_01"]["bbox"] == dict(min_lat=53.9, min_lon=27.2, max_lat=54.1, max_lon=27.4)   # порядок угадан
        assert reg["WB_01"]["area_km2"] == 31 and reg["WB_01"]["lat"] == 54.0
        assert reg["WB_02"]["lat"] and reg["WB_02"]["area_km2"] is None
        res = pipeline.run_pipeline(c, "WB_01", date(2025, 8, 5))                   # 5 августа -> декада 1 августа
        assert res["date_used"] == date(2025, 8, 1), res
        assert res["alert_level"] in ("GREEN", "RED") and res["reasons"] == ["причина"] and res["trophic_index"] == 55.1
        assert pipeline.run_pipeline(c, "WB_01", date(2035, 1, 1)) is None
        assert pipeline.run_pipeline(c, "WB_01", date(2025, 1, 1)) is not None
        # панель динамическая: данных внутри нет, она ходит в API
        js = (Path(__file__).resolve().parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
        assert "/predict/" in js and "/lakes" in js and "/history/" in js and "const D =" not in js

        # 3) файл на месте, но сломан -> громкая ошибка, а не тихая заглушка
        (root / "data/features_dataset.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        try: pipeline.build_components(); raise AssertionError("должна быть IntegrationError")
        except adapters.IntegrationError as e: print("ожидаемая ошибка:", str(e)[:90])
    print("OK")


def _contract_project(root: Path, lakes=("naroch", "myastro")):
    """Проект строго по DATA_CONTRACT.md: имена столбцов, YAML с name_ru/center_lat/morphometry, lakes.geojson."""
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    for d in ("configs", "data/processed", "data/reference", "models"): (root / d).mkdir(parents=True, exist_ok=True)
    yaml_lines, feats, rng = [], [], np.random.default_rng(1)
    for k, wb in enumerate(lakes):
        lat, lon = 54.85 + 0.03 * k, 26.77 + 0.05 * k
        yaml_lines.append(f"{wb}:\n  id: {wb}\n  name_ru: Озеро {wb}\n  center_lat: {lat}\n  center_lon: {lon}\n"
                          f"  bbox: [{lon-.05}, {lat-.04}, {lon+.05}, {lat+.04}]\n  morphometry: {{area_km2: {10+k}, mean_depth_m: 5.5}}\n")
        for d in pd.date_range("2020-01-01", "2026-11-21", freq="D"):
            if d.day not in (1, 11, 21): continue
            doy = d.dayofyear; ice = d.month in (12, 1, 2, 3)
            status = "ice_cover" if ice else "interpolated_1step" if (d.month, d.day) == (9, 1) else "valid"
            chl = np.nan if ice else max(.5, 4 + 28*np.exp(-((doy-215)/40)**2) + rng.normal(0, 1))
            feats.append(dict(water_body_id=wb, water_body_name=wb, date=d.date().isoformat(), obs_status=status, valid_water_pixels=0 if ice else 900,
                scenes_in_decade=0 if ice else 2, is_interpolated=status == "interpolated_1step", month=d.month, day_of_year=doy,
                sin_doy=np.sin(2*np.pi*doy/365.25), cos_doy=np.cos(2*np.pi*doy/365.25), area_km2=10+k, ndci_current=np.nan if ice else chl/100,
                chl_a_est_current=chl, temp_mean_14d=10+14*np.sin(2*np.pi*(doy-110)/365), calm_fraction_7d=rng.uniform(0, .6)))
    (root / "configs/water_bodies.yaml").write_text("".join(yaml_lines), encoding="utf-8")
    df = pd.DataFrame(feats); df.to_csv(root / "data/processed/features_dataset.csv", index=False)
    geo = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": wb}, "geometry": {"type": "Polygon", "coordinates": [[[26.7+.05*k, 54.8+.03*k], [26.8+.05*k, 54.8+.03*k], [26.8+.05*k, 54.9+.03*k], [26.7+.05*k, 54.8+.03*k]]]}}
        for k, wb in enumerate(lakes)] + [{"type": "Feature", "properties": {"id": "x"}, "geometry": {"type": "Point", "coordinates": [26, 54]}}]}
    (root / "data/reference/lakes.geojson").write_text(json.dumps(geo), encoding="utf-8")
    cols = ["month", "sin_doy", "cos_doy", "area_km2", "ndci_current", "chl_a_est_current", "temp_mean_14d", "calm_fraction_7d"]
    y = df.chl_a_est_current.shift(-1).fillna(5)
    joblib.dump(HistGradientBoostingRegressor().fit(df[cols], y), root / "models/lgb_regressor_chla.joblib")
    joblib.dump(HistGradientBoostingClassifier().fit(df[cols], (y >= 25).astype(int)), root / "models/lgb_classifier_bloom.joblib")


def test_contract_modes_and_history():
    """Основные имена витрин (features_satellite / features_lab), режимы и маршрут истории."""
    from src.api import pipeline
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); os.environ["PROJECT_ROOT"] = tmp
        for k in ("WATER_STRICT", "WATER_FEATURES", "WATER_FEATURES_LAB", "WATER_CHL_COLUMN",
                  "WATER_CHL_COLUMN_LAB", "WATER_MODELS_LAB_DIR"):
            os.environ.pop(k, None)
        sys.modules.pop("src.knowledge_graph.graph_engine", None)
        fake_engine_module()
        _contract_project(root)
        sat_csv = root / "data/processed/features_dataset.csv"
        df = pd.read_csv(sat_csv)
        df.to_csv(root / "data/processed/features_satellite.csv", index=False)
        lab = df.rename(columns={"date": "sample_date", "chl_a_est_current": "chl_a_lab_ug_l"})
        lab["chl_a_est_current"] = lab["chl_a_lab_ug_l"]          # модели ждут этот признак
        lab.to_csv(root / "data/processed/features_lab.csv", index=False)
        cols = ["month", "sin_doy", "cos_doy", "area_km2", "ndci_current", "chl_a_est_current",
                "temp_mean_14d", "calm_fraction_7d"]
        y = lab.chl_a_lab_ug_l.shift(-1).fillna(5)
        joblib.dump(HistGradientBoostingRegressor().fit(lab[cols], y),
                    root / "models/lgb_regressor_chla_lab.joblib")
        joblib.dump(HistGradientBoostingClassifier().fit(lab[cols], (y >= 25).astype(int)),
                    root / "models/lgb_classifier_bloom_lab.joblib")

        c = pipeline.build_components()
        assert c["paths"]["satellite"] == "features_satellite.csv", c["paths"]
        assert c["paths"]["lab"] == "features_lab.csv", c["paths"]
        assert {"satellite", "lab"} <= set(pipeline.modes_for(c, "naroch"))
        sat = pipeline.run_pipeline(c, "naroch", date(2025, 7, 11), "satellite")
        labr = pipeline.run_pipeline(c, "naroch", date(2025, 7, 11), "lab")
        assert sat["mode"] == "satellite" and labr["mode"] == "lab"
        assert sat["chl_a_forecast"] is not None and labr["chl_a_forecast"] is not None
        h = pipeline.history(c, "naroch", "satellite", date(2025, 7, 1), 12)
        assert len(h["points"]) == 12 and h["points"][-1]["date"] == date(2025, 7, 1)
        assert any(p["forecast"] is not None for p in h["points"])
        assert any(p["observed"] is not None for p in h["points"])


def _bump(path: Path, seconds=3):                      # гарантируем другое время изменения файла
    st = path.stat(); os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 10**9))


def test_contract():
    from src.api import adapters, pipeline
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); os.environ["PROJECT_ROOT"] = tmp
        for k in ("WATER_STRICT", "WATER_CHL_COLUMN"): os.environ.pop(k, None)
        sys.modules.pop("src.knowledge_graph.graph_engine", None)
        _contract_project(root)
        # Г1: витрина по контракту подключается без подсказок (столбец chl_a_est_current)
        c = pipeline.build_components()
        assert c["provider"].chl_col == "chl_a_est_current" and c["status"]["part2"].startswith("реальная")
        # Г2: реестр: name_ru, center_lat/center_lon, морфометрия, полигон из GeoJSON (точки и прочее игнорируются)
        r = c["registry"]["naroch"]
        assert r["name"] == "Озеро naroch" and (r["lat"], r["lon"]) == (54.85, 26.77) and r["area_km2"] == 10
        assert r["geometry"]["type"] == "Polygon"
        assert "x" not in adapters.load_geometry(root / "data/reference/lakes.geojson"), "точки (не полигоны) игнорируются"
        # Г3: декада со льдом: прогноз и уровень не строятся, статус передан, значения не придуманы
        ice = pipeline.run_pipeline(c, "naroch", date(2025, 1, 11))
        assert ice["obs_status"] == "ice_cover" and ice["alert_level"] is None and ice["chl_a_forecast"] is None
        assert ice["chl_a_current"] is None and "Ледяной" in ice["reasons"][0] and ice["recommendations"] == []
        # Г4: интерполированная декада: прогноз есть, признак интерполяции передан
        ip = pipeline.run_pipeline(c, "naroch", date(2025, 9, 1))
        assert ip["is_interpolated"] is True and ip["obs_status"] == "interpolated_1step" and ip["alert_level"] is not None
        ok = pipeline.run_pipeline(c, "naroch", date(2025, 7, 11))
        assert ok["obs_status"] == "valid" and ok["is_interpolated"] is False and 0 <= ok["bloom_probability"] <= 1
        # Г5: добавление озера при работающем сервисе: файлы обновились -> озеро появилось, модели не перезагружались
        pred_before = c["predictor"]
        assert "batorino" not in c["registry"]
        _contract_project(root, lakes=("naroch", "myastro", "batorino"))
        for f in ("configs/water_bodies.yaml", "data/processed/features_dataset.csv", "data/reference/lakes.geojson"): _bump(root / f)
        pipeline.refresh_part1(c)
        assert "batorino" in c["registry"] and c["registry"]["batorino"]["geometry"] and c["reload_error"] is None
        assert c["predictor"] is pred_before, "модели не должны перезагружаться"
        assert pipeline.run_pipeline(c, "batorino", date(2025, 7, 11))["obs_status"] == "valid"
        # Г6: файлы без изменений -> повторного чтения нет
        reg_obj = c["registry"]; pipeline.refresh_part1(c); assert c["registry"] is reg_obj
        # Г7: новые файлы испорчены -> прежние данные остаются, причина сообщается
        (root / "data/processed/features_dataset.csv").write_text("a,b\n1,2\n", encoding="utf-8"); _bump(root / "data/processed/features_dataset.csv", 6)
        pipeline.refresh_part1(c)
        assert "batorino" in c["registry"] and c["reload_error"] and "water_body_id" in c["reload_error"]
        # Г8: полигон не найден для озера -> geometry=None (на карте будет прямоугольник BBox)
        from src.api import adapters
        assert adapters.load_geometry(root / "data/reference/lakes.geojson").get("nonexistent") is None
    print("OK (contract)")


if __name__ == "__main__":
    test_all()
    test_contract()
