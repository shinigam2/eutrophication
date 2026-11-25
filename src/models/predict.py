import json
from datetime import datetime, timezone
from pathlib import Path
import joblib
import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
SAVED_MODELS_DIR = Path(__file__).resolve().parent / "saved_models"
RESULT_PATH = ROOT_DIR / "data/processed/result.json"


def build_optical_features(df: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """Формирует те же спектральные признаки, на которых обучались модели."""
    bands = ["b02_blue", "b03_green", "b04_red", "b05_rededge1", "b06_rededge2", "b08_nir"]
    out = pd.DataFrame(index=df.index)

    for b in bands:
        col = f"{prefix}{b}"
        if col not in df.columns and f"{b}_current" in df.columns:
            col = f"{b}_current"
        out[b] = pd.to_numeric(df[col], errors="coerce")

    eps = 1e-6
    out["ndci"] = (out["b05_rededge1"] - out["b04_red"]) / (out["b05_rededge1"] + out["b04_red"] + eps)
    out["ndti"] = (out["b04_red"] - out["b03_green"]) / (out["b04_red"] + out["b03_green"] + eps)
    out["b03_b02_ratio"] = out["b03_green"] / (out["b02_blue"].replace(0, np.nan))
    out["b04_b02_ratio"] = out["b04_red"] / (out["b02_blue"].replace(0, np.nan))
    out["b05_b04_ratio"] = out["b05_rededge1"] / (out["b04_red"].replace(0, np.nan))
    out["b06_b04_ratio"] = out["b06_rededge2"] / (out["b04_red"].replace(0, np.nan))

    band_sum = out[bands].sum(axis=1).replace(0, np.nan)
    for b in bands:
        out[f"{b}_norm"] = out[b] / band_sum

    feature_cols = [
        "ndci", "ndti",
        "b03_b02_ratio", "b04_b02_ratio", "b05_b04_ratio", "b06_b04_ratio",
        "b02_blue_norm", "b03_green_norm", "b04_red_norm",
        "b05_rededge1_norm", "b06_rededge2_norm", "b08_nir_norm",
    ]
    return out[feature_cols]


def predict_lake(lake_id: str, date: str | None = None) -> dict:
    """
    Делает прогноз регрессии и классификации для конкретного озера
    по последнему доступному спутниковому снимку (или по указанной дате).
    """
    reg_artifact = joblib.load(SAVED_MODELS_DIR / "regressor.joblib")
    clf_artifact = joblib.load(SAVED_MODELS_DIR / "classifier.joblib")

    sat = pd.read_parquet(ROOT_DIR / "data/processed/features_satellite.parquet")
    lake_df = sat[sat["water_body_id"] == lake_id].sort_values("date")

    if lake_df.empty:
        available = sorted(sat["water_body_id"].unique().tolist())
        raise ValueError(f"Озеро '{lake_id}' не найдено. Доступные: {available}")

    if date is not None:
        row_df = lake_df[lake_df["date"].astype(str) == date]
        if row_df.empty:
            raise ValueError(f"Для озера '{lake_id}' нет данных за дату {date}")
        row_df = row_df.iloc[[-1]]
    else:
        row_df = lake_df.iloc[[-1]]

    X = build_optical_features(row_df, prefix="")

    # 1. Регрессия (концентрация хлорофилла-а + коридор ±MAE)
    chl_pred = float(np.clip(np.expm1(reg_artifact["model"].predict(X)[0]), 0.1, None))
    mae_margin = float(reg_artifact["bsu_mae_ug_l"])
    chl_low = max(0.1, chl_pred - mae_margin)
    chl_high = chl_pred + mae_margin

    # 2. Классификация (вероятность эвтрофного состояния >= 8.0 мкг/л)
    eutrophic_prob = float(clf_artifact["model"].predict_proba(X)[0, 1])
    eutrophic_flag = int(eutrophic_prob >= 0.5)

    row = row_df.iloc[0]
    return {
        "water_body_id": lake_id,
        "date": str(row["date"])[:10],
        "chl_a_pred_ug_l": round(chl_pred, 2),
        "chl_a_pred_low_ug_l": round(chl_low, 2),
        "chl_a_pred_high_ug_l": round(chl_high, 2),
        "eutrophic_threshold_ug_l": clf_artifact["threshold_ug_l"],
        "eutrophic_prob": round(eutrophic_prob, 3),
        "eutrophic_flag": eutrophic_flag,
        "models_info": {
            "regressor": reg_artifact["model_name"],
            "regressor_bsu_mae": reg_artifact["bsu_mae_ug_l"],
            "classifier": clf_artifact["model_name"],
            "classifier_bsu_roc_auc": clf_artifact["bsu_roc_auc"],
        },
    }


def save_predictions_to_json(lake_ids: list[str] | None = None) -> dict:
    """Прогоняет прогноз по выбранным озёрам (или по всем), выводит в консоль и сохраняет result.json."""
    sat = pd.read_parquet(ROOT_DIR / "data/processed/features_satellite.parquet")
    if lake_ids is None:
        lake_ids = sorted(sat["water_body_id"].unique().tolist())

    results = {}
    print("=== ПРОГНОЗ ПО ОЗЁРАМ (GLORIA ML-МОДЕЛИ) ===")
    print(f"{'Озеро':15s} | {'Дата':10s} | {'Хл-а (мкг/л)':14s} | {'Коридор':15s} | {'Риск >=8':8s} | {'Флаг'}")
    print("-" * 80)

    for lid in lake_ids:
        res = predict_lake(lid)
        results[lid] = res
        corridor = f"{res['chl_a_pred_low_ug_l']:.1f}..{res['chl_a_pred_high_ug_l']:.1f}"
        print(
            f"{lid:15s} | {res['date']:10s} | {res['chl_a_pred_ug_l']:14.2f} | "
            f"{corridor:15s} | {res['eutrophic_prob']*100:6.1f}% | {res['eutrophic_flag']}"
        )

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "predictions": results,
    }
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] Результаты сохранены в: {RESULT_PATH}")
    return payload


if __name__ == "__main__":
    # Можно передать конкретное озеро, например: save_predictions_to_json(["batorino"])
    # По умолчанию считает сразу по всем озёрам и записывает в data/processed/result.json
    save_predictions_to_json()