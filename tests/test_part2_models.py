"""Тесты Части 2 (ML-модели): оптика без обвала на тёмных озёрах, лаги <=12 дн., predict.py и стыковка с Частью 3."""
from datetime import date
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest

from src.models.features import OPTICAL_FEATURE_COLS, build_optical_features
from src.models.predict import predict_lake

ROOT = Path(__file__).resolve().parents[1]


def test_glint_subtraction_bounded_on_dark_lakes():
    """Вычитание SWIR-блика ограничено 0.5*min(B02,B03,B04) и не обнуляет видимые каналы тёмных озёр."""
    df = pd.DataFrame([
        # Тёмное малое озеро с умеренным SWIR (ранее обваливало B02/B04 в пол 0.001 и давало ложное цветение)
        {"b02_blue": 0.0020, "b03_green": 0.0040, "b04_red": 0.0030, "b11_swir1": 0.0150},
        # Чистое озеро с низким SWIR
        {"b02_blue": 0.0120, "b03_green": 0.0180, "b04_red": 0.0090, "b11_swir1": 0.0020},
    ])
    feats = build_optical_features(df, prefix="", subtract_glint=True)
    assert list(feats.columns) == OPTICAL_FEATURE_COLS
    assert np.isfinite(feats.to_numpy()).all()
    # Для тёмного озера glint ограничен 0.5*0.002 = 0.001 -> b02=0.001, b04=0.002 -> b04/b02 = 2.0 (не артефакт!)
    assert 1.5 <= float(feats.loc[0, "b04_b02_ratio"]) <= 2.5
    assert 2.5 <= float(feats.loc[0, "b03_b02_ratio"]) <= 3.5


def test_forecast_dataset_no_stale_lags_or_targets():
    """В features_forecast_10d.parquet нет лагов/целей через зиму или облачные пропуски (>12 дн.) и ложных весенних пиков."""
    sat = pd.read_parquet(ROOT / "data/processed/features_satellite.parquet")
    fc = pd.read_parquet(ROOT / "data/processed/features_forecast_10d.parquet")
    assert len(fc) == len(sat)
    assert set(fc["water_body_id"]) == set(sat["water_body_id"])

    fc["date_dt"] = pd.to_datetime(fc["date"])
    prev_dt = fc.groupby("water_body_id")["date_dt"].shift(1)
    next_dt = fc.groupby("water_body_id")["date_dt"].shift(-1)

    stale_lags = fc["chl_a_lag10d"].notna() & ((fc["date_dt"] - prev_dt).dt.days > 12)
    stale_targets = fc["target_chl_a_next_10d"].notna() & ((next_dt - fc["date_dt"]).dt.days > 12)
    assert int(stale_lags.sum()) == 0
    assert int(stale_targets.sum()) == 0

    spring_false_blooms = fc[(fc["date_dt"].dt.month.isin([4, 5])) & (fc["chl_a_current_ug_l"] >= 25.0)]
    assert len(spring_false_blooms) == 0


def test_model_metrics_and_artifacts_consistent():
    """Сохранённые joblib-артефакты согласованы с model_metrics.json, а Модель 2 превосходит наивную инерцию."""
    reg = joblib.load(ROOT / "src/models/saved_models/regressor.joblib")
    fc = joblib.load(ROOT / "src/models/saved_models/forecaster.joblib")
    metrics = json.loads((ROOT / "data/processed/model_metrics.json").read_text(encoding="utf-8"))

    m1 = metrics["model_1_current_chlorophyll"]
    m2 = metrics["model_2_forecast_10d"]
    assert m1["winner"] == reg["model_name"]
    assert m1["bsu_mae_ug_l"] == reg["bsu_mae_ug_l"]
    assert m1["bsu_spearman_rho"] == reg["bsu_spearman_rho"]
    assert m1["bsu_mae_ug_l"] < m1["mishra_baseline"]["mae_ug_l"]

    assert m2["winner"] == fc["model_name"]
    assert m2["test_mae_ug_l"] == fc["test_mae_ug_l"]
    assert m2["attention_15_roc_auc"] == fc["attention_15_roc_auc"]
    assert m2["test_mae_ug_l"] < m2["naive_baseline"]["mae_ug_l"]
    assert m2["test_rmse_ug_l"] < m2["naive_baseline"]["rmse_ug_l"]
    assert m2["attention_15_roc_auc"] >= 0.75


