# Единый контракт данных и интерфейсов (DATA CONTRACT v2.0)

Этот документ фиксирует фактические форматы данных, схемы таблиц, JSON-артефактов, RDF/SPARQL-графа и REST API, по которым взаимодействуют все 4 реализованные части системы.

---

## 1. Общие соглашения

### 1.1. Идентификаторы водоёмов (`water_body_id`)
Единый реестр задаётся в [`configs/water_bodies.yaml`](configs/water_bodies.yaml). Активные озёра (`8` шт., `enabled: true`):

| `water_body_id` | Название | Группа | Площадь (км²) | Ср. / макс. глубина (м) | Лабораторные пробы БГУ |
| :--- | :--- | :--- | :---: | :---: | :---: |
| `naroch` | Озеро Нарочь | Нарочанская | 79,60 | 8,9 / 24,8 | Да (12 проб 2018–2019 + 112 2004–2018) |
| `myastro` | Озеро Мястро | Нарочанская | 13,10 | 5,4 / 11,3 | Да (12 проб 2018–2019) |
| `batorino` | Озеро Баторино | Нарочанская | 6,25 | 2,4 / 5,5 | Да (12 проб 2018–2019) |
| `myadel` | Озеро Мядель | Мядельская | 16,20 | 6,3 / 24,6 | Нет |
| `kuzmichi` | Озеро Кузьмичи | Мядельская | 0,68 | 3,0 / 4,9 | Нет |
| `volchino` | Озеро Волчино | Мядельская | 0,53 | 1,6 / 3,2 | Нет |
| `lotviny` | Озеро Лотвины | Мядельская | 0,42 | 6,2 / 15,1 | Нет |
| `rudakovo` | Озеро Рудаково | Мядельская | 0,24 | 11,3 / 28,6 | Нет |

Резервный водоём: `rossokhi` (`enabled: false`, не участвует в текущих витринах и инференсе).

### 1.2. Даты и декадная сетка
- Формат всех дат во всех файлах, SPARQL и API: ISO 8601 `YYYY-MM-DD` (`xsd:date` в RDF).
- Единая декадная сетка спутникового мониторинга: **`1`, `11` и `21` число каждого месяца** (`2018-04-01 .. 2026-11-11`).
- Горизонт прогнозирования Модели 2: **1 декада (`+10` календарных дней)**.

### 1.3. Статусы наблюдений (`obs_status`)
Единый словарь статусов в Части 1 (`satellite_decadal.parquet`), Части 3 (`graph_cli.py`: `OBSERVATION_STATUSES`) и Части 4 (`schemas.py`):
- `valid` — есть реальный безоблачный снимок Sentinel-2 за декаду; числовое значение обязательно;
- `interpolated_1step` — заполнение одиночного 1-декадного пропуска в `satellite_decadal.parquet` (исключено из `features_satellite.parquet` и обучения); числовое значение присутствует;
- `cloudy_missing` — облачность/дымка в открытую воду; значение `null`/отсутствует;
- `ice_cover` — зимний ледостав (ноябрь–март); значение `null`/отсутствует;
- `insufficient_pixels` — недостаточно чистых водных пикселей (`< 20` или `< 15%` площади); значение `null`/отсутствует.

---

## 2. Выход Части 1 → Вход Частей 2, 3 и 4

### 2.1. Геометрия: `data/reference/lakes.geojson`
GeoJSON `FeatureCollection` с полигонами (`MultiPolygon`/`Polygon`, WGS-84) и свойствами `water_body_id`, `name`, `geometry_kind` (`water_polygon` и `inner_buffer_25m`), `area_km2`, `centroid_lat`, `centroid_lon`, `bbox`. Используется:
- Частью 1 (`sentinel_fetcher.py`, `load_to_postgis.py`),
- Частью 3 (`graph_cli.py sync-lakes`),
- Частью 4 (`adapters.py` для отображения полигонов и внутренних буферов `−25 м` на карте).

