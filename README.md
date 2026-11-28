# Интеллектуальная система мониторинга, прогнозирования и раннего предупреждения риска эвтрофикации водоёмов Беларуси

Программный комплекс объединяет спутниковый мониторинг **Sentinel-2 L2A**, погодные данные **NASA POWER**, мировой оптико-лабораторный эталон **GLORIA**, реальные гидробиологические пробы **Нарочанской биостанции БГУ**, двухэтапное машинное обучение, **RDF/SPARQL-граф знаний** (Oxigraph / `rdflib`) и интерактивную **веб-ГИС панель** (FastAPI + MapLibre GL JS + Chart.js).

Объект исследования — **8 активных озёр** Нарочанской и Мядельской групп (`Нарочь`, `Мястро`, `Баторино`, `Мядель`, `Кузьмичи`, `Волчино`, `Лотвины`, `Рудаково`; `Россохи` оставлено в резерве как `enabled: false`, ещё 6 малых озёр отсеяны на радиометрической проверке из-за зарастания и недостатка чистых водных пикселей). Временной охват спутникового архива — **2018–2026 гг.**

---

## Архитектура системы (4 интегрированные части)

| Часть | Назначение | Основные модули и артефакты | Статус |
| :---: | :--- | :--- | :---: |
| **1** | **ETL, геометрия, валидация, витрины, PostGIS** | [`src/etl/`](src/etl/), [`configs/water_bodies.yaml`](configs/water_bodies.yaml), [`data/reference/lakes.geojson`](data/reference/lakes.geojson), `features_satellite.parquet` (`1 140 × 54`, `826` обучающих), `features_lab.parquet` (`36 × 44`), `gloria_sentinel2_pairs.parquet` (`5 405 × 27`), [`src/etl/load_to_postgis.py`](src/etl/load_to_postgis.py) | Реализовано |
| **2** | **Двухэтапное ML (оценка Chl-a + прогноз +10 д)** | [`src/models/features.py`](src/models/features.py), [`regressor.py`](src/models/regressor.py), [`build_forecast_dataset.py`](src/models/build_forecast_dataset.py), [`forecaster.py`](src/models/forecaster.py), [`predict.py`](src/models/predict.py), `saved_models/{regressor,forecaster}.joblib`, `features_forecast_10d.parquet`, `model_metrics.json`, `result.json` | Реализовано |
| **3** | **RDF/SPARQL-граф знаний и пороговый вывод** | [`src/knowledge_graph/graph_cli.py`](src/knowledge_graph/graph_cli.py), [`data/knowledge_graph/graph.ttl`](data/knowledge_graph/graph.ttl), сервис `oxigraph` в [`compose.yaml`](compose.yaml) (Rust SPARQL 1.1) + локальный `rdflib` | Реализовано |
| **4** | **REST API и интерактивная веб-ГИС** | [`src/api/main.py`](src/api/main.py), [`pipeline.py`](src/api/pipeline.py), [`adapters.py`](src/api/adapters.py), [`check_integration.py`](src/api/check_integration.py), [`frontend/`](frontend/) (MapLibre GL JS + Chart.js) | Реализовано |

Все 4 части работают **напрямую на реальных моделях и графе знаний без заглушек**: API загружает сохранённые модели Части 2 через `TwoStageModelPredictor` и обращается к SPARQL-движку Части 3 через `GraphCliEngine` (живой Oxigraph по `OXIGRAPH_URL` или локальный `rdflib` по `graph.ttl`).

---

## Подтверждённые метрики качества моделей

Значения зафиксированы в [`data/processed/model_metrics.json`](data/processed/model_metrics.json):

### Этап 1 (Модель 1) — оценка текущего `Chl-a` по оптике Sentinel-2 (`B02, B03, B04` + блик-коррекция по `B11`)
- Обучение: **4 737** пар из мирового датасета **GLORIA** (`Chl-a ≤ 60 мкг/л`, без категории `'other'`).
- Независимая zero-shot проверка: **36** реальных лабораторных проб БГУ (`Нарочь`, `Мястро`, `Баторино`, 2018–2019 гг.; на пробах БГУ модель **не обучалась**).

| Модель | MAE (мкг/л) | RMSE (мкг/л) | Spearman $\rho$ |
| :--- | :---: | :---: | :---: |
| Бейзлайн Mishra & Mishra (2012) по `NDCI` | 7,80 | 8,76 | −0,252 |
| HistGradientBoosting (кандидат) | 2,84 | 4,24 | +0,742 |
| **Ridge (выбранная Модель 1)** | **2,76** | **3,22** | **+0,814** |

### Этап 2 (Модель 2) — прогноз `Chl-a` на `+10 дней` вперёд
- Разделение строго по времени без утечки будущего: обучение **2018–2024** (`599` декад), независимый тест **2025–2026** (`227` декад), 19 признаков (текущий `Chl-a`, лаг и прирост за 10 дней, погодные окна 7/14/21 д, морфометрия озера, сезонность).

