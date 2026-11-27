import json
from datetime import datetime, timezone
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, roc_auc_score, root_mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

try:
    from lightgbm import LGBMRegressor
except ImportError:
    LGBMRegressor = None

ROOT_DIR = Path(__file__).resolve().parents[2]
SAVED_MODELS_DIR = Path(__file__).resolve().parent / "saved_models"
DATASET_PATH = ROOT_DIR / "data/processed/features_forecast_10d.parquet"
METRICS_PATH = ROOT_DIR / "data/processed/model_metrics.json"

CANDIDATE_FEATURES = [
    # Текущее состояние и динамика хлорофилла (от Модели 1)
    "chl_a_current_ug_l",
    "chl_a_lag10d",
    "chl_a_delta_10d",
    # Погодные окна (без утечек будущего)
    "temp_mean_7d",
    "temp_mean_14d",
    "temp_mean_21d",
    "temp_max_7d",
    "hot_days_14d",
    "precip_sum_7d",
    "precip_sum_14d",
    "radiation_mean_7d",
    "wind_mean_7d",
    "calm_fraction_7d",
    # Свойства озера
    "area_km2",
    "mean_depth_m",
    "max_depth_m",
    "watershed_ratio",
    # Сезонность
    "sin_doy",
    "cos_doy",
]


def extract_top_drivers(model, feature_cols: list[str], top_n: int = 5) -> list[str]:
    """Извлекает наиболее важные факторы модели для объяснения прогноза."""
    estimator = model.named_steps["model"] if isinstance(model, Pipeline) else model
    if hasattr(estimator, "feature_importances_"):
        importances = np.asarray(estimator.feature_importances_, dtype=float)
    elif hasattr(estimator, "coef_"):
        importances = np.abs(np.asarray(estimator.coef_, dtype=float)).ravel()
    else:
        return feature_cols[:top_n]

    order = np.argsort(importances)[::-1]
    return [feature_cols[i] for i in order[:top_n]]