def test_predict_lake_date_validation_and_part3_import():
    """predict_lake() валидирует YYYY-MM-DD, честно отдаёт пропуски/интерполяции, а result.json грузится в Часть 3."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        predict_lake("naroch", "2026-08")

    with pytest.raises(ValueError):
        predict_lake("naroch", "2010-01-01")

    snapped = predict_lake("naroch", "2026-08-05")
    assert snapped["base_date"] == "2026-08-01"
    assert snapped["obs_status"] == "valid"
    assert snapped["date"] == "2026-08-11"
    assert 0.0 <= snapped["bloom_prob"] <= snapped["attention_prob"] <= 1.0

    # Зимний пропуск (ice_cover): дата и статус фиксируются, числа и прогноз = None
    winter = predict_lake("naroch", "2025-01-11")
    assert winter["base_date"] == "2025-01-11"
    assert winter["obs_status"] == "ice_cover"
    assert winter["chl_a_current_ug_l"] is None and winter["chl_a_pred_ug_l"] is None and winter["date"] is None

    # Одношаговая интерполяция: числовое наблюдение от Модели 1, но без прогноза на +10д
    interp = predict_lake("naroch", "2026-07-11")
    assert interp["base_date"] == "2026-07-11"
    assert interp["obs_status"] == "interpolated_1step"
    assert isinstance(interp["chl_a_current_ug_l"], float) and interp["chl_a_current_ug_l"] > 0
    assert interp["chl_a_pred_ug_l"] is None and interp["date"] is None

    res_json = json.loads((ROOT / "data/processed/result.json").read_text(encoding="utf-8"))
    for lid, rec in res_json["predictions"].items():
        live = predict_lake(lid, rec["base_date"])
        assert live["obs_status"] == rec["obs_status"]
        assert live["chl_a_current_ug_l"] == rec["chl_a_current_ug_l"]
        assert live["chl_a_pred_ug_l"] == rec["chl_a_pred_ug_l"]
        assert live["attention_prob"] == rec["attention_prob"]
        assert live["bloom_prob"] == rec["bloom_prob"]

    # Прямая проверка контракта Часть 2 -> Часть 3 (load_results + insert_records + search_records в памяти)
    from src.knowledge_graph import graph_cli

    g = graph_cli.load_graph(ROOT / "data/knowledge_graph/graph.ttl")
    forecasts, observations = graph_cli.load_results(ROOT / "data/processed/result.json")
    expected_fc = sum(1 for r in res_json["predictions"].values() if r["chl_a_pred_ug_l"] is not None)
    assert len(observations) == len(res_json["predictions"]) == 8
    assert len(forecasts) == expected_fc
    assert any(r["obs_status"] == "cloudy_missing" for r in res_json["predictions"].values())
    g.update(graph_cli.PREFIXES + graph_cli.SEED_RULE)
    g.update(graph_cli.PREFIXES + graph_cli.insert_records("observe", observations))
    g.update(graph_cli.PREFIXES + graph_cli.insert_records("forecast", forecasts))
    for lid, rec in res_json["predictions"].items():
        obs_rows = graph_cli.search_records(
            graph=g,
            record_type="observation",
            water_body_id=lid,
            observed_date=date.fromisoformat(rec["base_date"]),
        )
        assert len(obs_rows) == 1
        assert obs_rows[0]["obsStatus"] == rec["obs_status"]
        if rec["chl_a_pred_ug_l"] is not None:
            rows = graph_cli.search_records(
                graph=g,
                record_type="forecast",
                water_body_id=lid,
                target_date=date.fromisoformat(rec["date"]),
            )
            assert len(rows) == 1
            assert abs(float(rows[0]["predictedUgL"]) - rec["chl_a_pred_ug_l"]) < 0.01
            assert rows[0]["baseDate"] == rec["base_date"]
            assert rows[0]["alertStatus"] in ("warning", "below_threshold")
        else:
            assert obs_rows[0]["observedUgL"] is None
            fc_rows = graph_cli.search_records(graph=g, record_type="forecast", water_body_id=lid)
            assert fc_rows == []
