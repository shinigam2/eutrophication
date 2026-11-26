"""Записывает водоёмы, прогнозы и наблюдения и запрашивает прогнозы из Oxigraph."""

import argparse
from calendar import monthrange
from datetime import date
import json
from math import isfinite
from pathlib import Path
from string import Template
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from filelock import FileLock, Timeout
from rdflib import Graph, Namespace
from rdflib.exceptions import ParserError
from rdflib.plugins.serializers.turtle import TurtleSerializer


BASE_IRI = "https://shinigam2.github.io/eutrophication/"
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_GRAPH = DATA_DIR / "knowledge_graph" / "graph.ttl"
DEFAULT_GEOJSON = DATA_DIR / "reference" / "lakes.geojson"
DEFAULT_PREDICTIONS = DATA_DIR / "processed" / "result.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:7878"
PREFIXES = f"""
PREFIX eutro: <{BASE_IRI}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

ALERT_RULE_ID = "chlorophyll-alert"
SEED_RULE = f"INSERT DATA {{ eutro:{ALERT_RULE_ID} eutro:thresholdUgL 25 . }}"

CREATE_ALERTS = """
INSERT {
  ?alert a eutro:Alert ; eutro:forForecast ?forecast ; eutro:triggeredRule ?rule .
}
WHERE {
  ?forecast a eutro:Forecast ; eutro:chlorophyllUgL ?predicted ; eutro:usesRule ?rule .
  ?rule eutro:thresholdUgL ?threshold .
  FILTER (?predicted >= ?threshold)
  BIND(IRI(CONCAT(STR(?forecast), "/alert")) AS ?alert)
}
"""

HISTORY = Template("""
SELECT ?waterBodyId ?waterBody ?periodStart ?periodEnd ?predictedUgL ?thresholdUgL
       ?alertStatus ?observedUgL ?obsStatus
WHERE {
  {
    SELECT DISTINCT ?water ?periodStart ?periodEnd
    WHERE {
      $period_values
      VALUES ?kind { eutro:Forecast eutro:Observation }
      ?entry a ?kind ; eutro:waterBody ?water ; eutro:period ?period .
      ?period eutro:start ?periodStart ; eutro:end ?periodEnd .
    }
  }
  OPTIONAL { ?water eutro:waterBodyId ?registeredId . }
  OPTIONAL { ?water eutro:name ?name . }
  BIND(COALESCE(?registeredId, STRAFTER(STR(?water), STR(eutro:))) AS ?waterBodyId)
  BIND(COALESCE(?name, ?waterBodyId) AS ?waterBody)
  OPTIONAL {
    ?forecast a eutro:Forecast ; eutro:waterBody ?water ; eutro:period ?forecastPeriod ;
        eutro:chlorophyllUgL ?predictedUgL .
    ?forecastPeriod eutro:start ?periodStart .
    OPTIONAL {
      ?forecast eutro:usesRule ?rule .
      ?rule eutro:thresholdUgL ?thresholdUgL .
      OPTIONAL { ?alert a eutro:Alert ; eutro:forForecast ?forecast . }
      BIND(IF(BOUND(?alert), "warning", "below_threshold") AS ?alertStatus)
    }
  }
  OPTIONAL {
    ?observation a eutro:Observation ; eutro:waterBody ?water ; eutro:period ?observationPeriod ;
        eutro:chlorophyllUgL ?observedUgL .
    ?observationPeriod eutro:start ?periodStart .
    OPTIONAL { ?observation eutro:obsStatus ?obsStatus . }
  }
  $filters
}
ORDER BY ?periodStart ?waterBody
""")


def parse_period_start(value):
    try:
        start = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("укажи дату в формате YYYY-MM-DD") from error
    if start.day not in (1, 11, 21):
        raise argparse.ArgumentTypeError("начало декады — 1, 11 или 21 число")
    return start


def parse_concentration(value):
    try:
        concentration = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("укажи число в мкг/л") from error
    if not isfinite(concentration) or concentration < 0:
        raise argparse.ArgumentTypeError("концентрация должна быть конечной и неотрицательной")
    return concentration


def water_binding(water_segment, id_literal):
    water = f"<{BASE_IRI}water/{water_segment}>"
    return f"""
  OPTIONAL {{ ?registeredWater eutro:waterBodyId {id_literal} . }}
  BIND(COALESCE(?registeredWater, {water}) AS ?water)
