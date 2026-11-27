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
    """predict_lake() валидирует YYYY-MM-DD, привязывает дату в пределах <=10 дн., а result.json грузится в Часть 3."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        predict_lake("naroch", "2026-08")

    with pytest.raises(ValueError):
        predict_lake("naroch", "2010-01-01")

    snapped = predict_lake("naroch", "2026-08-05")
    assert snapped["base_date"] == "2026-08-01"
    assert snapped["date"] == "2026-08-11"
    assert 0.0 <= snapped["bloom_prob"] <= snapped["attention_prob"] <= 1.0

    res_json = json.loads((ROOT / "data/processed/result.json").read_text(encoding="utf-8"))
    for lid, rec in res_json["predictions"].items():
        live = predict_lake(lid, rec["base_date"])
        assert live["chl_a_current_ug_l"] == rec["chl_a_current_ug_l"]
        assert live["chl_a_pred_ug_l"] == rec["chl_a_pred_ug_l"]
        assert live["attention_prob"] == rec["attention_prob"]
        assert live["bloom_prob"] == rec["bloom_prob"]

    # Прямая проверка контракта Часть 2 -> Часть 3 (SPARQL-импорт result.json в памяти)
    from src.knowledge_graph import graph_cli

    g = graph_cli.load_graph(ROOT / "data/knowledge_graph/graph.ttl")
    ops = graph_cli.forecast_import_operations(ROOT / "data/processed/result.json")
    assert len(ops) == len(res_json["predictions"]) == 8
    for op in ops:
        g.update(op)
    g.update(graph_cli.PREFIXES + graph_cli.CREATE_ALERTS)
    for lid, rec in res_json["predictions"].items():
        rows = graph_cli.read_history(g, lid, period_start=date.fromisoformat(rec["date"]))
        assert rows and any(abs(float(r["predictedUgL"]) - rec["chl_a_pred_ug_l"]) < 0.01 for r in rows)
