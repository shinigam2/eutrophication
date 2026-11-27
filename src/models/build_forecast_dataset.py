from pathlib import Path
import joblib
import numpy as np
import pandas as pd

try:
    from src.models.features import build_optical_features
except ImportError:
    from features import build_optical_features

ROOT_DIR = Path(__file__).resolve().parents[2]
SAVED_MODELS_DIR = Path(__file__).resolve().parent / "saved_models"
OUTPUT_DATASET_PATH = ROOT_DIR / "data/processed/features_forecast_10d.parquet"


def main() -> None:
    reg_artifact = joblib.load(SAVED_MODELS_DIR / "regressor.joblib")
    sat = pd.read_parquet(ROOT_DIR / "data/processed/features_satellite.parquet")

    df = sat.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["water_body_id", "date"]).reset_index(drop=True)

    # 1. Прогоняем Модель 1 (обученную на GLORIA) по всем снимкам Sentinel-2
    X_opt = build_optical_features(df, prefix="", subtract_glint=True)
    df["chl_a_current_ug_l"] = np.clip(np.expm1(reg_artifact["model"].predict(X_opt)), 0.1, None)

    # 2. Лаг за прошлую декаду (-10 дней) и динамика внутри каждого озера (строго <= 12 дн., без перескока через зиму/облака)
    prev_date = df.groupby("water_body_id")["date"].shift(1)
    prev_ok = (df["date"] - prev_date).dt.days <= 12
    df["chl_a_lag10d"] = df.groupby("water_body_id")["chl_a_current_ug_l"].shift(1).where(prev_ok, np.nan)
    df["chl_a_delta_10d"] = df["chl_a_current_ug_l"] - df["chl_a_lag10d"]

    # 3. Целевое значение через +10 дней (строго по target_date_next_10d без утечки)
    if "target_date_next_10d" in df.columns:
        df["target_date_next_10d"] = pd.to_datetime(df["target_date_next_10d"])
        lookup = df[["water_body_id", "date", "chl_a_current_ug_l"]].rename(
            columns={
                "date": "target_date_next_10d",
                "chl_a_current_ug_l": "target_chl_a_next_10d",
            }
        )
        df = df.merge(lookup, on=["water_body_id", "target_date_next_10d"], how="left")
    else:
        next_date = df.groupby("water_body_id")["date"].shift(-1)
        days_diff = (next_date - df["date"]).dt.days
        df["target_date_next_10d"] = next_date
        df["target_chl_a_next_10d"] = np.where(
            days_diff.between(5, 15),
            df.groupby("water_body_id")["chl_a_current_ug_l"].shift(-1),
            np.nan,
        )

    df.to_parquet(OUTPUT_DATASET_PATH, index=False)

    trainable_mask = (df["is_trainable_target"] == True) & df["target_chl_a_next_10d"].notna()
    print("=== ДАТАСЕТ ДЛЯ МОДЕЛИ 2 СФОРМИРОВАН ===")
    print(f"Всего строк: {len(df)} | Валидных для обучения (+10д): {int(trainable_mask.sum())}")
    print("\nПроверка адекватности текущей концентрации по озёрам (мкг/л):")
    summary = df.groupby("water_body_id")["chl_a_current_ug_l"].agg(["count", "min", "median", "max"])
    print(summary.round(2).to_string())
    print(f"\n[OK] Датасет сохранён в: {OUTPUT_DATASET_PATH}")


if __name__ == "__main__":
    main()