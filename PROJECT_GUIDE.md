# Путеводитель по проекту: архитектура и сквозная работа 4 частей

Этот документ описывает фактическую архитектуру работающей системы мониторинга и прогнозирования эвтрофикации водоёмов Беларуси: из каких модулей состоит каждая из 4 частей, какие данные передаются между ними и как устроен сквозной поток от сырых спутниковых сцен до веб-ГИС.

---

## 1. Сквозная схема работы системы

```text
[1. Внешние источники]
  • OSM (полигоны 8 озёр, буфер -25 м)
  • Sentinel-2 L2A COG (Element84 STAC, 7 каналов B02..B11, 2018–2026)
  • NASA POWER MERRA-2 (суточная погода, 2018–2026, 0% пропусков)
  • GLORIA (5 405 мировых оптико-лабораторных пар Rrs -> Chl-a)
  • Пробы Нарочанской биостанции БГУ (36 проб 2018–2019 + 112 проб Нарочи 2004–2018)
                              │
                              ▼
[2. Часть 1 — ETL и витрины (src/etl/)]
  • data/reference/lakes.geojson
  • data/interim/satellite_decadal.parquet (2 488 декад, статусы valid / interpolated_1step / cloudy_missing / ice_cover)
  • data/processed/features_satellite.parquet (1 140 валидных декад × 54 колонки, 826 обучающих пар)
  • data/processed/features_lab.parquet (36 проб БГУ × 44 колонки)
  • data/interim/data_quality_report.json, data/processed/dataset_passport.json
  • PostGIS (load_to_postgis.py -> таблицы water_bodies, meteo_daily, satellite_decadal, features_*)
                              │
                              ▼
[3. Часть 2 — Двухэтапное ML (src/models/)]
  • Модель 1 (regressor.py -> Ridge, обучена на 4 737 парах GLORIA, проверена zero-shot на 36 пробах БГУ)
      → восстанавливает Chl-a_current по B02, B03, B04 с ограниченной блик-коррекцией по B11
      → build_forecast_dataset.py собирает data/processed/features_forecast_10d.parquet (1 140 × 58)
  • Модель 2 (forecaster.py -> RandomForest, 19 признаков, train 2018–2024 [599] / test 2025–2026 [227])
      → прогнозирует Chl-a на +10 дней, коридор ±MAE (±1,82 мкг/л), P(≥15) и P(≥25)
      → predict.py формирует data/processed/result.json и data/processed/model_metrics.json
                              │
                              ▼
[4. Часть 3 — Граф знаний RDF/SPARQL (src/knowledge_graph/graph_cli.py)]
  • data/knowledge_graph/graph.ttl + Rust-сервер Oxigraph (:7878) / локальный rdflib
  • Хранит eutro:WaterBody, eutro:Observation, eutro:Forecast, eutro:Alert
  • Пороговое SPARQL-правило eutro:chlorophyll-alert (eutro:thresholdUgL = 25 мкг/л)
      → формирует статус warning (RED, ≥ 25) или below_threshold (GREEN, < 25)
                              │
                              ▼
[5. Часть 4 — REST API и веб-ГИС (src/api/, frontend/)]
  • FastAPI загружает витрины Части 1, реальные модели Части 2 (TwoStageModelPredictor)
    и реальный SPARQL-движок Части 3 (GraphCliEngine)
  • Вычисляет трофический индекс Карлсона TSI = 9,81 * ln(Chl-a) + 30,6
  • Отдаёт REST API (/lakes, /timeline, /predict/{id}, /history/{id}, /metrics, /lab/upload)
    и интерактивную карту MapLibre GL JS + графики Chart.js на /dashboard/
```

---

## 2. Состав и роль каждой части