### 2.2. Спутниковая витрина: `data/processed/features_satellite.parquet` (и `.csv`)
- Объём: **1 140** строк (`obs_status == "valid"`, `2018-04-11 .. 2026-11-11`), **54** колонки.
- Обучающих строк с валидной следующей декадой (`is_trainable_target == True`): **826**.
- Ключевые группы колонок (полный перечень хранится в [`data/processed/dataset_passport.json`](data/processed/dataset_passport.json)):
  - **Метаданные (12):** `water_body_id`, `water_body_name`, `date`, `obs_status`, `valid_water_pixels`, `water_coverage_pct`, `scenes_in_decade`, `sun_glint_flag`, `last_scene_id`, `last_scene_date`, `is_trainable_target`, `target_date_next_10d`.
  - **Сезонность и морфометрия (8):** `month`, `day_of_year`, `sin_doy`, `cos_doy`, `area_km2`, `mean_depth_m`, `max_depth_m`, `watershed_ratio`.
  - **Оптика Sentinel-2 (7 каналов + индексы):** `b02_blue`, `b03_green`, `b04_red`, `b05_rededge1`, `b06_rededge2`, `b08_nir`, `b11_swir1`, `ndci_current`, `ndci_lag10d`, `ndci_growth_10d`, `ndti_current`, `b03_b02_ratio_current`, `b04_b02_ratio_current`, `b04_b02_ratio_lag10d`, `b04_b02_growth_10d`, `b05_b04_ratio_current`, `chl_a_est_current` *(бейзлайн Mishra, с отсечением вершины при `NDCI >= -0,2216`)*, `chl_a_est_lag10d`, `tsi_current`.
  - **Погодные окна NASA POWER (10, строго `<= date`):** `temp_mean_7d`, `temp_mean_14d`, `temp_mean_21d`, `temp_max_7d`, `hot_days_14d`, `precip_sum_7d`, `precip_sum_14d`, `radiation_mean_7d`, `wind_mean_7d`, `calm_fraction_7d`.
  - **Базовые цели (5):** `target_ndci_next_10d`, `target_b04_b02_next_10d`, `target_chl_a_est_next_10d`, `target_attention_risk_next_10d`, `target_bloom_risk_next_10d`.

### 2.3. Полная декадная лента: `data/interim/satellite_decadal.parquet`
- Объём: **2 488** строк × **23** колонки (`1 140 valid`, `768 ice_cover`, `386 cloudy_missing`, `194 interpolated_1step`).
- Используется Частью 4 (`Part1ParquetProvider`) для честного показа облачных и ледовых декад на временной шкале и графике.

### 2.4. Лабораторная витрина: `data/processed/features_lab.parquet` (и `.csv`)
- Объём: **36** строк × **44** колонки (`naroch`, `myastro`, `batorino`, `2018-05-15 .. 2019-10-15`, 100% совпадение со Sentinel-2 в окне `±10` дней).
- Целевая колонка: `chl_a_lab_ug_l`; сопутствующие: `secchi_depth_m`, `total_phosphorus_ug_l`, `date` (дата пробы), `date_precision`, `s2_days_diff`, оптические каналы `s2_b02_blue .. s2_b11_swir1` и погодные окна.
- Исходный CSV для дозаписи новых проб через API: [`data/lab/lab_measurements.csv`](data/lab/lab_measurements.csv).

### 2.5. Мировой эталон GLORIA: `data/interim/gloria_sentinel2_pairs.parquet`
- Объём: **5 405** очищенных пар × **27** колонок (`b02_blue..b08_nir`, `chl_a_ug_l`, `secchi_depth_m`, `water_type` и др.).

### 2.6. Отчёты качества и PostGIS
- [`data/interim/data_quality_report.json`](data/interim/data_quality_report.json) — покрытие по озёрам, отсеянные сцены и озёра (читается в `GET /metrics`).
- [`data/processed/dataset_passport.json`](data/processed/dataset_passport.json) — метаданные витрин и рекомендуемое временное разбиение.
- PostGIS (`src/etl/load_to_postgis.py`): таблицы `water_bodies` (`GEOMETRY(MultiPolygon, 4326)` с GIST-индексом), `meteo_daily`, `satellite_decadal`, `features_satellite`, `features_lab`, `lab_measurements`, `dataset_passports`.

---

## 3. Выход Части 2 → Вход Частей 3 и 4

### 3.1. Сохранённые модели (`src/models/saved_models/`)
- `regressor.joblib` — словарь-бандл Модели 1:
  - `model` (`Pipeline(SimpleImputer → StandardScaler → Ridge)`), `model_name`, `feature_cols` (7 оптических признаков), `target_transform: "log1p"`, `max_train_chl: 60.0`, `bsu_metrics`.
- `forecaster.joblib` — словарь-бандл Модели 2:
  - `model` (`RandomForestRegressor`), `model_name`, `feature_cols` (19 признаков), `target_transform: "log1p"`, `test_mae_ug_l` (`1.821`), `log_residual_std` (`0.3153`), `attention_threshold_ug_l` (`15.0`), `bloom_threshold_ug_l` (`25.0`), `top_drivers`, `test_metrics`.

