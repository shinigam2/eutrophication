"""Тесты Части 3 (граф знаний graph_cli.py) и её стыков с Частями 1, 2 и 4."""
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest
from rdflib import Literal, Namespace
import yaml

from src.api import adapters
from src.knowledge_graph import graph_cli

ROOT = Path(__file__).resolve().parents[1]
EUTRO = Namespace(graph_cli.BASE_IRI)


def test_seed_rule_preserves_custom_threshold_without_duplicates():
    """Пункт 1: SEED_RULE не дописывает дубликат 25 мкг/л, если в графе уже задан кастомный порог (30 мкг/л)."""
    g = graph_cli.load_graph(ROOT / "data/knowledge_graph/graph.ttl")
    rule = EUTRO[graph_cli.ALERT_RULE_ID]
    g.remove((rule, EUTRO.thresholdUgL, None))
    g.add((rule, EUTRO.thresholdUgL, Literal(30)))

    g.update(graph_cli.PREFIXES + graph_cli.SEED_RULE)
    g.update(
        graph_cli.PREFIXES
        + graph_cli.insert_records("forecast", [("naroch", date(2026, 11, 21), 27.0, date(2026, 11, 11))])
    )

    thresholds = [float(v) for v in g.objects(rule, EUTRO.thresholdUgL)]
    assert thresholds == [30.0]

    rows = graph_cli.read_history(g, "naroch", target_date=date(2026, 11, 21))
    assert len(rows) == 1
    assert rows[0]["thresholdUgL"] == 30.0
    assert rows[0]["alertStatus"] == "below_threshold"