| Модель | MAE (мкг/л) | RMSE (мкг/л) | Spearman $\rho$ | ROC-AUC ($\ge 15$ мкг/л) |
| :--- | :---: | :---: | :---: | :---: |
| Наивная инерция ($y_{t+10} = y_t$) | 2,59 | 3,63 | +0,477 | — |
| Ridge (кандидат) | 2,12 | 3,08 | +0,482 | 0,797 |
| HistGradientBoosting (кандидат) | 1,91 | 2,83 | +0,625 | 0,845 |
| **RandomForest (выбранная Модель 2)** | **1,82** | **2,74** | **+0,643** | **0,858** |

Доверительный коридор прогноза строится как $\pm\text{MAE}$ (`±1,82 мкг/л`), вероятности превышения порогов `P(≥15)` и `P(≥25)` оцениваются в пространстве $\ln(1 + \text{Chl-a})$ через нормальное распределение остатков (`log_residual_std = 0,3153`).

---

## Быстрый старт

### Вариант 1: Локальный запуск (Python `.venv`, самый быстрый способ)

```bash
# 1. Создать окружение и установить зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Проверить интеграцию всех 4 частей
python -m src.api.check_integration

# 3. Запустить веб-сервер FastAPI и веб-ГИС панель
uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

После запуска откройте в браузере:
- **Веб-ГИС панель:** `http://127.0.0.1:8000/dashboard/`
- **Swagger / OpenAPI документация:** `http://127.0.0.1:8000/docs`
- **Статус компонентов:** `http://127.0.0.1:8000/`
- **Метрики моделей и качества данных:** `http://127.0.0.1:8000/metrics`

*(Если сервер Oxigraph на `:7878` не запущен, `GraphCliEngine` автоматически работает на том же SPARQL-запросе поверх локального [`data/knowledge_graph/graph.ttl`](data/knowledge_graph/graph.ttl) через `rdflib`.)*

### Вариант 2: Полный запуск в Docker Compose (`Oxigraph + PostGIS + FastAPI`)

```bash
# 1. Поднять граф знаний Oxigraph (:7878) и БД PostGIS (:5432)
docker compose up -d oxigraph postgis

# 2. Загрузить витрины Части 1 в PostGIS
docker compose --profile etl run --rm etl python -m src.etl.load_to_postgis

# 3. Загрузить онтологию, озёра и актуальные прогнозы в Oxigraph
docker compose --profile etl run --rm etl python -m src.knowledge_graph.graph_cli import-ttl --endpoint http://oxigraph:7878 --input data/knowledge_graph/graph.ttl
docker compose --profile etl run --rm etl python -m src.knowledge_graph.graph_cli sync-lakes --endpoint http://oxigraph:7878
docker compose --profile etl run --rm etl python -m src.knowledge_graph.graph_cli import-results --endpoint http://oxigraph:7878 --input data/processed/result.json

# 4. Запустить API + веб-ГИС с подключением к живому Oxigraph
docker compose --profile api up -d --build api
```

Веб-панель будет доступна по адресу `http://127.0.0.1:8000/dashboard/`, SPARQL-эндпоинт Oxigraph — на `http://127.0.0.1:7878`.

*(Если на хосте активен VPN с перехватом маршрутов до подсети Docker, запускайте интеграционные команды внутри Docker-сети через `docker compose --profile etl run --rm etl ...` или используйте локальный режим `.venv`.)*

---

## Полная пересборка пайплайна с нуля

Все готовые витрины, обученные модели и результаты уже лежат в репозитории. При обновлении исходных данных полный цикл пересчитывается командами:

```bash
# Часть 1: пересборка витрин из скачанных сырых архивов (или --stage all для скачивания с нуля)
python -m src.etl.run_pipeline --stage validate,features

# Часть 2: переобучение Модели 1, сборка датасета прогноза, переобучение Модели 2 и генерация result.json
python -m src.models.regressor
python -m src.models.build_forecast_dataset
python -m src.models.forecaster
python -m src.models.predict

# Часть 3: обновление локального TTL-графа (или с --endpoint http://127.0.0.1:7878 для Oxigraph)
python -m src.knowledge_graph.graph_cli sync-lakes --graph data/knowledge_graph/graph.ttl
python -m src.knowledge_graph.graph_cli import-results --graph data/knowledge_graph/graph.ttl --input data/processed/result.json

# Часть 4: проверка сквозной совместимости
python -m src.api.check_integration
```

---

## Тестирование

В проекте реализовано **41 автоматический тест** (39 выполняются локально без Docker + 2 интеграционных теста против живых PostGIS и Oxigraph):

```bash
# Все локальные модульные и интеграционные тесты (39 passed)
pytest -v

# Живые тесты PostGIS + Oxigraph внутри Docker-сети (8 passed, 0 skipped)
docker compose --profile etl run --rm \
  -e DATABASE_URL=postgresql://eutro:eutro@postgis:5432/eutrophication \
  -e OXIGRAPH_URL=http://oxigraph:7878 \
  etl pytest tests/test_postgis_and_compose.py tests/test_graph_upsert.py -v
```

