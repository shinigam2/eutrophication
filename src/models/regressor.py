from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from scipy.stats import spearmanr
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, root_mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT_DIR = Path(__file__).resolve().parents[2]
SAVED_MODELS_DIR = Path(__file__).resolve().parent / "saved_models"


def build_optical_features(df: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """Формирует устойчивые к атмосфере спектральные признаки (работает для GLORIA, LAB и SATELLITE)."""
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


def main() -> None:
    SAVED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Обучение на пресноводных озёрах GLORIA
    gloria = pd.read_parquet(ROOT_DIR / "data/interim/gloria_sentinel2_pairs.parquet")
    gloria = gloria[gloria["is_inland_lake"] == True].dropna(subset=["chl_a_ug_l"]).reset_index(drop=True)

    X_train = build_optical_features(gloria, prefix="")
    y_train_log = np.log1p(gloria["chl_a_ug_l"].to_numpy())

    # 2. Независимый экзамен на 36 пробах БГУ
    lab = pd.read_parquet(ROOT_DIR / "data/processed/features_lab.parquet")
    X_test = build_optical_features(lab, prefix="s2_")
    y_test = lab["chl_a_lab_ug_l"].to_numpy()
    y_mishra = lab["s2_chl_a_est_ug_l"].to_numpy()

    mishra_mae = mean_absolute_error(y_test, y_mishra)
    mishra_rho, _ = spearmanr(y_test, y_mishra)
    print("=== РЕГРЕССИЯ: Экзамен на 36 пробах БГУ (2018–2019) ===")
    print(f"[Baseline] Mishra (2012) | MAE: {mishra_mae:.2f} мкг/л | Spearman rho: {mishra_rho:+.3f}")

    models = {
        "Ridge": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", Ridge(alpha=1.0)),
        ]),
        "LightGBM": LGBMRegressor(
            n_estimators=200,
            learning_rate=0.03,
            max_depth=4,
            num_leaves=15,
            min_child_samples=20,
            random_state=42,
            verbose=-1,
        ),
    }

    best_name = None
    best_mae = float("inf")
    best_artifact = None

    for name, model in models.items():
        model.fit(X_train, y_train_log)
        y_pred = np.clip(np.expm1(model.predict(X_test)), 0.1, None)

        mae = float(mean_absolute_error(y_test, y_pred))
        rmse = float(root_mean_squared_error(y_test, y_pred))
        rho, _ = spearmanr(y_test, y_pred)

        print(f"[{name:8s}]               | MAE: {mae:.2f} мкг/л | RMSE: {rmse:.2f} | Spearman rho: {rho:+.3f}")

        if mae < best_mae:
            best_mae = mae
            best_name = name
            best_artifact = {
                "model_name": name,
                "model": model,
                "feature_cols": list(X_train.columns),
                "bsu_mae_ug_l": round(mae, 3),
                "bsu_rmse_ug_l": round(rmse, 3),
                "bsu_spearman_rho": round(float(rho), 3),
            }

    save_path = SAVED_MODELS_DIR / "regressor.joblib"
    joblib.dump(best_artifact, save_path)
    print(f"\n[OK] Лучший регрессор ({best_name}, MAE={best_mae:.2f}) сохранён в: {save_path}")


if __name__ == "__main__":
    main()