def main() -> None:
    SAVED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(DATASET_PATH)
    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year

    trainable = df[(df["is_trainable_target"] == True) & df["target_chl_a_next_10d"].notna()].copy()
    feature_cols = [c for c in CANDIDATE_FEATURES if c in trainable.columns]

    # Строгое временное разбиение без перемешивания
    train_df = trainable[trainable["year"] <= 2024]
    test_df = trainable[trainable["year"] >= 2025]

    X_train = train_df[feature_cols]
    y_train_log = np.log1p(train_df["target_chl_a_next_10d"].to_numpy())
    X_test = test_df[feature_cols]
    y_test = test_df["target_chl_a_next_10d"].to_numpy()
    y_test_log = np.log1p(y_test)

    # Бейзлайн: Инерция ("через 10 дней будет как сегодня")
    naive_pred = test_df["chl_a_current_ug_l"].to_numpy()
    naive_mae = float(mean_absolute_error(y_test, naive_pred))
    naive_rmse = float(root_mean_squared_error(y_test, naive_pred))
    naive_rho, _ = spearmanr(y_test, naive_pred)

    print("=== МОДЕЛЬ 2 (ПРОГНОЗ ХЛОРОФИЛЛА ЧЕРЕЗ +10 ДНЕЙ) ===")
    print(f"Признаков: {len(feature_cols)} | Train (2018-2024): {len(train_df)} | Test (2025-2026): {len(test_df)}")
    print(
        f"[Baseline] Инерция       | MAE: {naive_mae:.2f} | RMSE: {naive_rmse:.2f} | "
        f"Spearman rho: {naive_rho:+.3f}"
    )

    models = {
        "Ridge": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", Ridge(alpha=10.0)),
        ]),
        "RandomForest": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", RandomForestRegressor(
                n_estimators=200,
                max_depth=6,
                min_samples_leaf=8,
                random_state=42,
                n_jobs=-1,
            )),
        ]),
    }
    if LGBMRegressor is not None:
        models["LightGBM"] = LGBMRegressor(
            n_estimators=200,
            learning_rate=0.03,
            max_depth=4,
            num_leaves=15,
            min_child_samples=20,
            random_state=42,
            verbose=-1,
        )
    else:
        models["HistGB"] = HistGradientBoostingRegressor(
            max_iter=200,
            learning_rate=0.03,
            max_depth=4,
            min_samples_leaf=20,
            random_state=42,
        )

    best_name = None
    best_mae = float("inf")
    best_artifact = None
    candidates_metrics = {}
    y_att_bin = (y_test >= 15.0).astype(int)

    for name, model in models.items():
        model.fit(X_train, y_train_log)
        y_pred_log = model.predict(X_test)
        y_pred = np.clip(np.expm1(y_pred_log), 0.1, None)

        mae = float(mean_absolute_error(y_test, y_pred))
        rmse = float(root_mean_squared_error(y_test, y_pred))
        rho, _ = spearmanr(y_test, y_pred)
        log_residual_std = float(np.std(y_test_log - y_pred_log, ddof=1))
        att_auc = float(roc_auc_score(y_att_bin, y_pred_log)) if 0 < y_att_bin.sum() < len(y_att_bin) else None

        candidates_metrics[name] = {
            "mae_ug_l": round(mae, 3),
            "rmse_ug_l": round(rmse, 3),
            "spearman_rho": round(float(rho), 3),
            "attention_15_roc_auc": round(att_auc, 3) if att_auc is not None else None,
        }

        print(
            f"[{name:12s}]           | MAE: {mae:.2f} | RMSE: {rmse:.2f} | "
            f"Spearman rho: {rho:+.3f}" + (f" | AUC>=15: {att_auc:.3f}" if att_auc is not None else "")
        )

        if mae < best_mae:
            best_mae = mae
            best_name = name
            best_artifact = {
                "model_name": name,
                "model": model,
                "feature_cols": feature_cols,
                "train_rows": int(len(train_df)),
                "test_rows": int(len(test_df)),
                "test_mae_ug_l": round(mae, 3),
                "test_rmse_ug_l": round(rmse, 3),
                "test_spearman_rho": round(float(rho), 3),
                "attention_15_roc_auc": round(att_auc, 3) if att_auc is not None else None,
                "log_residual_std": round(max(log_residual_std, 0.15), 4),
                "top_drivers": extract_top_drivers(model, feature_cols, top_n=5),
                "naive_baseline": {
                    "mae_ug_l": round(naive_mae, 3),
                    "rmse_ug_l": round(naive_rmse, 3),
                    "spearman_rho": round(float(naive_rho), 3),
                },
                "candidates": candidates_metrics,
            }

    save_path = SAVED_MODELS_DIR / "forecaster.joblib"
    joblib.dump(best_artifact, save_path)
    print(f"\n[OK] Лучшая модель прогноза ({best_name}, MAE={best_mae:.2f} мкг/л) сохранена в: {save_path}")

    # Сохраняем общую сводку метрик обеих моделей для API (Участник 4)
    reg_artifact = joblib.load(SAVED_MODELS_DIR / "regressor.joblib")
    metrics_payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_1_current_chlorophyll": {
            "winner": reg_artifact["model_name"],
            "train_dataset": f"GLORIA (<= {reg_artifact['gloria_max_chl_ug_l']} ug/L, without 'other')",
            "train_rows": reg_artifact["train_rows"],
            "validation_dataset": "BSU lab measurements (36 samples, 2018-2019, zero-shot)",
            "bsu_mae_ug_l": reg_artifact["bsu_mae_ug_l"],
            "bsu_rmse_ug_l": reg_artifact["bsu_rmse_ug_l"],
            "bsu_spearman_rho": reg_artifact["bsu_spearman_rho"],
            "mishra_baseline": reg_artifact["mishra_baseline"],
            "candidates": reg_artifact["candidates"],
        },
        "model_2_forecast_10d": {
            "winner": best_artifact["model_name"],
            "train_period": "2018-2024",
            "train_rows": best_artifact["train_rows"],
            "test_period": "2025-2026",
            "test_rows": best_artifact["test_rows"],
            "test_mae_ug_l": best_artifact["test_mae_ug_l"],
            "test_rmse_ug_l": best_artifact["test_rmse_ug_l"],
            "test_spearman_rho": best_artifact["test_spearman_rho"],
            "attention_15_roc_auc": best_artifact["attention_15_roc_auc"],
            "naive_baseline": best_artifact["naive_baseline"],
            "candidates": best_artifact["candidates"],
            "top_drivers": best_artifact["top_drivers"],
        },
    }
    METRICS_PATH.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] Сводка метрик сохранена в: {METRICS_PATH}")


if __name__ == "__main__":
    main()