### 3.2. Витрина с оценками Модели 1: `data/processed/features_forecast_10d.parquet`
- Объём: **1 140** строк × **58** колонок (`features_satellite` + `chl_a_current_ug_l`, `chl_a_lag10d`, `chl_a_delta_10d`, `target_chl_a_next_10d`).

### 3.3. Метрики: `data/processed/model_metrics.json`
Содержит два верхнеуровневых блока:
- `model_1_current_chlorophyll`: `winner`, `train_rows` (`4737`), `bsu_mae_ug_l` (`2.76`), `bsu_rmse_ug_l` (`3.215`), `bsu_spearman_rho` (`0.814`), `mishra_baseline`, `candidates` (`Ridge`, `HistGB`).
- `model_2_forecast_10d`: `winner`, `train_period` (`2018-2024`, `599`), `test_period` (`2025-2026`, `227`), `test_mae_ug_l` (`1.821`), `test_rmse_ug_l` (`2.744`), `test_spearman_rho` (`0.643`), `attention_15_roc_auc` (`0.858`), `naive_baseline`, `candidates` (`Ridge`, `RandomForest`, `HistGB`), `top_drivers`.

### 3.4. Пакет прогнозов: `data/processed/result.json` (контракт Часть 2 → Часть 3)
Формируется `python -m src.models.predict` и импортируется командой `graph_cli.py import-results`:

```json
{
  "generated_at_utc": "2026-11-28T13:18:19Z",
  "forecast_horizon_days": 10,
  "predictions": {
    "batorino": {
      "water_body_id": "batorino",
      "base_date": "2026-11-11",
      "obs_status": "valid",
      "chl_a_current_ug_l": 6.3,
      "date": "2026-11-21",
      "chl_a_pred_ug_l": 7.24,
      "chl_a_pred_low_ug_l": 5.42,
      "chl_a_pred_high_ug_l": 9.06,
      "attention_threshold_ug_l": 15.0,
      "bloom_threshold_ug_l": 25.0,
      "attention_prob": 0.018,
      "bloom_prob": 0.0,
      "weather_context": { "temp_mean_14d": 3.6, "calm_fraction_7d": 0.57, "precip_sum_14d": 5.5 },
      "top_drivers": ["max_depth_m", "mean_depth_m", "precip_sum_14d"]
    },
    "rudakovo": {
      "water_body_id": "rudakovo",
      "base_date": "2026-11-11",
      "obs_status": "cloudy_missing",
      "chl_a_current_ug_l": null,
      "date": null,
      "chl_a_pred_ug_l": null,
      "chl_a_pred_low_ug_l": null,
      "chl_a_pred_high_ug_l": null,
      "attention_threshold_ug_l": 15.0,
      "bloom_threshold_ug_l": 25.0,
      "attention_prob": null,
      "bloom_prob": null,
      "weather_context": { "temp_mean_14d": null, "calm_fraction_7d": null, "precip_sum_14d": null },
      "top_drivers": []
    }
  }
}
```
Для пропусков (`ice_cover`, `cloudy_missing`, `insufficient_pixels`) Часть 2 честно передаёт `obs_status` и `null`, а `graph_cli.py import-results` сохраняет в граф знаний наблюдение без числа и без прогноза, сохраняя непрерывную историю.

---

## 4. Контракт Части 3 (RDF / SPARQL-граф знаний)

### 4.1. Пространства имён и сущности ([`data/knowledge_graph/graph.ttl`](data/knowledge_graph/graph.ttl))
- `@prefix eutro: <https://shinigam2.github.io/eutrophication/> .`
- `@prefix water: <https://shinigam2.github.io/eutrophication/water/> .`
- Классы и свойства:
  - `water:<id> a eutro:WaterBody ; eutro:waterBodyId "<id>" ; eutro:name "<Название>" .`
  - `eutro:chlorophyll-alert eutro:thresholdUgL 25 .` *(условный seed — не перезаписывает настроенный порог)*
  - `eutro:Observation` — свойства: `eutro:aboutWaterBody`, `eutro:observedDate` (`xsd:date`), `eutro:obsStatus`, опционально `eutro:chlorophyllUgL` (`xsd:decimal`, обязательно для `valid` и `interpolated_1step`, запрещено для `cloudy_missing`/`ice_cover`/`insufficient_pixels`).
  - `eutro:Forecast` — свойства: `eutro:aboutWaterBody`, `eutro:baseDate` (`xsd:date`), `eutro:targetDate` (`xsd:date`, строго `> baseDate`), `eutro:chlorophyllUgL` (`xsd:decimal`), `eutro:hasAlert` → `eutro:Alert`.
  - `eutro:Alert` — свойства: `eutro:level` (`"warning"` если прогноз $\ge$ `thresholdUgL`, иначе `"below_threshold"`), `eutro:thresholdUgL`.

