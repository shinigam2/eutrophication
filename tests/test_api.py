"""pytest tests/ -v  (нужны fastapi и httpx)"""
import os, tempfile
_tmp = tempfile.mkdtemp()
os.environ["PROJECT_ROOT"] = _tmp   # пустой проект: тест всегда идёт на заглушках
from fastapi.testclient import TestClient
from src.api.main import app


def test_all():
    with TestClient(app) as c:                      # with — чтобы сработал lifespan
        assert c.get("/").json()["status"] == "ok"
        wbs = c.get("/lakes").json()                # основной маршрут по PROJECT_GUIDE
        assert len(wbs) == 2 and all(w["geometry"]["type"] == "Polygon" for w in wbs)
        assert all({"satellite", "lab"} <= set(w["modes"]) for w in wbs)
        assert wbs[0]["area_km2"] and wbs[0]["mean_depth_m"]            # паспорт водоёма
        assert c.get("/water-bodies").json() == wbs    # старый адрес остался совместимым
        assert "reload_error" in c.get("/").json()
        comp = c.get("/").json()["components"]
        assert comp["part1"].startswith("заглушка") and "modes" in c.get("/").json()

        met = c.get("/metrics").json()
        assert "note" in met and "modes" in met and "data_quality" in met
        assert met["horizon_days"] == 10 and {"satellite", "lab"} <= set(met["modes"])

        page = c.get("/dashboard")                       # без слеша: перенаправление на /dashboard/
        assert "<html" in page.text and 'src="app.js"' in page.text
        assert c.get("/dashboard/style.css").status_code == 200
        js = c.get("/dashboard/app.js")
        assert js.status_code == 200
        for ep in ("/lakes", "/timeline", "/predict/", "/history/", "/metrics", "/lab/upload"):
            assert ep in js.text, ep

        # линия времени: декады, годы и снимки по всем водоёмам
        tl = c.get("/timeline").json()
        assert tl["mode"] == "satellite" and tl["dates"] and tl["years"]
        assert tl["default_date"] in tl["snapshots"]
        assert "naroch" in tl["snapshots"][tl["default_date"]]
        tl_lab = c.get("/timeline", params={"mode": "lab"}).json()
        assert tl_lab["mode"] == "lab" and tl_lab["dates"]
        assert c.get("/timeline", params={"mode": "bogus"}).status_code == 422

        assert c.get("/predict/zaslavskoe", params={"date": "2025-07-11"}).json()["alert_level"] == "RED"
        assert c.get("/predict/naroch", params={"date": "2025-07-11"}).json()["alert_level"] == "YELLOW"
        r = c.get("/predict/naroch", params={"date": "2025-05-21"}).json()
        assert r["alert_level"] == "GREEN" and r["obs_status"] == "valid"
        assert r["mode"] == "satellite" and r["observation"]["source"].startswith("Демонстрационные")
        assert r["horizon_days"] == 10 and r["sources"] and r["uncertainty_ug_l"] is None
        assert any("MAE" in n for n in r["notes"]), "без model_metrics.json интервал не выдумывается"
        assert r["pred_low_ug_l"] is None and r["pred_high_ug_l"] is None

        # лабораторный режим — отдельные данные и модель, та же схема ответа
        lab = c.get("/predict/naroch", params={"date": "2025-07-11", "mode": "lab"}).json()
        assert lab["mode"] == "lab" and lab["water_body_id"] == "naroch"
        assert lab["chl_a_current"] is not None
        assert c.get("/predict/naroch", params={"mode": "bogus"}).status_code == 422

        # зима: наблюдения нет — прогноз/уровень/вероятность отсутствуют
        j = c.get("/predict/naroch", params={"date": "2025-01-11"}).json()
        assert j["obs_status"] == "ice_cover"
        assert j["alert_level"] is None and j["chl_a_forecast"] is None and j["bloom_probability"] is None
        j = c.get("/predict/naroch", params={"date": "2025-09-01"}).json()   # интерполяция
        assert j["is_interpolated"] is True and j["alert_level"] is not None
        assert c.get("/predict/unknown").status_code == 404
        assert c.get("/predict/naroch", params={"date": "2030-01-01"}).status_code == 404
        assert c.get("/predict/naroch", params={"date": "abc"}).status_code == 422

        # история: наблюдения и прогноз на +10 дней отдельными полями
        h = c.get("/history/naroch", params={"date": "2025-07-01", "limit": 12}).json()
        assert h["mode"] == "satellite" and h["horizon_days"] == 10 and len(h["points"]) == 12
        assert h["points"][-1]["date"] == "2025-07-01"
        assert any(pt["observed"] is not None for pt in h["points"])
        assert any(pt["forecast"] is not None for pt in h["points"])
        assert h["sources"] and any("не являются реальными" in n for n in h["notes"])
        hl = c.get("/history/naroch", params={"date": "2025-07-01", "mode": "lab", "limit": 12}).json()
        assert hl["mode"] == "lab" and hl["points"][-1]["date"] == "2025-07-01"
        assert c.get("/history/unknown").status_code == 404
        assert c.get("/history/naroch", params={"mode": "bogus"}).status_code == 422


