import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rdflib import Graph, Literal, Namespace, RDF

from src.knowledge_graph import graph_cli


class GraphUpsertTests(TestCase):
    def setUp(self):
        self.graph = Graph()
        self.eutro = Namespace(graph_cli.BASE_IRI)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.results = Path(directory.name) / "result.json"

    def run_cli(self, *args):
        def graph_request(_endpoint, route, content, _media_type):
            if route == "query":
                return self.graph.query(content.decode("utf-8")).serialize(format="json")
            self.assertEqual(route, "update")
            self.graph.update(content.decode("utf-8"))

        with patch("sys.argv", ["graph_cli", *args]), patch.object(graph_cli, "oxigraph_post", side_effect=graph_request):
            with redirect_stdout(StringIO()):
                graph_cli.main()

    def import_results(self, **fields):
        prediction = {
            "water_body_id": "naroch",
            "base_date": "2026-11-11",
            "date": "2026-11-21",
            "chl_a_current_ug_l": 3.65,
            "chl_a_pred_ug_l": 3.47,
            **fields,
        }
        self.results.write_text(json.dumps({"predictions": {"naroch": prediction}}), encoding="utf-8")

        self.run_cli("import-results", "--input", str(self.results))

    def test_reimport_updates_the_forecast_and_its_alert(self):
        self.import_results()
        forecast, = self.graph.subjects(RDF.type, self.eutro.Forecast)
        self.graph.add((forecast, self.eutro.source, self.eutro.model))
        for value, status, alert_count in (
            (32.5, "warning", 1),
            (32.5, "warning", 1),
            (25, "warning", 1),
            (3.47, "below_threshold", 0),
        ):
            self.import_results(chl_a_pred_ug_l=value)
            row, = graph_cli.search_records(graph=self.graph, record_type="forecast")
            self.assertEqual(row["predictedUgL"], value)
            self.assertEqual(row["alertStatus"], status)
            self.assertEqual(list(self.graph.subjects(RDF.type, self.eutro.Forecast)), [forecast])
            self.assertEqual(len(list(self.graph.subjects(RDF.type, self.eutro.Alert))), alert_count)
            self.assertIn((forecast, self.eutro.source, self.eutro.model), self.graph)

    def test_reimport_replaces_observation_status_and_value(self):
        self.import_results()
        observation, = self.graph.subjects(RDF.type, self.eutro.Observation)
        self.import_results(obs_status="cloudy_missing", chl_a_current_ug_l=None, chl_a_pred_ug_l=None)
        row, = graph_cli.search_records(graph=self.graph, record_type="observation")
        self.assertEqual(row["obsStatus"], "cloudy_missing")
        self.assertIsNone(row["observedUgL"])
        self.assertNotIn((observation, self.eutro.chlorophyllUgL, None), self.graph)

        self.import_results(chl_a_current_ug_l=5.2)
        row, = graph_cli.search_records(graph=self.graph, record_type="observation")
        self.assertEqual(row["obsStatus"], "valid")
        self.assertEqual(row["observedUgL"], 5.2)
        self.assertEqual(list(self.graph.subjects(RDF.type, self.eutro.Observation)), [observation])

    def sync_lakes(self, *lakes):
        path = self.results.with_name("lakes.geojson")
        features = [{"type": "Feature", "properties": {"id": water_id, "name_ru": name}} for water_id, name in lakes]
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
        self.run_cli("sync-lakes", "--geojson", str(path), "--endpoint", graph_cli.DEFAULT_ENDPOINT)

    def test_sync_lakes_updates_names_on_existing_nodes(self):
        lake = self.eutro["registered-naroch"]
        self.graph.add((lake, self.eutro.waterBodyId, Literal("naroch")))
        self.graph.add((lake, self.eutro.name, Literal("Старое название")))
        self.import_results()
        forecast, = self.graph.subjects(RDF.type, self.eutro.Forecast)

        self.sync_lakes(("naroch", "Нарочь"), ("myastro", "Мястро"))
        self.sync_lakes(("naroch", "Озеро Нарочь"), ("myastro", "Озеро Мястро"))
        after_update = set(self.graph)
        self.sync_lakes(("naroch", "Озеро Нарочь"), ("myastro", "Озеро Мястро"))

        self.assertEqual(set(self.graph), after_update)
        self.assertEqual(list(self.graph.subjects(self.eutro.waterBodyId, Literal("naroch"))), [lake])
        self.assertEqual(list(self.graph.objects(lake, self.eutro.name)), [Literal("Озеро Нарочь")])
        self.assertIn((forecast, self.eutro.waterBody, lake), self.graph)

    def test_sync_lakes_rejects_duplicate_ids_before_writing(self):
        self.sync_lakes(("naroch", "Нарочь"))
        before = set(self.graph)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            self.sync_lakes(("naroch", "Новое название"), (" naroch ", "Другой водоём"))
        self.assertEqual(set(self.graph), before)

        self.graph.add((self.eutro["duplicate-naroch"], self.eutro.waterBodyId, Literal("naroch")))
        before = set(self.graph)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            self.sync_lakes(("naroch", "Новое название"))
        self.assertEqual(set(self.graph), before)
