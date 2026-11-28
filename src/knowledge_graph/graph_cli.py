"""Записывает водоёмы, прогнозы и наблюдения и запрашивает прогнозы из Oxigraph."""

import argparse
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
DEFAULT_RESULTS = DATA_DIR / "processed" / "result.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:7878"
PREFIXES = f"""
PREFIX eutro: <{BASE_IRI}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

ALERT_RULE_ID = "chlorophyll-alert"
SEED_RULE = (
    f"INSERT {{ eutro:{ALERT_RULE_ID} eutro:thresholdUgL 25 . }}\n"
    f"WHERE {{ FILTER NOT EXISTS {{ eutro:{ALERT_RULE_ID} eutro:thresholdUgL ?existingThreshold . }} }}"
)
VALUE_OBSERVATION_STATUSES = ("valid", "interpolated_1step")
MISSING_OBSERVATION_STATUSES = ("ice_cover", "cloudy_missing", "insufficient_pixels")
OBSERVATION_STATUSES = VALUE_OBSERVATION_STATUSES + MISSING_OBSERVATION_STATUSES

FORECAST_PATTERN = (
    "{ ?entry a eutro:Forecast ; eutro:waterBody ?water ; "
    "eutro:targetDate ?sliceDate ; eutro:chlorophyllUgL ?entryValue . }"
)
OBSERVATION_PATTERN = (
    "{ ?entry a eutro:Observation ; eutro:waterBody ?water ; eutro:observedDate ?sliceDate . }"
)
SEARCH_PATTERNS = {
    "all": f"{FORECAST_PATTERN} UNION {OBSERVATION_PATTERN}",
    "forecast": FORECAST_PATTERN,
    "observation": OBSERVATION_PATTERN,
}

SEARCH = Template("""
SELECT ?waterBodyId ?waterBody ?baseDate ?targetDate ?observedDate
       ?predictedUgL ?thresholdUgL ?alertStatus ?observedUgL ?obsStatus ?differenceUgL
WHERE {
  {
    SELECT DISTINCT ?water ?sliceDate
    WHERE {
      $date_filters
      $record_pattern
    }
  }
  OPTIONAL { ?water eutro:waterBodyId ?registeredId . }
  OPTIONAL { ?water eutro:name ?name . }
  BIND(COALESCE(?registeredId, STRAFTER(STR(?water), STR(eutro:))) AS ?waterBodyId)
  BIND(COALESCE(?name, ?waterBodyId) AS ?waterBody)
  OPTIONAL {
    ?forecast a eutro:Forecast ; eutro:waterBody ?water ; eutro:targetDate ?sliceDate ;
        eutro:chlorophyllUgL ?predictedUgL .
    BIND(?sliceDate AS ?targetDate)
    OPTIONAL { ?forecast eutro:baseDate ?baseDate . }
    OPTIONAL { ?forecast eutro:usesRule/eutro:thresholdUgL ?thresholdUgL . }
    BIND(IF(
      EXISTS { ?alert a eutro:Alert ; eutro:forForecast ?forecast . },
      "warning", IF(BOUND(?thresholdUgL), "below_threshold", ?unknownAlertStatus)
    ) AS ?alertStatus)
  }
  OPTIONAL {
    ?observation a eutro:Observation ; eutro:waterBody ?water ; eutro:observedDate ?sliceDate .
    BIND(?sliceDate AS ?observedDate)
    OPTIONAL { ?observation eutro:obsStatus ?rawObsStatus . }
    BIND(COALESCE(?rawObsStatus, "valid") AS ?obsStatus)
    OPTIONAL { ?observation eutro:chlorophyllUgL ?measuredUgL . }
    BIND(IF(?obsStatus IN ("valid", "interpolated_1step"), ?measuredUgL, ?missingObservationValue) AS ?observedUgL)
  }
  BIND((?predictedUgL - ?observedUgL) AS ?differenceUgL)
  $filters
}
ORDER BY $sort_direction(?sliceDate) ?waterBody ?waterBodyId
$pagination
""")

def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("укажи дату в формате YYYY-MM-DD") from error


def parse_concentration(value):
    try:
        concentration = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("укажи число в мкг/л") from error
    if not isfinite(concentration) or concentration < 0:
        raise argparse.ArgumentTypeError("концентрация должна быть конечной и неотрицательной")
    return concentration


def load_lakes(geojson_path):
    source = json.loads(geojson_path.read_text(encoding="utf-8"))
    if (not isinstance(source, dict) or source.get("type") != "FeatureCollection"
            or not isinstance(source.get("features"), list)):
        raise ValueError("ожидается GeoJSON FeatureCollection")

    lakes = {}
    for index, feature in enumerate(source["features"], start=1):
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            raise ValueError(f"объект №{index} должен содержать properties")
        water_id, name = properties.get("id"), properties.get("name_ru")
        if not isinstance(water_id, str) or not water_id.strip():
            raise ValueError(f"объект №{index} должен содержать properties.id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"водоём {water_id!r} должен содержать properties.name_ru")
        water_id = water_id.strip()
        if water_id in lakes:
            raise ValueError(f"повторяющийся ID водоёма {water_id!r}")
        lakes[water_id] = name
    return lakes


def lake_sync_operation(lakes):
    rows = "\n".join(
        f"    ({json.dumps(water_id, ensure_ascii=False)} {json.dumps(name, ensure_ascii=False)})"
        for water_id, name in lakes.items()
    )
    return f"""