### Часть 1 — Сбор, очистка данных, геометрия и хранилище (`src/etl/`)
Отвечает за получение, валидацию и унификацию всех исходных данных:
- [`configs/water_bodies.yaml`](configs/water_bodies.yaml) — единый реестр водоёмов: **8 активных** (`naroch`, `myastro`, `batorino`, `myadel`, `kuzmichi`, `volchino`, `lotviny`, `rudakovo`) и **1 резервный** (`rossokhi`, `enabled: false`). Шесть сверхмалых/заросших озёр (`knyagininskoe`, `khodosy`, `chertok`, `telyakovskoe`, `barsuki`, `repyakhovskoe`) документально отсеяны на этапе радиометрической проверки (см. [`data/interim/data_quality_report.json`](data/interim/data_quality_report.json)).
- [`src/etl/osm_geometry.py`](src/etl/osm_geometry.py) — загрузка реальных полигонов береговой линии и островов из OpenStreetMap, построение внутреннего буфера **−25 м** (исключение мелководья, камыша и смешанных пикселей берега) и сохранение в [`data/reference/lakes.geojson`](data/reference/lakes.geojson).
- [`src/etl/sentinel_fetcher.py`](src/etl/sentinel_fetcher.py) — скачивание и агрегация 7 спектральных каналов Sentinel-2 L2A (`B02, B03, B04, B05, B06, B08, B11`) с маской `SCL == 6` (чистая вода) и коррекцией сдвига Baseline `04.00` (`−1000` DN). Итого в архиве **2 224** сцен за `2018–2026` гг.
- [`src/etl/meteo_fetcher.py`](src/etl/meteo_fetcher.py) — суточная метеоистория **NASA POWER** (`25 928` строк, `2018-01-01 .. 2026-11-15`, `0` пропусков).
- [`src/etl/gloria_loader.py`](src/etl/gloria_loader.py) — приведение гиперспектрального датасета GLORIA к каналам Sentinel-2 (**5 405** очищенных пар после удаления отрицательных `b06`/`b08` и некорректных `secchi ≤ 0`).
- [`src/etl/lab_loader.py`](src/etl/lab_loader.py) — загрузка и валидация реальных проб БГУ ([`data/lab/lab_measurements.csv`](data/lab/lab_measurements.csv), 36 записей 2018–2019 гг.) и исторического поверхностного ряда Нарочи ([`data/lab/naroch_surface_2004_2018.csv`](data/lab/naroch_surface_2004_2018.csv), 112 записей).
- [`src/etl/validate_data.py`](src/etl/validate_data.py) — фильтрация атмосферной дымки (`B11 > 0,06`), отсечение 1-DN артефактов Sen2Cor, сборка декадной сетки (`1, 11, 21` числа каждого месяца, всего **2 488** декад: `1 140 valid`, `768 ice_cover`, `386 cloudy_missing`, `194 interpolated_1step`) и генерация `data_quality_report.json`.
- [`src/etl/build_features.py`](src/etl/build_features.py) — сборка витрин `features_satellite.parquet` (**1 140** валидных строк × **54** колонки, из них **826** с валидной целевой декадой `t + 10д` без утечки будущего) и `features_lab.parquet` (**36** строк × **44** колонки, `100%` сопоставление со Sentinel-2 в окне `±10` дней), а также паспорта `dataset_passport.json`.
- [`src/etl/load_to_postgis.py`](src/etl/load_to_postgis.py) — идемпотентная загрузка полигонов и витрин в геопространственную БД **PostgreSQL + PostGIS** (`compose.yaml`).