### 4.2. JSON-формат выдачи CLI (`history`, `get-forecast`, `search`)
```json
{
  "water_body_id": "myastro",
  "name": "Озеро Мястро",
  "kind": "forecast",
  "base_date": "2026-11-11",
  "target_date": "2026-11-21",
  "chlorophyll_ug_l": 5.09,
  "alert_level": "below_threshold",
  "threshold_ug_l": 25.0
}
```
Для наблюдений (`"kind": "observation"`): возвращаются `observed_date`, `obs_status` и опционально `chlorophyll_ug_l`.

---

## 5. Контракт Части 4 (REST API — `src/api/main.py` и `src/api/schemas.py`)

| Метод и путь | Назначение | Ключевые поля ответа |
| :--- | :--- | :--- |
| `GET /` | Статус сервиса и подключённых частей | `status`, `horizon_days` (`10`), `components` (`part1/part2/part3`), `modes`, `files` |
| `GET /lakes` *(алиас `/water-bodies`)* | Список 8 озёр, паспорта и геометрия | `id`, `name`, `geometry`, `buffer_geometry`, `area_km2`, `mean_depth_m`, `max_depth_m`, `watershed_ratio`, `protected_area`, `cascade_inflow_from`, `cascade_outflow_to`, `attention_ug_l` (`15`), `bloom_ug_l` (`25`), `modes` |
| `GET /timeline?mode=satellite\|lab` | Полная временная лента 2018–2026 для карты | `dates[]`, `by_date[date][lake_id]` (`chl`, `obs_chl`, `tsi`, `obs_tsi`, `alert`, `prob`, `obs_status`, `is_interpolated`, `forecast_date`, `has_forecast`, `dc`) |
| `GET /predict/{id}?date=YYYY-MM-DD&mode=satellite\|lab` | Прогноз на `+10 д` и вывод графа знаний (`AlertResponse`) | `date_used`, `chl_a_current`, `obs_status`, `observed_tsi`, `observed_trophic_class`, `chl_a_forecast`, `pred_low_ug_l`, `pred_high_ug_l`, `uncertainty_ug_l`, `bloom_probability`, `attention_probability`, `trophic_index`, `trophic_class`, `alert_level` (`GREEN` \| `RED` \| `null`), `reasons[]`, `recommendations[]` *(пустой список в реальном режиме)*, погодные и оптические поля, `observation`, `sources[]`, `notes[]` |
| `GET /history/{id}?mode=satellite\|lab&date=...&limit=120` | История наблюдений и прогнозов (`HistoryResponse`) | `points[]`: `date`, `observed`, `obs_status`, `is_interpolated`, `forecast`, `forecast_low`, `forecast_high` |
| `GET /metrics` | Метрики ML и отчёт качества данных | Содержимое `model_metrics.json` + `data_quality` + `modes` |
| `POST /lab/upload` | Добавление новой пробы в `data/lab/lab_measurements.csv` | Валидация диапазонов (`0 < chl <= 500`, `0 < secchi <= 50`, `0 < tp <= 1000`, запрет будущих дат и дублей), сохранение и мгновенная сверка со спутником (`satellite_comparison`) |
| `GET /dashboard/` | Статические файлы веб-ГИС панели | HTML/CSS/JS с заголовком `Cache-Control: no-cache` |

### Шкала трофического индекса Карлсона (TSI, вычисляется в `GraphCliEngine`)
$$\text{TSI}(\text{Chl-a}) = 9{,}81 \cdot \ln(\text{Chl-a}) + 30{,}6$$

| Диапазон TSI | Диапазон Chl-a (мкг/л) | Трофический класс в API (`trophic_class`) |
| :---: | :---: | :--- |
| `< 40` | `< 2,6` | `олиготрофный` |
| `40 .. 50` | `2,6 .. 7,3` | `мезотрофный` |
| `50 .. 70` | `7,3 .. 56,0` | `эвтрофный` |
| `≥ 70` | `≥ 56,0` | `гиперэвтрофный` |