DELETE {{ ?water eutro:name ?previousName . }}
INSERT {{
  ?water a eutro:WaterBody ; eutro:waterBodyId ?waterId ; eutro:name ?name .
}}
WHERE {{
  VALUES (?waterId ?name) {{
{rows}
  }}
  OPTIONAL {{ ?registeredWater eutro:waterBodyId ?waterId . }}
  BIND(IRI(CONCAT(STR(eutro:), "water/", ENCODE_FOR_URI(?waterId))) AS ?defaultWater)
  BIND(COALESCE(?registeredWater, ?defaultWater) AS ?water)
  OPTIONAL {{ ?water eutro:name ?previousName . }}
}}
"""


def validate_lake_ids(lakes, *, graph=None, endpoint=None):
    query = PREFIXES + "SELECT DISTINCT ?water ?waterId WHERE { ?water eutro:waterBodyId ?waterId . }"
    if endpoint is None:
        payload = graph.query(query).serialize(format="json")
    else:
        payload = oxigraph_post(endpoint, "query", query.encode("utf-8"), "application/sparql-query")

    nodes_by_id = {}
    ids_by_node = {}
    for row in json.loads(payload)["results"]["bindings"]:
        water = row["water"]["value"]
        water_id = row["waterId"]["value"]
        nodes_by_id.setdefault(water_id.strip(), set()).add(water)
        ids_by_node.setdefault(water, set()).add(water_id)
    for water_id in lakes:
        nodes = nodes_by_id.get(water_id, set())
        if len(nodes) > 1:
            raise ValueError(f"ID водоёма {water_id!r} уже принадлежит нескольким узлам графа")
        default_water = f"{BASE_IRI}water/{quote(water_id, safe='')}"
        water = next(iter(nodes), default_water)
        if ids_by_node.get(water, set()) - {water_id}:
            raise ValueError(f"узел для водоёма {water_id!r} уже содержит другой ID")


def insert_records(phase, records):
    record_type, date_property, detail_property = {
        "forecast": ("Forecast", "targetDate", "baseDate"),
        "observe": ("Observation", "observedDate", "obsStatus"),
    }[phase]
    values = []
    for water_body_id, record_date, value, record_detail in records:
        id_literal = json.dumps(water_body_id, ensure_ascii=False)
        value_term = "UNDEF" if value is None else str(value)
        if phase == "forecast":
            detail_term = "UNDEF" if record_detail is None else f'"{record_detail}"^^xsd:date'
        else:
            detail_term = json.dumps(record_detail or "valid")
        values.append(f'    ({id_literal} "{record_date}"^^xsd:date {value_term} {detail_term})')

    delete_alert = insert_alert = match_alert = ""
    if phase == "forecast":
        delete_alert = """
  ?oldAlert a eutro:Alert ; eutro:forForecast ?record ; eutro:triggeredRule ?oldAlertRule .