"""


def lake_import_operations(geojson_path):
    source = json.loads(geojson_path.read_text(encoding="utf-8"))
    if (not isinstance(source, dict) or source.get("type") != "FeatureCollection"
            or not isinstance(source.get("features"), list)):
        raise ValueError("ожидается GeoJSON FeatureCollection")

    operations = []
    seen = set()
    for index, feature in enumerate(source["features"], start=1):
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            raise ValueError(f"объект №{index} должен содержать properties")
        water_id, name = properties.get("id"), properties.get("name_ru")
        if not isinstance(water_id, str) or not water_id.strip():
            raise ValueError(f"объект №{index} должен содержать properties.id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"водоём {water_id!r} должен содержать properties.name_ru")
        if water_id in seen:
            raise ValueError(f"повторяющийся ID водоёма {water_id!r}")
        seen.add(water_id)

        water_segment = quote(water_id, safe="")
        id_literal = json.dumps(water_id, ensure_ascii=False)
        name_literal = json.dumps(name, ensure_ascii=False)
        operations.append(f"""
DELETE {{ ?water eutro:name ?currentName . }}
INSERT {{
  ?water a eutro:WaterBody ; eutro:waterBodyId {id_literal} ; eutro:name {name_literal} .
}}
WHERE {{
  {water_binding(water_segment, id_literal)}
  OPTIONAL {{ ?water eutro:name ?currentName . }}
}}
""")
    return operations


def insert_record(phase, water_body_id, period_start, value, name):
    water_segment = quote(water_body_id, safe="")
    last_day = monthrange(period_start.year, period_start.month)[1]
    period_end = period_start.replace(day=last_day if period_start.day == 21 else period_start.day + 9)
    period = f"<{BASE_IRI}period/{period_start}>"
    record_type, detail = {
        "forecast": ("Forecast", f"eutro:usesRule eutro:{ALERT_RULE_ID}"),
        "observe": ("Observation", 'eutro:obsStatus "valid"'),
    }[phase]
    record = f"<{BASE_IRI}{record_type.lower()}/{water_segment}/{period_start}>"
    id_literal = json.dumps(water_body_id, ensure_ascii=False)

    # Keep previously issued records and avoid duplicates when the job is retried.
    operation = f"""
INSERT {{
  ?water a eutro:WaterBody ; eutro:waterBodyId {id_literal} .
  {period} eutro:start "{period_start}"^^xsd:date ; eutro:end "{period_end}"^^xsd:date .
  {record} a eutro:{record_type} ; eutro:waterBody ?water ; eutro:period {period} ;
      eutro:chlorophyllUgL {value} ; {detail} .
}}
WHERE {{
  {water_binding(water_segment, id_literal)}
  OPTIONAL {{
    ?existing a eutro:{record_type} ; eutro:waterBody ?water ; eutro:period ?existingPeriod .
    ?existingPeriod eutro:start "{period_start}"^^xsd:date .
  }}
  FILTER(!BOUND(?existing))
}}
"""
    if name:
        name_literal = json.dumps(name, ensure_ascii=False)
        operation += f""";