def test_observation_statuses_and_load_results_validation(tmp_path):
    """Пункты 2–3: interpolated_1step и insufficient_pixels поддержаны; прогноз на льду/облаках запрещён; strip ID."""
    # 1) interpolated_1step с числовым значением + пробелы вокруг water_body_id
    ok_path = tmp_path / "ok.json"
    ok_path.write_text(
        json.dumps({
            "predictions": {
                "naroch": {
                    "water_body_id": " naroch ",
                    "base_date": "2026-07-01",
                    "obs_status": "interpolated_1step",
                    "chl_a_current_ug_l": 4.8,
                    "chl_a_pred_ug_l": None,
                },
                "rudakovo": {
                    "water_body_id": "rudakovo",
                    "base_date": "2026-01-11",
                    "obs_status": "insufficient_pixels",
                    "chl_a_current_ug_l": None,
                    "chl_a_pred_ug_l": None,
                },
            }
        }),
        encoding="utf-8",
    )
    forecasts, observations = graph_cli.load_results(ok_path)
    assert forecasts == []
    assert ("naroch", date(2026, 7, 1), 4.8, "interpolated_1step") in observations
    assert ("rudakovo", date(2026, 1, 11), None, "insufficient_pixels") in observations

    g = graph_cli.load_graph(ROOT / "data/knowledge_graph/graph.ttl")
    g.update(graph_cli.PREFIXES + graph_cli.insert_records("observe", observations))
    interp_rows = graph_cli.search_records(graph=g, obs_status="interpolated_1step")
    assert len(interp_rows) == 1 and interp_rows[0]["observedUgL"] == 4.8
    miss_rows = graph_cli.search_records(graph=g, obs_status="insufficient_pixels")
    assert len(miss_rows) == 1 and miss_rows[0]["observedUgL"] is None

    # 2) Прогноз при ice_cover / cloudy_missing должен отклоняться
    bad_path = tmp_path / "bad_ice.json"
    bad_path.write_text(
        json.dumps({
            "predictions": {
                "naroch": {
                    "water_body_id": "naroch",
                    "base_date": "2026-01-11",
                    "date": "2026-01-21",
                    "obs_status": "ice_cover",
                    "chl_a_current_ug_l": None,
                    "chl_a_pred_ug_l": 12.0,
                }
            }
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="chl_a_pred_ug_l"):
        graph_cli.load_results(bad_path)


def test_search_records_on_search_test_ttl_and_legacy_obs_status():
    """Пункты 4 и 6б: фильтры search_records() на search_test.ttl + согласованность obs_status='valid' без трипла."""
    g = graph_cli.load_graph(ROOT / "data/knowledge_graph/search_test.ttl")

    assert len(graph_cli.search_records(graph=g)) == 5
    assert len(graph_cli.search_records(graph=g, record_type="forecast")) == 4
    assert len(graph_cli.search_records(graph=g, record_type="observation")) == 3
    assert len(graph_cli.search_records(graph=g, alert_status="warning")) == 2
    assert len(graph_cli.search_records(graph=g, alert_status="below_threshold")) == 2
    assert len(graph_cli.search_records(graph=g, date_from="2026-11-21", date_to="2026-12-01")) == 3
    assert len(graph_cli.search_records(graph=g, predicted_ug_l_min=20, predicted_ug_l_max=30)) == 2
    assert len(graph_cli.search_records(graph=g, observed_ug_l_min=15)) == 2

    desc_page = graph_cli.search_records(graph=g, sort_order="desc", limit=2, offset=1)
    assert [(r["waterBodyId"], r["targetDate"] or r["observedDate"]) for r in desc_page] == [
        ("test-myastro", "2026-11-21"),
        ("test-naroch", "2026-11-21"),
    ]
    row_21, = graph_cli.search_records(graph=g, water_body_id="test-myastro", target_date="2026-11-21")
    assert row_21["differenceUgL"] == 6.0

    # Пункт 4: наблюдения без явного трипла eutro:obsStatus считаются valid и в SEARCH, и в фильтре obs_status='valid'
    g.remove((None, EUTRO.obsStatus, None))
    valid_rows = graph_cli.search_records(graph=g, obs_status="valid")
    assert len(valid_rows) == 3
    assert all(r["obsStatus"] == "valid" and r["observedUgL"] is not None for r in valid_rows)


def test_cli_import_results_history_and_get_forecast_with_local_graph(tmp_path):
    """Пункт 6а: CLI import-results / history / get-forecast работают с локальным --graph (без Oxigraph)."""
    ttl = tmp_path / "local.ttl"
    shutil.copy2(ROOT / "data/knowledge_graph/graph.ttl", ttl)

    r_imp = subprocess.run(
        [sys.executable, str(ROOT / "src/knowledge_graph/graph_cli.py"), "import-results", "--graph", str(ttl)],
        capture_output=True,
        text=True,
        check=True,
    )
    res_json = json.loads((ROOT / "data/processed/result.json").read_text(encoding="utf-8"))
    exp_fc = sum(1 for r in res_json["predictions"].values() if r["chl_a_pred_ug_l"] is not None)
    exp_obs = len(res_json["predictions"])
    assert f"Обработано прогнозов: {exp_fc}. Наблюдений: {exp_obs}." in r_imp.stdout

    r_hist = subprocess.run(
        [sys.executable, str(ROOT / "src/knowledge_graph/graph_cli.py"), "history", "--graph", str(ttl), "--water-body-id", "batorino"],
        capture_output=True,
        text=True,
        check=True,
    )
    hist_rows = json.loads(r_hist.stdout)
    assert len(hist_rows) == 2

    # Рудаково на 2026-11-11 имеет пропуск cloudy_missing -> в графе сохранено наблюдение без числа и без прогноза
    r_rud = subprocess.run(
        [sys.executable, str(ROOT / "src/knowledge_graph/graph_cli.py"), "history", "--graph", str(ttl), "--water-body-id", "rudakovo"],
        capture_output=True,
        text=True,
        check=True,
    )
    rud_rows = json.loads(r_rud.stdout)
    assert len(rud_rows) == 1
    assert rud_rows[0]["obsStatus"] == "cloudy_missing"
    assert rud_rows[0]["observedUgL"] is None and rud_rows[0]["predictedUgL"] is None

    bat = res_json["predictions"]["batorino"]
    r_get = subprocess.run(
        [
            sys.executable,
            str(ROOT / "src/knowledge_graph/graph_cli.py"),
            "get-forecast",
            "--graph",
            str(ttl),
            "--water-body-id",
            "batorino",
            "--target-date",
            bat["date"],
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    rec = json.loads(r_get.stdout)
    assert rec["waterBodyId"] == "batorino"
    assert abs(rec["predictedUgL"] - bat["chl_a_pred_ug_l"]) < 0.01


def test_compose_api_wired_to_oxigraph_and_engine_syncs_threshold():
    """Пункты 5 и 7: compose.yaml связывает api -> oxigraph, а GraphCliEngine синхронизирует порог из Oxigraph."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    api_svc = compose["services"]["api"]
    assert "oxigraph" in (api_svc.get("depends_on") or [])
    assert api_svc.get("environment", {}).get("OXIGRAPH_URL") == "http://oxigraph:7878"

    class _CustomThrOxi(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8")
            if "SELECT ?thresholdUgL" in body:
                payload = json.dumps({
                    "head": {"vars": ["thresholdUgL"]},
                    "results": {"bindings": [{"thresholdUgL": {"type": "literal", "value": "30"}}]},
                }).encode("utf-8")
            else:
                payload = json.dumps({"head": {"vars": []}, "results": {"bindings": []}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/sparql-results+json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = HTTPServer(("127.0.0.1", 0), _CustomThrOxi)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        eng = adapters.GraphCliEngine(
            "src.knowledge_graph.graph_cli",
            ROOT / "data/knowledge_graph/graph.ttl",
            oxigraph_url=f"http://127.0.0.1:{srv.server_port}",
        )
        assert eng.threshold_ug_l == 30.0
        # Локальный fallback rdflib также использует синхронизированный порог 30 мкг/л
        assert eng.assess("naroch", "Озеро Нарочь", date(2025, 7, 11), 27.0, 0.4, 20.0, 0.3).alert_level == "GREEN"
        assert eng.assess("naroch", "Озеро Нарочь", date(2025, 7, 11), 31.0, 0.8, 20.0, 0.3).alert_level == "RED"
    finally:
        srv.shutdown()