"""
        insert_alert = """
  ?record eutro:usesRule ?rule .
  ?alert a eutro:Alert ; eutro:forForecast ?record ; eutro:triggeredRule ?rule .
"""
        match_alert = f"""
  OPTIONAL {{ ?record eutro:usesRule ?existingRule . }}
  BIND(COALESCE(?existingRule, eutro:{ALERT_RULE_ID}) AS ?rule)
  OPTIONAL {{ ?rule eutro:thresholdUgL ?threshold . }}
  OPTIONAL {{
    ?oldAlert a eutro:Alert ; eutro:forForecast ?record .
    OPTIONAL {{ ?oldAlert eutro:triggeredRule ?oldAlertRule . }}
  }}
  BIND(IF(
    ?value >= ?threshold, COALESCE(?oldAlert, IRI(CONCAT(STR(?record), "/alert"))), ?noAlert
  ) AS ?alert)
"""

    rows = "\n".join(values)
    return f"""
DELETE {{
  ?record eutro:chlorophyllUgL ?oldValue ; eutro:{detail_property} ?oldDetail .
  {delete_alert}
}}
INSERT {{
  ?water a eutro:WaterBody ; eutro:waterBodyId ?waterId .
  ?record a eutro:{record_type} ; eutro:waterBody ?water ; eutro:{date_property} ?recordDate ;
      eutro:chlorophyllUgL ?value ; eutro:{detail_property} ?recordDetail .
  {insert_alert}
}}
WHERE {{
  VALUES (?waterId ?recordDate ?value ?recordDetail) {{
{rows}
  }}
  BIND(ENCODE_FOR_URI(?waterId) AS ?waterSegment)
  BIND(IRI(CONCAT(STR(eutro:), "water/", ?waterSegment)) AS ?defaultWater)
  OPTIONAL {{ ?registeredWater eutro:waterBodyId ?waterId . }}
  BIND(COALESCE(?registeredWater, ?defaultWater) AS ?water)
  OPTIONAL {{
    ?existing a eutro:{record_type} ; eutro:waterBody ?water ; eutro:{date_property} ?recordDate .
  }}
  BIND(IRI(CONCAT(STR(eutro:), "{record_type.lower()}/", ?waterSegment, "/date/", STR(?recordDate))) AS ?newRecord)
  BIND(COALESCE(?existing, ?newRecord) AS ?record)
  OPTIONAL {{ ?record eutro:chlorophyllUgL ?oldValue . }}
  OPTIONAL {{ ?record eutro:{detail_property} ?oldDetail . }}
  {match_alert}
}}
"""


def insert_record(phase, water_body_id, record_date, value, name, *, base_date=None, obs_status="valid"):
    detail = base_date if phase == "forecast" else obs_status
    operation = insert_records(phase, [(water_body_id, record_date, value, detail)])
    if name:
        id_literal = json.dumps(water_body_id, ensure_ascii=False)
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


def load_results(results_path):
    source = json.loads(results_path.read_text(encoding="utf-8"))
    predictions = source.get("predictions") if isinstance(source, dict) else None
    if not isinstance(predictions, dict):
        raise ValueError("ожидается объект predictions с записями по водоёмам")

    forecasts = {}
    observations = {}
    for key, prediction in predictions.items():
        if not isinstance(prediction, dict):
            raise ValueError(f"запись {key!r} должна быть объектом")
        water_id = prediction.get("water_body_id")
        raw_base_date = prediction.get("base_date")
        raw_target_date = prediction.get("date")
        raw_observed = prediction.get("chl_a_current_ug_l")
        raw_predicted = prediction.get("chl_a_pred_ug_l")
        obs_status = prediction.get("obs_status", "valid")
        if not isinstance(water_id, str) or not water_id.strip():
            raise ValueError(f"запись {key!r}: укажи water_body_id")
        water_id = water_id.strip()
        if obs_status not in OBSERVATION_STATUSES:
            allowed = ", ".join(OBSERVATION_STATUSES)
            raise ValueError(f"запись {key!r}: obs_status должен быть одним из: {allowed}")
        if not isinstance(raw_base_date, str):
            raise ValueError(f"запись {key!r}: base_date должна содержать дату строкой")
        if obs_status in VALUE_OBSERVATION_STATUSES and type(raw_observed) not in (int, float):
            raise ValueError(f"запись {key!r}: для {obs_status} укажи числовое chl_a_current_ug_l")
        if obs_status in MISSING_OBSERVATION_STATUSES and raw_observed is not None:
            raise ValueError(f"запись {key!r}: для {obs_status} значение chl_a_current_ug_l должно быть null")
        if obs_status in MISSING_OBSERVATION_STATUSES and raw_predicted is not None:
            raise ValueError(f"запись {key!r}: для {obs_status} прогноз chl_a_pred_ug_l должен быть null")
        try:
            base_date = parse_date(raw_base_date)
            observed = parse_concentration(str(raw_observed)) if obs_status in VALUE_OBSERVATION_STATUSES else None
            records_to_add = [((water_id, base_date, observed, obs_status), observations)]
            if raw_predicted is not None:
                if type(raw_predicted) not in (int, float) or not isinstance(raw_target_date, str):
                    raise ValueError(f"запись {key!r}: для прогноза укажи дату date и числовое chl_a_pred_ug_l")
                target_date = parse_date(raw_target_date)
                if target_date <= base_date:
                    raise ValueError(f"запись {key!r}: date должна быть позже base_date")
                predicted = parse_concentration(str(raw_predicted))
                records_to_add.append(((water_id, target_date, predicted, base_date), forecasts))
        except argparse.ArgumentTypeError as error:
            raise ValueError(f"запись {key!r}: {error}") from error
        for record, records in records_to_add:
            record_key = record[:2]
            if record_key in records and records[record_key] != record:
                raise ValueError(f"запись {key!r}: противоречивые данные для {water_id!r} на {record[1]}")
            records[record_key] = record
    return list(forecasts.values()), list(observations.values())


def load_graph(graph_path):
    graph = Graph()
    if graph_path.exists():
        graph.parse(graph_path, format="turtle")
    graph.bind("eutro", Namespace(BASE_IRI), replace=True)
    graph.bind("water", Namespace(BASE_IRI + "water/"), replace=True)
    return graph


def update_file(graph_path, *operations, validate=None):
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = graph_path.with_name(f".{graph_path.name}.lock")
    with FileLock(lock_path, timeout=10):
        graph = load_graph(graph_path)
        if validate is not None:
            validate(graph)
        for operation in operations:
            graph.update(PREFIXES + operation)

        temporary = NamedTemporaryFile(dir=graph_path.parent, suffix=".ttl", delete=False)
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                serializer = TurtleSerializer(graph)
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


def _search_date(value, field):
    if value is None or type(value) is date:
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field}: укажи дату в формате YYYY-MM-DD") from error


def _search_concentration(value, field):
    if value is None:
        return None
    try:
        return parse_concentration(str(value))
    except argparse.ArgumentTypeError as error:
        raise ValueError(f"{field}: {error}") from error


def search_records(
    *, record_type="all", water_body_id=None, target_date=None, observed_date=None,
    date_from=None, date_to=None, alert_status=None, obs_status=None,
    predicted_ug_l=None, predicted_ug_l_min=None, predicted_ug_l_max=None,
    observed_ug_l=None, observed_ug_l_min=None, observed_ug_l_max=None,
    sort_order="asc", limit=None, offset=0, endpoint=None, graph=None,
):
    """Ищет записи по фильтрам. Диапазоны дат и концентраций включают границы."""
    if not isinstance(record_type, str) or record_type not in SEARCH_PATTERNS:
        raise ValueError("record_type должен быть all, forecast или observation")
    if sort_order not in ("asc", "desc"):
        raise ValueError("sort_order должен быть asc или desc")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit должен быть неотрицательным целым числом")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset должен быть неотрицательным целым числом")
    if alert_status not in (None, "warning", "below_threshold"):
        raise ValueError("alert_status должен быть warning или below_threshold")
    if obs_status is not None and obs_status not in OBSERVATION_STATUSES:
        allowed = ", ".join(OBSERVATION_STATUSES)
        raise ValueError(f"obs_status должен быть одним из: {allowed}")

    target_date = _search_date(target_date, "target_date")
    observed_date = _search_date(observed_date, "observed_date")
    date_from = _search_date(date_from, "date_from")
    date_to = _search_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("date_from должна быть не позже date_to")

    filters = []
    if water_body_id is not None:
        if not isinstance(water_body_id, str):
            raise ValueError("water_body_id должен быть строкой")
        id_literal = json.dumps(water_body_id, ensure_ascii=False)
        filters.append(f"FILTER (?waterBodyId = {id_literal})")
    if alert_status is not None:
        filters.append(f'FILTER (?alertStatus = "{alert_status}")')
    if obs_status is not None:
        filters.append(f'FILTER (?obsStatus = "{obs_status}")')
    if target_date is not None:
        filters.append(f'FILTER (?targetDate = "{target_date}"^^xsd:date)')
    if observed_date is not None:
        filters.append(f'FILTER (?observedDate = "{observed_date}"^^xsd:date)')
    for field, variable, exact, minimum, maximum in (
        ("predicted_ug_l", "predictedUgL", predicted_ug_l, predicted_ug_l_min, predicted_ug_l_max),
        ("observed_ug_l", "observedUgL", observed_ug_l, observed_ug_l_min, observed_ug_l_max),
    ):
        exact = _search_concentration(exact, field)
        minimum = _search_concentration(minimum, f"{field}_min")
        maximum = _search_concentration(maximum, f"{field}_max")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"{field}_min должен быть не больше {field}_max")
        for operator, value in (("=", exact), (">=", minimum), ("<=", maximum)):
            if value is not None:
                filters.append(f"FILTER (?{variable} {operator} {value})")

    date_filters = []
    exact_date = target_date or observed_date
    if exact_date is not None:
        date_filters.append(f'VALUES ?sliceDate {{ "{exact_date}"^^xsd:date }}')
    if date_from is not None:
        date_filters.append(f'FILTER (?sliceDate >= "{date_from}"^^xsd:date)')
    if date_to is not None:
        date_filters.append(f'FILTER (?sliceDate <= "{date_to}"^^xsd:date)')

    pagination = []
    if limit is not None:
        pagination.append(f"LIMIT {limit}")
    if offset:
        pagination.append(f"OFFSET {offset}")
    query = PREFIXES + SEARCH.substitute(
        record_pattern=SEARCH_PATTERNS[record_type],
        date_filters="\n      ".join(date_filters),
        filters="\n  ".join(filters),
        sort_direction=sort_order.upper(),
        pagination="\n".join(pagination),
    )
    if endpoint is None and graph is not None:
        payload = graph.query(query).serialize(format="json")
    else:
        payload = oxigraph_post(endpoint or DEFAULT_ENDPOINT, "query", query.encode("utf-8"), "application/sparql-query")

    result = json.loads(payload)
    columns = result["head"]["vars"]
    rows = []
    for binding in result["results"]["bindings"]:
        row = {column: binding.get(column, {}).get("value") for column in columns}
        for column in ("predictedUgL", "thresholdUgL", "observedUgL", "differenceUgL"):
            if row[column] is not None:
                row[column] = float(row[column])
        rows.append(row)
    return rows


def read_history(graph, water_body_id, *, endpoint=None, target_date=None):
    return search_records(
        record_type="forecast" if target_date is not None else "all",
        water_body_id=water_body_id, target_date=target_date, endpoint=endpoint, graph=graph,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=("import-ttl", "import-results", "sync-lakes", "forecast", "observe", "history", "get-forecast"),
    )
    parser.add_argument("--graph", type=Path, help="путь к Turtle-файлу")
    parser.add_argument("--endpoint", help="адрес Oxigraph, например http://127.0.0.1:7878")
    parser.add_argument("--geojson", type=Path, help="файл контуров для команды sync-lakes")
    parser.add_argument(
        "--input", type=Path,
        help="JSON для import-results, по умолчанию data/processed/result.json",
    )
    parser.add_argument("--water-body-id", help="ID из реестра водоёмов")
    parser.add_argument("--name", help="название водоёма при первой записи")
    parser.add_argument("--base-date", type=parse_date, help="дата входных данных прогноза")
    parser.add_argument("--target-date", type=parse_date, help="дата, на которую сделан прогноз")
    parser.add_argument("--observed-date", type=parse_date, help="дата среза наблюдения")
    parser.add_argument("--obs-status", choices=OBSERVATION_STATUSES,
                        help="статус для observe, по умолчанию valid")
    parser.add_argument("--value", type=parse_concentration, help="концентрация хлорофилла-а в мкг/л")
    args = parser.parse_args()

    if args.input is not None and args.phase != "import-results":
        parser.error("--input доступен только для import-results")
    if args.obs_status is not None and args.phase != "observe":
        parser.error("--obs-status доступен только для observe")
    water_body_id = args.water_body_id.strip() if args.water_body_id is not None else None
    record_fields = (args.name, args.base_date, args.target_date, args.observed_date, args.value)
    graph_path = (args.graph or DEFAULT_GRAPH).expanduser().resolve()
    if args.phase == "import-ttl":
        if args.geojson is not None or args.water_body_id is not None or any(field is not None for field in record_fields):
            parser.error("import-ttl принимает только --endpoint и --graph")
        endpoint = args.endpoint or DEFAULT_ENDPOINT
        try:
            oxigraph_post(endpoint, "store?default", graph_path.read_bytes(), "text/turtle")
        except (OSError, ValueError) as error:
            parser.exit(1, f"Ошибка импорта {graph_path} в {endpoint}\n{error}\n")
        print(f"Граф загружен в Oxigraph: {endpoint}")
        return

    if args.endpoint is not None and args.graph is not None:
        parser.error("выбери одно хранилище: --endpoint или --graph")

    if args.phase == "import-results":
        if (args.geojson is not None or args.water_body_id is not None
                or any(field is not None for field in record_fields)):
            parser.error("import-results принимает только --endpoint/--graph и --input")
        results_path = (args.input or DEFAULT_RESULTS).expanduser().resolve()
        endpoint = None if args.graph is not None else (args.endpoint or DEFAULT_ENDPOINT)
        try:
            forecasts, observations = load_results(results_path)
            if observations:
                operations = [insert_records("observe", observations)]
                if forecasts:
                    operations.extend((SEED_RULE, insert_records("forecast", forecasts)))
                apply_updates(graph_path, endpoint, *operations)
        except (OSError, ValueError, SyntaxError, ParserError, Timeout) as error:
            parser.exit(1, f"Ошибка импорта результатов из {results_path}\n{error}\n")
        print(f"Обработано прогнозов: {len(forecasts)}. Наблюдений: {len(observations)}.")
        return

    if args.phase == "sync-lakes":
        if args.water_body_id is not None or any(field is not None for field in record_fields):
            parser.error("sync-lakes принимает только --geojson, --graph и --endpoint")
        geojson_path = (args.geojson or DEFAULT_GEOJSON).expanduser().resolve()
        try:
            lakes = load_lakes(geojson_path)
            if lakes:
                operation = lake_sync_operation(lakes)
                if args.endpoint is None:
                    update_file(graph_path, operation, validate=lambda graph: validate_lake_ids(lakes, graph=graph))
                else:
                    validate_lake_ids(lakes, endpoint=args.endpoint)
                    apply_updates(graph_path, args.endpoint, operation)
        except (OSError, ValueError, SyntaxError, ParserError, Timeout) as error:
            parser.exit(1, f"Ошибка импорта озёр из {geojson_path}\n{error}\n")
        print(f"Озёр синхронизировано: {len(lakes)}")
        return

    if args.geojson is not None:
        parser.error("--geojson доступен только для sync-lakes")
    if args.phase in ("history", "get-forecast"):
        if any(field is not None for field in (args.name, args.value, args.base_date, args.observed_date)):
            parser.error(f"{args.phase} принимает --endpoint или --graph и --water-body-id при необходимости")
        if args.phase == "get-forecast" and (not water_body_id or args.target_date is None):
            parser.error("для get-forecast укажи --water-body-id и --target-date")
        if args.phase == "history" and args.target_date is not None:
            parser.error("для поиска по целевой дате используй get-forecast")

        endpoint = None if args.graph is not None else (args.endpoint or DEFAULT_ENDPOINT)
        try:
            local_graph = load_graph(graph_path) if endpoint is None else None
            rows = read_history(local_graph, water_body_id, endpoint=endpoint, target_date=args.target_date)
        except (OSError, ValueError, SyntaxError, ParserError) as error:
            parser.exit(1, f"Ошибка при работе с {endpoint or graph_path}\n{error}\n")
        if args.phase == "get-forecast":
            if not rows:
                parser.exit(1, f"Прогноз для {water_body_id} на {args.target_date} не найден.\n")
            if len(rows) > 1:
                parser.exit(1, f"Для {water_body_id} на {args.target_date} найдено несколько записей.\n")
            print(json.dumps(rows[0], ensure_ascii=False, indent=2))
        else:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if not water_body_id:
        parser.error("укажи --water-body-id")
    obs_status = args.obs_status or "valid"
    if args.phase == "forecast":
        if args.base_date is None or args.target_date is None or args.value is None or args.observed_date is not None:
            parser.error("для forecast укажи --base-date, --target-date и --value")
        if args.target_date <= args.base_date:
            parser.error("--target-date должна быть позже --base-date")
        record_date = args.target_date
    else:
        if args.observed_date is None or args.base_date is not None or args.target_date is not None:
            parser.error("для observe укажи --observed-date")
        if obs_status in VALUE_OBSERVATION_STATUSES and args.value is None:
            parser.error(f"для observe со статусом {obs_status} укажи --value")
        if obs_status in MISSING_OBSERVATION_STATUSES and args.value is not None:
            parser.error(f"для observe со статусом {obs_status} убери --value")
        record_date = args.observed_date
    operation = insert_record(
        args.phase, water_body_id, record_date, args.value, args.name,
        base_date=args.base_date, obs_status=obs_status,
    )
    endpoint = None if args.graph is not None else (args.endpoint or DEFAULT_ENDPOINT)
    try:
        if args.phase == "forecast":
            graph = apply_updates(graph_path, endpoint, SEED_RULE, operation)
        else:
            graph = apply_updates(graph_path, endpoint, operation)
        rows = read_history(graph, water_body_id, endpoint=endpoint)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    except (OSError, ValueError, SyntaxError, ParserError, Timeout) as error:
        parser.exit(1, f"Ошибка при работе с {endpoint or graph_path}\n{error}\n")


if __name__ == "__main__":
    main()
