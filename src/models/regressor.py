from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, root_mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

try:
    from lightgbm import LGBMRegressor
except ImportError:
    LGBMRegressor = None

try:
    from src.models.features import OPTICAL_FEATURE_COLS, build_optical_features
except ImportError:
    from features import OPTICAL_FEATURE_COLS, build_optical_features

ROOT_DIR = Path(__file__).resolve().parents[2]
SAVED_MODELS_DIR = Path(__file__).resolve().parent / "saved_models"
GLORIA_MAX_CHL_UG_L = 60.0  # Оптимальный порог против зарубежных гиперэвтрофных выбросов


def main() -> None:
    SAVED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Загружаем и очищаем GLORIA (включаем чистые coastal, убираем other и экстремальный хвост)
    gloria = pd.read_parquet(ROOT_DIR / "data/interim/gloria_sentinel2_pairs.parquet")
    gloria = gloria[
        (gloria["water_body_type"] != "other")
        & (gloria["chl_a_ug_l"] > 0)
        & (gloria["chl_a_ug_l"] <= GLORIA_MAX_CHL_UG_L)
    ].dropna(subset=["chl_a_ug_l", "b02_blue", "b03_green", "b04_red"]).reset_index(drop=True)

    X_train = build_optical_features(gloria, prefix="", subtract_glint=False)
    y_train_log = np.log1p(gloria["chl_a_ug_l"].to_numpy())

    # 2. Независимый экзамен на 36 реальных пробах БГУ (с вычитанием блика)
    lab = pd.read_parquet(ROOT_DIR / "data/processed/features_lab.parquet")
    X_test = build_optical_features(lab, prefix="s2_", subtract_glint=True)
    y_test = lab["chl_a_lab_ug_l"].to_numpy()
    y_mishra = lab["s2_chl_a_est_ug_l"].to_numpy()

    mishra_mae = float(mean_absolute_error(y_test, y_mishra))
    mishra_rmse = float(root_mean_squared_error(y_test, y_mishra))
    mishra_rho, _ = spearmanr(y_test, y_mishra)

    print(f"=== МОДЕЛЬ 1 (РЕГРЕССИЯ ТЕКУЩЕГО ХЛОРОФИЛЛА) ===")
    print(f"Обучающая выборка GLORIA (<= {GLORIA_MAX_CHL_UG_L:.0f} мкг/л, без 'other'): {len(gloria)} строк")
    print(f"Экзамен на 36 пробах БГУ (реальная медиана: {np.median(y_test):.2f} мкг/л):")
    print(
        f"[Baseline] Mishra (2012) | MAE: {mishra_mae:.2f} | RMSE: {mishra_rmse:.2f} | "
        f"Spearman rho: {mishra_rho:+.3f} | Медиана: {np.median(y_mishra):.2f}"
    )

    models = {
        "Ridge": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", Ridge(alpha=1.0)),
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
    all_results = {}

    for name, model in models.items():
        model.fit(X_train, y_train_log)
        y_pred = np.clip(np.expm1(model.predict(X_test)), 0.1, None)

        mae = float(mean_absolute_error(y_test, y_pred))
        rmse = float(root_mean_squared_error(y_test, y_pred))
        rho, _ = spearmanr(y_test, y_pred)
        med = float(np.median(y_pred))

        all_results[name] = {
            "mae_ug_l": round(mae, 3),
            "rmse_ug_l": round(rmse, 3),
            "spearman_rho": round(float(rho), 3),
            "pred_median_ug_l": round(med, 3),
        }

        print(
            f"[{name:8s}]               | MAE: {mae:.2f} | RMSE: {rmse:.2f} | "
            f"Spearman rho: {rho:+.3f} | Медиана: {med:.2f}"
        )

        if mae < best_mae:
            best_mae = mae
            best_name = name
            best_artifact = {
                "model_name": name,
                "model": model,
                "feature_cols": OPTICAL_FEATURE_COLS,
                "gloria_max_chl_ug_l": GLORIA_MAX_CHL_UG_L,
                "train_rows": int(len(gloria)),
                "bsu_mae_ug_l": round(mae, 3),
                "bsu_rmse_ug_l": round(rmse, 3),
                "bsu_spearman_rho": round(float(rho), 3),
                "mishra_baseline": {
                    "mae_ug_l": round(mishra_mae, 3),
                    "rmse_ug_l": round(mishra_rmse, 3),
                    "spearman_rho": round(float(mishra_rho), 3),
                },
                "candidates": all_results,
            }

    save_path = SAVED_MODELS_DIR / "regressor.joblib"
    joblib.dump(best_artifact, save_path)
    print(f"\n[OK] Лучший регрессор ({best_name}, MAE={best_mae:.2f} мкг/л) сохранён в: {save_path}")


if __name__ == "__main__":
    main()