### Часть 2 — Двухэтапное машинное обучение (`src/models/`)
Подробности и обоснование формул вынесены в [`PART2_ML_GUIDE.md`](PART2_ML_GUIDE.md). Кратко:
- [`src/models/features.py`](src/models/features.py) — единый расчёт 7 оптических признаков по видимым каналам (`ndti`, `b03_b02_ratio`, `b04_b02_ratio`, `b04_b03_ratio`, `b02/b03/b04_norm`) с ограниченной блик-коррекцией `glint = min(0,8 * B11, 0,5 * min(B02, B03, B04))` для спутниковых сцен.
- [`src/models/regressor.py`](src/models/regressor.py) — обучение **Модели 1** (`Ridge` на `log1p(chl_a)`) на **4 737** парах GLORIA (`≤ 60 мкг/л`, без `'other'`) и честная zero-shot проверка на **36** пробах БГУ: `MAE = 2,76 мкг/л`, `RMSE = 3,215`, `Spearman ρ = +0,814` (против формулы Mishra: `7,796 / 8,758 / −0,252`). Сохраняет `saved_models/regressor.joblib`.
- [`src/models/build_forecast_dataset.py`](src/models/build_forecast_dataset.py) — прогоняет Модель 1 по всем 1 140 декадам витрины, формирует `chl_a_current_ug_l`, защищённые лаги (`<= 12` дней) `chl_a_lag10d` / `chl_a_delta_10d` и целевую переменную `target_chl_a_next_10d`, сохраняя `data/processed/features_forecast_10d.parquet` (`1 140 × 58`).
- [`src/models/forecaster.py`](src/models/forecaster.py) — обучение **Модели 2** (`RandomForest` на `log1p`, 19 признаков) на периоде **2018–2024** (`599` строк) и тест на **2025–2026** (`227` строк): `MAE = 1,821 мкг/л`, `RMSE = 2,744`, `Spearman ρ = +0,643`, `ROC-AUC(≥15) = 0,858` (против наивной инерции `2,593 / 3,625 / +0,477`). Сохраняет `saved_models/forecaster.joblib` и `data/processed/model_metrics.json`.
- [`src/models/predict.py`](src/models/predict.py) — пакетный и точечный инференс (`--lake`, `--date`) со сверкой по полной декадной ленте `satellite_decadal.parquet`: для `valid`-декад считает прогноз `+10 д`, а для `ice_cover` / `cloudy_missing` / `insufficient_pixels` честно записывает `obs_status` и `null` в [`data/processed/result.json`](data/processed/result.json).

### Часть 3 — Граф знаний и пороговый вывод (`src/knowledge_graph/graph_cli.py`)
- Онтология и стартовые факты хранятся в [`data/knowledge_graph/graph.ttl`](data/knowledge_graph/graph.ttl) в пространстве имён `eutro: <https://shinigam2.github.io/eutrophication/>` и `water: <https://shinigam2.github.io/eutrophication/water/>`.
- Работает как с живым Rust-сервером **Oxigraph** (`--endpoint http://127.0.0.1:7878` или `http://oxigraph:7878` внутри Docker), так и автономно поверх локального TTL-файла (`--graph data/knowledge_graph/graph.ttl`) через `rdflib`.
- Поддерживает команды CLI:
  - `import-ttl` — загрузка исходного графа в Oxigraph;
  - `sync-lakes` — синхронизация 8 водоёмов из `data/reference/lakes.geojson`;
  - `import-results` — импорт наблюдений (включая пропуски `cloudy_missing` / `ice_cover` без числа) и прогнозов из `data/processed/result.json`;
  - `observe` / `forecast` — идемпотентный upsert одиночных записей;
  - `history` / `get-forecast` / `search` — SPARQL-выборка истории, прогноза и фильтрация записей (16 фильтров).
- Правило оповещения `eutro:chlorophyll-alert` задаёт порог `eutro:thresholdUgL = 25` мкг/л (условный `SEED_RULE` с `FILTER NOT EXISTS` не перезаписывает вручную изменённый порог). Результат SPARQL-вывода — **2 уровня**: `warning` (`RED`, прогноз $\ge 25$ мкг/л) и `below_threshold` (`GREEN`, прогноз $< 25$ мкг/л).
- Статусы наблюдений едины с Частью 1: `valid`, `interpolated_1step`, `ice_cover`, `cloudy_missing`, `insufficient_pixels`.

### Часть 4 — REST API и интерактивная веб-ГИС (`src/api/`, `frontend/`)
- [`src/api/adapters.py`](src/api/adapters.py):
  - `Part1ParquetProvider` — читает витрины `features_satellite.parquet` / `features_lab.parquet` и полную декадную ленту `satellite_decadal.parquet` (для корректного показа облачных и ледовых декад на временной шкале).
  - `TwoStageModelPredictor` — загружает `regressor.joblib` и `forecaster.joblib` и выполняет тот же расчёт `Chl-a`, прогноза `+10 д`, коридора `±MAE` и вероятностей `P(≥15)` / `P(≥25)`, что и `src/models/predict.py` (расхождение `0,000 мкг/л` по всем 8 озёрам).
  - `GraphCliEngine` — вызывает реальный SPARQL-движок Части 3 (`graph_cli.py`), синхронизирует порог `threshold_ug_l` с живым Oxigraph при старте, добавляет расчёт трофического индекса Карлсона (`TSI = 9,81 * ln(Chl-a) + 30,6`) и возвращает строго реальное SPARQL-объяснение без выдуманных рекомендаций.