INSERT {{ ?water eutro:name {name_literal} . }}
WHERE {{
  ?water eutro:waterBodyId {id_literal} .
  OPTIONAL {{ ?water eutro:name ?existingName . }}
  FILTER(!BOUND(?existingName))
}}
"""
    return operation


def forecast_import_operations(predictions_path):
    source = json.loads(predictions_path.read_text(encoding="utf-8"))
    predictions = source.get("predictions") if isinstance(source, dict) else None
    if not isinstance(predictions, dict):
        raise ValueError("ожидается объект predictions с записями по водоёмам")

    operations = []
    for key, prediction in predictions.items():
        if not isinstance(prediction, dict):
            raise ValueError(f"запись {key!r} должна быть объектом")
        water_id = prediction.get("water_body_id")
        raw_date = prediction.get("date")
        raw_value = prediction.get("chl_a_pred_ug_l")
        if not isinstance(water_id, str) or not water_id.strip():
            raise ValueError(f"запись {key!r}: укажи water_body_id")
        if not isinstance(raw_date, str):
            raise ValueError(f"запись {key!r}: поле date должно содержать дату строкой")
        if type(raw_value) not in (int, float):
            raise ValueError(f"запись {key!r}: поле chl_a_pred_ug_l должно быть числом")
        try:
            period_start = parse_period_start(raw_date)
            value = parse_concentration(str(raw_value))
        except argparse.ArgumentTypeError as error:
            raise ValueError(f"запись {key!r}: {error}") from error
        operations.append(insert_record("forecast", water_id, period_start, value, name=None))
    return operations


def load_graph(graph_path):
    graph = Graph()
    if graph_path.exists():
        graph.parse(graph_path, format="turtle")
    graph.bind("eutro", Namespace(BASE_IRI), replace=True)
    graph.bind("water", Namespace(BASE_IRI + "water/"), replace=True)
    graph.bind("period", Namespace(BASE_IRI + "period/"), replace=True)
    return graph


def update_file(graph_path, *operations):
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = graph_path.with_name(f".{graph_path.name}.lock")
    with FileLock(lock_path, timeout=10):
        graph = load_graph(graph_path)
        for operation in operations:
            graph.update(PREFIXES + operation)

        temporary = NamedTemporaryFile(dir=graph_path.parent, suffix=".ttl", delete=False)
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                serializer = TurtleSerializer(graph)
                serializer.roundtrip_prefixes = ("period",)
                serializer.serialize(temporary)
            temporary_path.replace(graph_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return graph


def oxigraph_post(endpoint, route, content, media_type):
    request = Request(
        f"{endpoint.rstrip('/')}/{route}",
        data=content,
        headers={"Content-Type": media_type, "Accept": "application/sparql-results+json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.read()
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace").strip()
        raise OSError(f"Oxigraph HTTP {error.code}\n{details}") from error


def apply_updates(graph_path, endpoint, *operations):
    if endpoint is None:
        return update_file(graph_path, *operations)
    update = PREFIXES + ";\n".join(operations)
    oxigraph_post(endpoint, "update", update.encode("utf-8"), "application/sparql-update")
    return None


def read_history(graph, water_body_id, *, endpoint=None, period_start=None):
    filters = []
    if water_body_id is not None:
        id_literal = json.dumps(water_body_id, ensure_ascii=False)
        filters.append(f"FILTER (?waterBodyId = {id_literal})")
    period_values = ""
    if period_start is not None:
        period_values = f'VALUES ?periodStart {{ "{period_start}"^^xsd:date }}'
        filters.append("FILTER (BOUND(?predictedUgL))")
    query = PREFIXES + HISTORY.substitute(period_values=period_values, filters="\n  ".join(filters))
    if endpoint is None:
        payload = graph.query(query).serialize(format="json")
    else:
        payload = oxigraph_post(endpoint, "query", query.encode("utf-8"), "application/sparql-query")

    result = json.loads(payload)
    columns = result["head"]["vars"]
    rows = []
    for binding in result["results"]["bindings"]:
        row = {column: binding.get(column, {}).get("value") for column in columns}
        for column in ("predictedUgL", "thresholdUgL", "observedUgL"):
            if row[column] is not None:
                row[column] = float(row[column])
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=("import-ttl", "import-forecasts", "sync-lakes", "forecast", "observe", "history", "get-forecast")
    )
    parser.add_argument("--graph", type=Path, help="путь к Turtle-файлу")
    parser.add_argument("--endpoint", help="адрес Oxigraph, например http://127.0.0.1:7878")
    parser.add_argument("--geojson", type=Path, help="файл контуров для команды sync-lakes")
    parser.add_argument("--input", type=Path, help="JSON для import-forecasts (по умолчанию data/processed/result.json)")
    parser.add_argument("--water-body-id", help="ID из реестра водоёмов")
    parser.add_argument("--name", help="название водоёма при первой записи")
    parser.add_argument("--period-start", type=parse_period_start, help="начало целевой декады")
    parser.add_argument("--value", type=parse_concentration, help="концентрация хлорофилла-а в мкг/л")
    args = parser.parse_args()

    if args.input is not None and args.phase != "import-forecasts":
        parser.error("--input доступен только для import-forecasts")

    record_fields = (args.name, args.period_start, args.value)
    graph_path = (args.graph or DEFAULT_GRAPH).expanduser().resolve()
    if args.phase == "import-ttl":
        if not args.endpoint:
            parser.error("для import-ttl укажи --endpoint")
        if args.geojson is not None or args.water_body_id is not None or any(field is not None for field in record_fields):
            parser.error("import-ttl принимает только --endpoint и --graph")
        try:
            oxigraph_post(args.endpoint, "store?default", graph_path.read_bytes(), "text/turtle")
        except (OSError, ValueError) as error:
            parser.exit(1, f"Ошибка импорта {graph_path} в {args.endpoint}\n{error}\n")
        print(f"Граф загружен в Oxigraph: {args.endpoint}")
        return

    if args.endpoint is not None and args.graph is not None:
        parser.error("выбери одно хранилище: --endpoint или --graph")

    if args.phase == "import-forecasts":
        if not args.endpoint:
            parser.error("для import-forecasts укажи --endpoint")
        if args.geojson is not None or args.water_body_id is not None or any(field is not None for field in record_fields):
            parser.error("import-forecasts принимает только --endpoint и --input")
        predictions_path = (args.input or DEFAULT_PREDICTIONS).expanduser().resolve()
        try:
            operations = forecast_import_operations(predictions_path)
            if operations:
                apply_updates(graph_path, args.endpoint, SEED_RULE, *operations, CREATE_ALERTS)
        except (OSError, ValueError) as error:
            parser.exit(1, f"Ошибка импорта прогнозов из {predictions_path}\n{error}\n")
        print(f"Прогнозов обработано: {len(operations)}")
        return

    if args.phase == "sync-lakes":
        if args.water_body_id is not None or any(field is not None for field in record_fields):
            parser.error("sync-lakes принимает только --geojson, --graph и --endpoint")
        geojson_path = (args.geojson or DEFAULT_GEOJSON).expanduser().resolve()
        try:
            operations = lake_import_operations(geojson_path)
            if operations:
                apply_updates(graph_path, args.endpoint, *operations)
        except (OSError, ValueError, SyntaxError, ParserError, Timeout) as error:
            parser.exit(1, f"Ошибка импорта озёр из {geojson_path}\n{error}\n")
        print(f"Озёр синхронизировано: {len(operations)}")
        return

    if args.geojson is not None:
        parser.error("--geojson доступен только для sync-lakes")
    if args.phase in ("history", "get-forecast"):
        if args.graph is not None or args.name is not None or args.value is not None:
            parser.error(f"для {args.phase} используй Oxigraph, --graph, --name и --value недоступны")
        if args.phase == "get-forecast" and (not args.water_body_id or args.period_start is None):
            parser.error("для get-forecast укажи --water-body-id и --period-start")
        if args.phase == "history" and args.period_start is not None:
            parser.error("для просмотра прогноза за одну декаду используй get-forecast")

        endpoint = args.endpoint or DEFAULT_ENDPOINT
        try:
            rows = read_history(None, args.water_body_id, endpoint=endpoint, period_start=args.period_start)
        except (OSError, ValueError) as error:
            parser.exit(1, f"Ошибка при работе с {endpoint}\n{error}\n")
        if args.phase == "get-forecast":
            if not rows:
                parser.exit(1, f"Прогноз для {args.water_body_id} за декаду с {args.period_start} не найден.\n")
            if len(rows) > 1:
                parser.exit(1, f"Для {args.water_body_id} за декаду с {args.period_start} найдено несколько записей.\n")
            print(json.dumps(rows[0], ensure_ascii=False, indent=2))
        else:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if not args.water_body_id or args.period_start is None or args.value is None:
        parser.error("укажи --water-body-id, --period-start и --value")
    operation = insert_record(args.phase, args.water_body_id, args.period_start, args.value, args.name)
    try:
        if args.phase == "forecast":
            graph = apply_updates(graph_path, args.endpoint, SEED_RULE, operation, CREATE_ALERTS)
        else:
            graph = apply_updates(graph_path, args.endpoint, operation)
        rows = read_history(graph, args.water_body_id, endpoint=args.endpoint)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    except (OSError, ValueError, SyntaxError, ParserError, Timeout) as error:
        parser.exit(1, f"Ошибка при работе с {args.endpoint or graph_path}\n{error}\n")


if __name__ == "__main__":
    main()
