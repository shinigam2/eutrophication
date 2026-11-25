from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT_DIR = Path(__file__).resolve().parents[2]
SAVED_MODELS_DIR = Path(__file__).resolve().parent / "saved_models"
THRESHOLD_UG_L = 8.0  # Порог эвтрофного состояния (в БГУ: 7 положительных из 36)


def build_optical_features(df: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """Формирует устойчивые к атмосфере спектральные признаки."""
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
    y_train = (gloria["chl_a_ug_l"] >= THRESHOLD_UG_L).astype(int).to_numpy()

    # 2. Независимый экзамен на 36 пробах БГУ
    lab = pd.read_parquet(ROOT_DIR / "data/processed/features_lab.parquet")
    X_test = build_optical_features(lab, prefix="s2_")
    y_test = (lab["chl_a_lab_ug_l"] >= THRESHOLD_UG_L).astype(int).to_numpy()

    print(f"=== КЛАССИФИКАЦИЯ (порог >= {THRESHOLD_UG_L} мкг/л): Экзамен на 36 пробах БГУ ===")
    print(f"Положительных в БГУ: {int(y_test.sum())} из {len(y_test)}")

    models = {
        "LogReg": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42)),
        ]),
        "LightGBM": LGBMClassifier(
            n_estimators=200,
            learning_rate=0.03,
            max_depth=4,
            num_leaves=15,
            min_child_samples=20,
            class_weight="balanced",
            random_state=42,
            verbose=-1,
        ),
    }

    best_name = None
    best_auc = -1.0
    best_artifact = None

    for name, model in models.items():
        model.fit(X_train, y_train)
        y_prob = model.predict_proba(X_test)[:, 1]
        y_pred = (y_prob >= 0.5).astype(int)

        roc_auc = float(roc_auc_score(y_test, y_prob))
        pr_auc = float(average_precision_score(y_test, y_prob))
        recall = float(recall_score(y_test, y_pred, zero_division=0))
        precision = float(precision_score(y_test, y_pred, zero_division=0))
        f1 = float(f1_score(y_test, y_pred, zero_division=0))

        print(
            f"[{name:8s}] | ROC-AUC: {roc_auc:.3f} | PR-AUC: {pr_auc:.3f} | "
            f"Recall: {recall:.3f} | Precision: {precision:.3f} | F1: {f1:.3f}"
        )

        if roc_auc > best_auc:
            best_auc = roc_auc
            best_name = name
            best_artifact = {
                "model_name": name,
                "model": model,
                "feature_cols": list(X_train.columns),
                "threshold_ug_l": THRESHOLD_UG_L,
                "bsu_roc_auc": round(roc_auc, 3),
                "bsu_pr_auc": round(pr_auc, 3),
                "bsu_f1": round(f1, 3),
                "bsu_recall": round(recall, 3),
            }

    save_path = SAVED_MODELS_DIR / "classifier.joblib"
    joblib.dump(best_artifact, save_path)
    print(f"\n[OK] Лучший классификатор ({best_name}, ROC-AUC={best_auc:.3f}) сохранён в: {save_path}")


if __name__ == "__main__":
    main()