- [`src/api/pipeline.py`](src/api/pipeline.py) и [`src/api/main.py`](src/api/main.py) — FastAPI-сервер с автоматическим перезачитыванием файлов Части 1 при их обновлении, заголовком `Cache-Control: no-cache` для `/dashboard/`, сжатием GZip и эндпоинтами `/`, `/lakes` (`/water-bodies`), `/timeline`, `/predict/{id}`, `/history/{id}`, `/metrics`, `POST /lab/upload`.
- [`src/api/check_integration.py`](src/api/check_integration.py) — консольная диагностика подключения всех реальных компонентов и прогон по всем 8 озёрам.
- [`frontend/`](frontend/) (`index.html`, `app.js`, `style.css`) — интерактивная карта MapLibre GL JS (полигоны озёр, внутренние буферы `−25 м`, стрелки гидрологического каскада из `water_bodies.yaml`, слои фактических значений `Chl-a` / `TSI` / `Спутник %`, ползунок дат 2018–2026), карточка выбранного озера с прогнозом `+10 д`, коридором `±MAE`, вердиктом графа знаний и графиком Chart.js (факт, прогноз `+10 д`, маркеры реальных проб БГУ), модальные окна метрик, справки и загрузки лабораторной пробы.

---

## 3. Текущий статус интеграции

| Этап | Содержание | Статус |
| :---: | :--- | :---: |
| 1 | Геометрия OSM, буфер −25 м, радиометрическая проверка, 8 активных озёр | Выполнено |
| 2 | Архивы Sentinel-2 L2A (2018–2026), NASA POWER, GLORIA, пробы БГУ | Выполнено |
| 3 | Валидация, декадная сетка, витрины `features_satellite` / `features_lab`, PostGIS | Выполнено |
| 4 | Модель 1 (`Ridge` на GLORIA, сверка БГУ) и Модель 2 (`RandomForest`, OOT 2025–2026) | Выполнено |
| 5 | RDF/SPARQL-граф знаний (`graph_cli.py`, `graph.ttl`, Oxigraph, `import-results`) | Выполнено |
| 6 | Интеграция Частей 1–3 в FastAPI и веб-ГИС без заглушек | Выполнено |
| 7 | Автоматические тесты (41 тест: 39 локально + 2 живых PostGIS/Oxigraph) | Выполнено |

---

## 4. Границы применимости и направления развития

В текущей версии сознательно **не реализованы** (и не имитируются заглушками):
1. **Отдельная ML-модель прогноза по лабораторным пробам** — для обучения не хватает объёма данных (всего 36 проб БГУ за 2 года по 3 озёрам); режим `lab` работает как фактический просмотр и ввод проб.
2. **Расширенная онтология и текстовые рекомендации в графе знаний** — правила запрета химической обработки в ООПТ, каскадного переноса цветения между озёрами, промежуточный жёлтый уровень тревоги, словарь W3C SOSA/SSN и SHACL-валидация (`pyshacl`) могут быть добавлены в будущей итерации.
3. **Постоянно запущенный Apache Airflow и MLflow** — в репозитории лежит шаблон DAG [`dags/eutrophication_etl_dag.py`](dags/eutrophication_etl_dag.py), но в `compose.yaml` тяжёлый кластер не поднимается ради экономии ресурсов.
4. **Оценка токсичности воды и подлёдный/облачный мониторинг** — Sentinel-2 физически ограничен облаками, льдом и верхним оптическим слоем воды; определение цианотоксинов (микроцистинов) остаётся задачей лабораторного анализа.
