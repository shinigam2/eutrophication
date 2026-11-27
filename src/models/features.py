import numpy as np
import pandas as pd

VISIBLE_BANDS = ("b02_blue", "b03_green", "b04_red")

OPTICAL_FEATURE_COLS = [
    "ndti",
    "b03_b02_ratio",
    "b04_b02_ratio",
    "b04_b03_ratio",
    "b02_blue_norm",
    "b03_green_norm",
    "b04_red_norm",
]


def _get_band_series(df: pd.DataFrame, band: str, prefix: str = "") -> pd.Series:
    """Находит нужный канал с учётом префиксов (GLORIA, features_lab, features_satellite)."""
    candidates = [f"{prefix}{band}", band, f"{band}_current"]
    for col in candidates:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def build_optical_features(
    df: pd.DataFrame,
    prefix: str = "",
    subtract_glint: bool = False,
) -> pd.DataFrame:
    """
    Формирует очищенные спектральные признаки по видимым каналам B02, B03, B04.
    Для спутниковых данных Sentinel-2 (subtract_glint=True) вычитает блик 0.8 * B11.
    """
    out = pd.DataFrame(index=df.index)

    for band in VISIBLE_BANDS:
        out[band] = _get_band_series(df, band, prefix=prefix)

    if subtract_glint:
        b11 = _get_band_series(df, "b11_swir1", prefix=prefix).fillna(0.0).clip(lower=0.0)
        # Ограничиваем вычитаемый блик половиной минимального видимого канала,
        # чтобы на тёмных малых озёрах B02/B04 не проваливались в упор 0.001
        min_vis = out[list(VISIBLE_BANDS)].min(axis=1).clip(lower=0.0)
        glint = np.minimum(0.8 * b11, 0.5 * min_vis)
        for band in VISIBLE_BANDS:
            out[band] = (out[band] - glint).clip(lower=0.001)
    else:
        for band in VISIBLE_BANDS:
            out[band] = out[band].clip(lower=0.001)

    eps = 1e-6
    out["ndti"] = (out["b04_red"] - out["b03_green"]) / (out["b04_red"] + out["b03_green"] + eps)
    out["b03_b02_ratio"] = (out["b03_green"] / out["b02_blue"]).clip(0.05, 5.0)
    out["b04_b02_ratio"] = (out["b04_red"] / out["b02_blue"]).clip(0.05, 5.0)
    out["b04_b03_ratio"] = (out["b04_red"] / out["b03_green"]).clip(0.05, 5.0)

    band_sum = out[list(VISIBLE_BANDS)].sum(axis=1).replace(0, np.nan)
    for band in VISIBLE_BANDS:
        out[f"{band}_norm"] = out[band] / band_sum

    return out[OPTICAL_FEATURE_COLS]