---

## Структура репозитория

```text
eutrophication/
├── configs/
│   └── water_bodies.yaml                 # Паспорта 8 активных (+1 резервного) озёр, пороги 15/25 мкг/л
├── dags/
│   └── eutrophication_etl_dag.py         # Шаблон Apache Airflow DAG (для развёртывания при наличии кластера)
├── data/
│   ├── reference/lakes.geojson           # Полигоны озёр (OSM) и внутренние буферы -25 м
│   ├── lab/
│   │   ├── lab_measurements.csv          # 36 реальных проб БГУ (Нарочь, Мястро, Баторино, 2018–2019)
│   │   └── naroch_surface_2004_2018.csv  # 112 исторических поверхностных проб Нарочи (2004–2018)
│   ├── interim/                          # Промежуточные слои ETL: satellite_scenes/decadal, meteo_clean, GLORIA, отчёт качества
│   ├── processed/
│   │   ├── features_satellite.{parquet,csv}  # Витрина Части 1 (1 140 валидных декад × 54 колонки)
│   │   ├── features_lab.{parquet,csv}        # Лабораторная витрина (36 строк × 44 колонки)
│   │   ├── features_forecast_10d.parquet     # Витрина Части 2 с оценками Модели 1 (1 140 × 58)
│   │   ├── dataset_passport.json             # Машиночитаемый паспорт витрин
│   │   ├── model_metrics.json                # Метрики Модели 1 и Модели 2 + бейзлайны
│   │   └── result.json                       # Актуальные прогнозы Части 2 по 8 озёрам (вход для Части 3)
│   └── knowledge_graph/
│       └── graph.ttl                     # Онтология и граф знаний Части 3 (Turtle)
├── src/
│   ├── etl/                              # Часть 1: OSM, Sentinel-2, NASA POWER, БГУ, GLORIA, валидация, витрины, PostGIS
│   ├── models/                           # Часть 2: features, regressor, build_forecast_dataset, forecaster, predict + saved_models/
│   ├── knowledge_graph/graph_cli.py      # Часть 3: CLI и SPARQL-движок (Oxigraph / rdflib)
│   └── api/                              # Часть 4: FastAPI (main, pipeline, adapters, config, schemas, stubs, check_integration)
├── frontend/                             # Веб-ГИС панель (index.html, app.js, style.css)
├── tests/                                # 41 автотест (ETL, ML, KG, PostGIS, API, динамика дашборда)
├── compose.yaml                          # Единая Docker-инфраструктура (oxigraph + postgis + etl + api)
├── Dockerfile                            # Контейнер Python + GDAL/Rasterio для etl и api
├── requirements.txt                      # Зависимости Python
└── *.md                                  # Документация: PROJECT_GUIDE, DATA_CONTRACT, PART2_ML_GUIDE, DECISIONS, PROFESSOR_SPEC_ALIGNMENT
```

---

## Честные границы текущей реализации

- **Режим «Лаборатория» (`mode=lab`):** отображает реальные лабораторные пробы БГУ (36 записей 2018–2019 гг.) и принимает новые пробы через `POST /lab/upload` с проверкой физических диапазонов и сверкой со спутником. Отдельная ML-модель прогноза на одних только пробах **не обучалась** из-за малого объёма выборки (`N = 36`), о чём API и интерфейс сообщают явно.
- **Граф знаний Части 3:** реализует хранение водоёмов, наблюдений и прогнозов в RDF/SPARQL и пороговое правило предупреждения по `eutro:thresholdUgL` (`25 мкг/л`, 2 уровня: `GREEN` / `below_threshold` и `RED` / `warning`). Текстовые рекомендации по мероприятиям, правила режима ООПТ, каскадный перенос между озёрами, SHACL-валидация и словарь W3C SOSA/SSN в текущей версии **не реализованы** и отнесены к направлениям развития системы.
- **Спутниковые ограничения:** Sentinel-2 не пробивает облака и зимний лёд (в эти декады честно выставляется статус `cloudy_missing` / `ice_cover`, прогноз не выдумывается), оценивает верхний оптический слой воды (`~0,5–2 м`) и не определяет токсичность цианобактерий (микроцистины проверяются только в лаборатории).

---

## Документация проекта

- [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md) — сквозная карта архитектуры, потока данных и всех 4 частей.
- [`DATA_CONTRACT.md`](DATA_CONTRACT.md) — формальный контракт таблиц, JSON-файлов, RDF/SPARQL и REST API.
- [`PART2_ML_GUIDE.md`](PART2_ML_GUIDE.md) — детальное описание реализованных ML-моделей, метрик и разбор расхождений с пробами БГУ.
- [`DECISIONS.md`](DECISIONS.md) — журнал ключевых научных и архитектурных решений (ADR-1..ADR-14).
- [`PROFESSOR_SPEC_ALIGNMENT.md`](PROFESSOR_SPEC_ALIGNMENT.md) — честное сопоставление реализации с исходным техническим заданием.