def test_lab_upload():
    with TestClient(app) as c:
        cors = c.options("/lab/upload", headers={"Origin": "http://localhost:5500",
                                                 "Access-Control-Request-Method": "POST"})
        assert cors.status_code == 200 and "POST" in cors.headers["access-control-allow-methods"]

        body = {"water_body_id": "naroch", "sample_date": "2025-08-01", "chl_a_lab_ug_l": 12.4,
                "secchi_depth_m": 2.1, "total_phosphorus_ug_l": 38, "source_note": "тест"}
        r = c.post("/lab/upload", json=body)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["saved"] is True and os.path.isfile(j["path"])
        assert j["record"]["chl_a_lab_ug_l"] == 12.4 and j["satellite_comparison"] is None
        with open(j["path"], encoding="utf-8") as f:
            assert "chl_a_lab_ug_l" in f.readline()

        assert c.post("/lab/upload", json=body).status_code == 409          # дубль
        assert c.post("/lab/upload", json={**body, "chl_a_lab_ug_l": 5000}).status_code == 422
        assert c.post("/lab/upload", json={**body, "sample_date": "2025-08-02"}).status_code == 200
        assert c.post("/lab/upload", json={**body, "water_body_id": "nope"}).status_code == 404


def test_registry_filtering_and_geojson_hydration(tmp_path):
    """Отключённые (enabled: false) озёра исключаются; lat/lon/bbox дозаполняются из lakes.geojson."""
    import json
    from src.api import adapters

    reg_path = tmp_path / "water_bodies.yaml"
    reg_path.write_text(
        "water_bodies:\n"
        "  - id: active_lake\n"
        "    name_ru: Активное озеро\n"
        "    enabled: true\n"
        "    ecological_context:\n"
        "      protected_area: ООПТ\n"
        "      chemical_treatment_allowed: false\n"
        "      cascade_outflow_to: naroch\n"
        "  - id: excluded_lake\n"
        "    name_ru: Исключённое озеро\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    geo_path = tmp_path / "lakes.geojson"
    geo_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"id": "active_lake", "center_lat": 54.85, "center_lon": 26.78,
                           "bbox": [26.70, 54.80, 26.85, 54.90]},
            "geometry": {"type": "Polygon", "coordinates": [[[26.7, 54.8], [26.85, 54.8], [26.85, 54.9], [26.7, 54.8]]]},
        }],
    }), encoding="utf-8")

    reg = adapters.load_registry(reg_path)
    assert list(reg.keys()) == ["active_lake"]
    assert reg["active_lake"]["chemical_treatment_allowed"] is False
    assert reg["active_lake"]["cascade_outflow_to"] == "naroch"
    adapters.hydrate_registry_geometry(reg, geo_path)
    assert reg["active_lake"]["lat"] == 54.85 and reg["active_lake"]["lon"] == 26.78
    assert reg["active_lake"]["bbox"]["min_lon"] == 26.7
    assert reg["active_lake"]["geometry"]["type"] == "Polygon"
