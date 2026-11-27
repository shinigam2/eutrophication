"""Юнит-тесты радиометрии, дедупликации сцен и оптических формул Sentinel-2."""

from __future__ import annotations

import math

import pytest

from src.etl.sentinel_fetcher import (
    _compute_carlson_tsi,
    _compute_mishra_chl_a,
    _get_band_scale_offset_nodata,
    _safe_normalized_diff,
    deduplicate_stac_scenes,
)


def test_boa_offset_applied_prevents_double_subtraction() -> None:
    asset = {"raster:bands": [{"scale": 0.0001, "offset": -0.1, "nodata": 0}]}

    # Element84 Earth Search уже вычел -1000 DN при сборке COG -> effective_offset == 0.0
    scale, offset, nodata = _get_band_scale_offset_nodata(
        asset, {"earthsearch:boa_offset_applied": True}
    )
    assert scale == pytest.approx(0.0001)
    assert offset == pytest.approx(0.0)
    assert nodata == 0.0

    # Если COG негармонизирован (boa_offset_applied=False), offset -0.1 применяется честно
    scale2, offset2, _ = _get_band_scale_offset_nodata(
        asset, {"earthsearch:boa_offset_applied": False}
    )
    assert scale2 == pytest.approx(0.0001)
    assert offset2 == pytest.approx(-0.1)


def test_deduplicate_prefers_newer_processing_baseline() -> None:
    scenes = [
        {
            "id": "S2B_35UMA_20180810_0_L2A",
            "properties": {
                "datetime": "2018-08-10T09:30:29Z",
                "grid:code": "MGRS-35UMA",
                "platform": "sentinel-2b",
                "s2:processing_baseline": "02.08",
            },
        },
        {
            "id": "S2B_35UMA_20180810_1_L2A",
            "properties": {
                "datetime": "2018-08-10T09:30:29Z",
                "grid:code": "MGRS-35UMA",
                "platform": "sentinel-2b",
                "s2:processing_baseline": "05.00",
            },
        },
    ]
    deduped = deduplicate_stac_scenes(scenes)
    assert len(deduped) == 1
    assert deduped[0]["id"] == "S2B_35UMA_20180810_1_L2A"


def test_optical_indices_and_mishra_carlson() -> None:
    assert _safe_normalized_diff(0.04, 0.02) == pytest.approx(1.0 / 3.0)
    assert _safe_normalized_diff(-0.01, 0.02) is None
    assert _safe_normalized_diff(0.0, 0.0) is None

    # Проверка формулы Mishra & Mishra (2012) в контрольной точке NDCI = 0
    assert _compute_mishra_chl_a(0.0) == pytest.approx(14.039)
    # Монотонное ограничение в вершине параболы (-0.2216): отрицательный NDCI не должен раздуваться до 70–85 мкг/л
    assert 4.4 <= _compute_mishra_chl_a(-0.85) <= 4.6
    assert _compute_mishra_chl_a(-0.85) == pytest.approx(_compute_mishra_chl_a(-0.5), abs=1e-4)
    # Проверка Карлсона TSI(10) = 9.81 * ln(10) + 30.6
    assert _compute_carlson_tsi(10.0) == pytest.approx(9.81 * math.log(10.0) + 30.6)
