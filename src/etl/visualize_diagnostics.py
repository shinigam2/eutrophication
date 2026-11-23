"""Визуальная диагностика и проверка реальных данных Этапов 1, 2, 3 и 5.

Строит 3 наглядных диагностических рисунка СТРОГО по реальным данным:
  1. diag_1_sentinel2_overview_20260701.png
     Живой снимок Sentinel-2C (2026-07-01, 0% облаков) всего региона Нарочь + Мядель
     с наложением 8 активных озёр (бирюзовый), 1 резервного «Россохи» (жёлтый пунктир)
     и 6 отсеянных малых/засвеченных озёр (красный).
  2. diag_2_pixel_extraction_autopsy.png
     Пошаговый разбор алгоритма вырезки пикселей на живом снимке 2026-07-01:
       (A) Озеро Мядель (10м RGB) + сырой контур OSM (8 островов) + буфер −25м;
       (B) Карта маски SCL (20м) на оз. Мядель — что идёт в расчёт (SCL==6) и что отбрасывается;
       (C) Оптический контраст воды (B04/B02) на каскаде Нарочь → Мястро → Баторино;
       (D) Увеличение отсеянного озера Черток (6 га): сетка 20м пикселей и засветка от леса.
  3. diag_3_meteo_lab_gloria_data.png
     Проверка реальных данных Этапов 3 и 5:
       (A) Погода NASA POWER 2018–2026 (3 241 день по оз. Нарочь);
       (B) Реальные лабораторные пробы БГУ 2018–2019 (Нарочь / Мястро / Баторино);
       (C) Мировой эталон GLORIA (3 368 внутренних озёр из 29 стран): цвет воды vs пробирка.

Запуск:
  python -m src.etl.visualize_diagnostics
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import shapely
from matplotlib.colors import ListedColormap
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from rasterio.windows import transform as window_transform
from shapely.geometry import MultiPolygon, Polygon

from src.etl.config import INTERIM_DIR, RAW_DIR, load_config
from src.etl.gloria_loader import GLORIA_PARQUET_PATH
from src.etl.lab_loader import LAB_CLEAN_PARQUET_PATH, NAROCH_APP_CSV_PATH
from src.etl.meteo_fetcher import METEO_PARQUET_PATH
from src.etl.osm_geometry import build_polygon_from_osm, fetch_osm_full_json
from src.etl.sentinel_fetcher import GDAL_COG_ENV

DIAG_DIR = INTERIM_DIR / "diagnostics"

# Реальная безоблачная сцена Sentinel-2C (0.0% облачности, 2026-07-01, тайл 35UMA)
SCENE_BASE = (
    "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
    "sentinel-s2-l2a-cogs/35/U/MA/2026/7/S2C_35UMA_20260701_0_L2A"
)
TF_WGS_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32635", always_xy=True)


def _to_utm(geom: Polygon | MultiPolygon) -> Polygon | MultiPolygon:
    def _fn(coords: np.ndarray) -> np.ndarray:
        x, y = TF_WGS_TO_UTM.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((x, y))

    return shapely.transform(geom, _fn)


def _plot_polygon_outline(
    ax: plt.Axes,
    geom: Polygon | MultiPolygon,
    color: str,
    lw: float = 1.4,
    ls: str = "-",
    alpha: float = 0.95,
) -> None:
    """Рисует внешнюю границу и внутренние острова (holes) полигона в координатах UTM."""
    polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms)
    for p in polys:
        x, y = p.exterior.xy
        ax.plot(x, y, color=color, linewidth=lw, linestyle=ls, alpha=alpha)
        for hole in p.interiors:
            hx, hy = hole.xy
            ax.plot(hx, hy, color=color, linewidth=max(0.8, lw * 0.8), linestyle=ls, alpha=alpha)


def _stretch_rgb(r: np.ndarray, g: np.ndarray, b: np.ndarray, p_low: float = 2.0, p_high: float = 98.5) -> np.ndarray:
    """Мягкая гамма-коррекция реальных каналов Sentinel-2 (B04, B03, B02) для естественного RGB."""
    rgb = np.stack([r, g, b], axis=-1).astype(np.float64)
    # Масштабируем по общим перцентилям, чтобы сохранить естественный цветовой баланс воды и леса
    valid = rgb[rgb > 0]
    lo = np.percentile(valid, p_low) if valid.size else 0.0
    hi = np.percentile(valid, p_high) if valid.size else 2500.0
    norm = np.clip((rgb - lo) / max(hi - lo, 1.0), 0.0, 1.0)
    # Небольшая гамма 0.78 делает тёмную воду и берега хорошо различимыми глазу
    return np.power(norm, 0.78)


def _read_rgb_window(
    bounds_utm: tuple[float, float, float, float],
    overview_level: int | None = None,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """Читает реальное RGB-окно (B04, B03, B02) с Sentinel-2C 2026-07-01 с повторами при обрыве."""
    open_kw = {"overview_level": overview_level} if overview_level is not None else {}
    env_cfg = {**GDAL_COG_ENV, "GDAL_HTTP_TIMEOUT": "60", "GDAL_HTTP_MAX_RETRY": "5"}
    bands = []
    extent = (0.0, 1.0, 0.0, 1.0)
    with rasterio.Env(**env_cfg):
        for bname in ("B04", "B03", "B02"):
            arr = None
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    with rasterio.open(f"{SCENE_BASE}/{bname}.tif", **open_kw) as src:
                        win = (
                            from_bounds(*bounds_utm, transform=src.transform)
                            .round_offsets()
                            .round_lengths()
                        )
                        arr = src.read(1, window=win)
                        if bname == "B04":
                            l, b_y, r_x, t = rasterio.windows.bounds(win, src.transform)
                            extent = (l, r_x, b_y, t)
                        break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(attempt * 2.0)
            if arr is None:
                raise RuntimeError(f"Не удалось прочитать {bname}.tif: {last_exc}")
            bands.append(arr)
    return _stretch_rgb(bands[0], bands[1], bands[2]), extent


def load_all_15_utm() -> tuple[dict[str, dict[str, Any]], dict[str, Polygon | MultiPolygon]]:
    """Загружает все 15 полигонов озёр из локального кэша OSM и переводит в UTM 35N."""
    cfg = load_config()
    meta: dict[str, dict[str, Any]] = {}
    geoms_utm: dict[str, Polygon | MultiPolygon] = {}
    for wb in cfg.water_bodies:
        payload = fetch_osm_full_json(wb, force_refresh=False)
        geom_wgs, _ = build_polygon_from_osm(wb, payload)
        geoms_utm[wb.id] = _to_utm(geom_wgs)
        status = "active" if wb.enabled else ("reserve" if wb.id == "rossokhi" else "dropped")
        meta[wb.id] = {
            "name_ru": wb.name_ru.replace("Озеро ", ""),
            "area_km2": wb.area_km2,
            "status": status,
        }
    return meta, geoms_utm


def make_fig1_overview(
    meta: dict[str, dict[str, Any]],
    geoms_utm: dict[str, Polygon | MultiPolygon],
) -> Path:
    """Рисунок 1: Общий реальный снимок Sentinel-2C (2026-07-01) с разметкой всех 15 озёр."""
    print("[VIS 1/3] Генерация обзорного спутникового снимка Нарочь + Мядель (2026-07-01)...", flush=True)
    t0 = time.time()
    xs0 = [g.bounds[0] for g in geoms_utm.values()]
    ys0 = [g.bounds[1] for g in geoms_utm.values()]
    xs1 = [g.bounds[2] for g in geoms_utm.values()]
    ys1 = [g.bounds[3] for g in geoms_utm.values()]
    pad = 1800.0
    bounds = (min(xs0) - pad, min(ys0) - pad, max(xs1) + pad, max(ys1) + pad)

    # overview_level=1 (40 м пирамида) — чётко и очень быстро для обзора 24×26 км
    rgb, extent = _read_rgb_window(bounds, overview_level=1)

    fig, ax = plt.subplots(figsize=(12.5, 11.5), dpi=160)
    ax.imshow(rgb, extent=extent, origin="upper")

    color_map = {"active": "#00F5FF", "reserve": "#FFE600", "dropped": "#FF4444"}
    ls_map = {"active": "-", "reserve": "--", "dropped": "-"}
    lw_map = {"active": 1.8, "reserve": 2.0, "dropped": 1.3}

    # Ручные сдвиги подписей (метры UTM), чтобы названия нигде не перекрывались
    label_offsets: dict[str, tuple[float, float]] = {
        "naroch": (0, 0),
        "myastro": (0, -1400),
        "batorino": (0, -1400),
        "myadel": (150, -350),
        "kuzmichi": (-1800, 900),
        "volchino": (1500, 600),
        "lotviny": (1600, 300),
        "rudakovo": (1600, -350),
        "rossokhi": (1900, -450),
        "knyagininskoe": (2100, -1050),
        "khodosy": (-2100, 450),
        "chertok": (-2400, 100),
        "telyakovskoe": (-2000, 500),
        "barsuki": (-1800, 600),
        "repyakhovskoe": (-2000, -600),
    }

    for lid, geom in geoms_utm.items():
        st = meta[lid]["status"]
        _plot_polygon_outline(ax, geom, color=color_map[st], lw=lw_map[st], ls=ls_map[st])
        pt = geom.representative_point()
        dx, dy = label_offsets.get(lid, (0, 0))
        tag = meta[lid]["name_ru"]
        if st == "reserve":
            tag += " (резерв)"
        fsize = 10.5 if meta[lid]["area_km2"] >= 5.0 else (8.8 if st != "dropped" else 7.5)
        ax.annotate(
            tag,
            xy=(pt.x, pt.y),
            xytext=(pt.x + dx, pt.y + dy),
            color="white" if st == "active" else color_map[st],
            fontsize=fsize,
            fontweight="bold" if st != "dropped" else "normal",
            ha="center",
            va="center",
            bbox=dict(boxstyle="round,pad=0.22", facecolor="black", alpha=0.68, edgecolor=color_map[st], lw=0.8),
            arrowprops=dict(arrowstyle="-", color=color_map[st], lw=0.8) if (dx or dy) else None,
        )

    ax.set_title(
        "Реальный снимок Sentinel-2C (01.07.2026, 0% облаков, тайл 35UMA)\n"
        "Наложение точных полигонов OpenStreetMap: 8 активных озёр + 1 резерв (Россохи) + 6 отсеянных",
        fontsize=12.5,
        fontweight="bold",
        pad=12,
    )
    ax.set_xlabel("UTM 35N Восток, м (EPSG:32635)", fontsize=10)
    ax.set_ylabel("UTM 35N Север, м (EPSG:32635)", fontsize=10)
    ax.ticklabel_format(style="plain", useOffset=False)

    legend_handles = [
        mpatches.Patch(facecolor="none", edgecolor="#00F5FF", linewidth=2.0, label="Активные озёра (8 шт., ≥374 чист. пикс., 81–96% воды)"),
        mpatches.Patch(facecolor="none", edgecolor="#FFE600", linewidth=2.0, linestyle="--", label="Резерв для живого добавления (Россохи, 316 пикс., 93,3%)"),
        mpatches.Patch(facecolor="none", edgecolor="#FF4444", linewidth=1.6, label="Отсеяны на Этапе 2 (6 шт., <20 пикс. или засветка ИК от леса)"),
    ]
    ax.legend(handles=legend_handles, loc="lower left", frameon=True, facecolor="black", edgecolor="white", labelcolor="white", fontsize=9.2)

    out_path = DIAG_DIR / "diag_1_sentinel2_overview_20260701.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    print(f"  -> готово за {time.time() - t0:.1f} с: {out_path}", flush=True)
    return out_path


def make_fig2_autopsy(
    meta: dict[str, dict[str, Any]],
    geoms_utm: dict[str, Polygon | MultiPolygon],
) -> Path:
    """Рисунок 2: Пошаговая анатомия вырезки пикселей, буфера −25м, SCL==6 и спектра B04/B02."""
    print("[VIS 2/3] Генерация 4-панельного разбора алгоритма вырезки пикселей...", flush=True)
    t0 = time.time()

    fig, axes = plt.subplots(2, 2, figsize=(14.5, 12.2), dpi=160)

    # --- Панель A: Озеро Мядель (20м RGB пирамида) + сырой контур OSM (8 островов) + буфер −25м ---
    myadel = geoms_utm["myadel"]
    myadel_buf = myadel.buffer(-25.0)
    mx0, my0, mx1, my1 = myadel.bounds
    m_bounds = (mx0 - 350, my0 - 350, mx1 + 350, my1 + 350)
    rgb_myadel, ext_myadel = _read_rgb_window(m_bounds, overview_level=0)  # 20м пирамида — быстро и чётко!

    ax = axes[0, 0]
    ax.imshow(rgb_myadel, extent=ext_myadel, origin="upper")
    _plot_polygon_outline(ax, myadel, color="white", lw=1.5, ls="-")
    _plot_polygon_outline(ax, myadel_buf, color="#00FF66", lw=1.4, ls="--")
    ax.set_title("A. Озеро Мядель (RGB Sentinel-2, 01.07.2026)\nБелый = берег и 8 островов OSM; Зелёный = буфер −25 м", fontsize=10.8, fontweight="bold")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(
        handles=[
            mpatches.Patch(facecolor="none", edgecolor="white", lw=1.6, label="Контур OSM (с вырезами 8 островов)"),
            mpatches.Patch(facecolor="none", edgecolor="#00FF66", lw=1.6, ls="--", label="Внутренний буфер −25 м (убран камыш)"),
        ],
        loc="lower right",
        facecolor="black",
        labelcolor="white",
        fontsize=8.2,
    )

    # --- Панель B: Реальная 20м маска SCL внутри Озера Мядель ---
    with rasterio.Env(**GDAL_COG_ENV):
        with rasterio.open(f"{SCENE_BASE}/SCL.tif") as scl_src:
            win = from_bounds(*m_bounds, transform=scl_src.transform).round_offsets().round_lengths()
            scl = scl_src.read(1, window=win)
            w_tf = window_transform(win, scl_src.transform)
            l, b_y, r_x, t = rasterio.windows.bounds(win, scl_src.transform)
            ext_scl = (l, r_x, b_y, t)

    inside_buf = ~geometry_mask([myadel_buf], out_shape=scl.shape, transform=w_tf, all_touched=False)
    # Коды отображения: 0=суша/вне буфера (серый), 1=чистая вода SCL==6 внутри буфера (синий),
    # 2=растительность/прочее внутри буфера (оранжевый), 3=облака/тени внутри буфера (красный)
    vis_cls = np.zeros_like(scl, dtype=np.uint8)
    vis_cls[inside_buf & (scl == 6)] = 1
    vis_cls[inside_buf & np.isin(scl, [4, 5, 7])] = 2
    vis_cls[inside_buf & np.isin(scl, [3, 8, 9, 10, 11])] = 3

    n_water = int(np.count_nonzero(vis_cls == 1))
    n_other = int(np.count_nonzero(vis_cls == 2))
    cmap_scl = ListedColormap(["#262A30", "#0088FF", "#FFAA00", "#FF3333"])

    ax = axes[0, 1]
    ax.imshow(vis_cls, extent=ext_scl, origin="upper", cmap=cmap_scl, vmin=0, vmax=3, interpolation="nearest")
    _plot_polygon_outline(ax, myadel, color="#AAAAAA", lw=0.9)
    ax.set_title(
        f"B. Маска SCL (сетка 20 м) на оз. Мядель\n"
        f"В расчёт идут ТОЛЬКО синие пиксели: {n_water:,} шт. (отсеяно внутри: {n_other})",
        fontsize=10.8,
        fontweight="bold",
    )
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(
        handles=[
            mpatches.Patch(color="#0088FF", label=f"SCL == 6: чистая вода ({n_water:,} пикс.)"),
            mpatches.Patch(color="#FFAA00", label=f"SCL 4/5/7: растительность/отмель ({n_other})"),
            mpatches.Patch(color="#262A30", label="Суша, острова и береговая полоса 25 м"),
        ],
        loc="lower right",
        facecolor="black",
        labelcolor="white",
        fontsize=8.2,
    )

    # --- Панель C: Оптическая карта B04/B02 на каскаде Нарочь → Мястро → Баторино (20м) ---
    cascade_ids = ["naroch", "myastro", "batorino"]
    c_geoms = [geoms_utm[k] for k in cascade_ids]
    c_bufs = [g.buffer(-25.0) for g in c_geoms]
    cx0 = min(g.bounds[0] for g in c_geoms) - 500
    cy0 = min(g.bounds[1] for g in c_geoms) - 500
    cx1 = max(g.bounds[2] for g in c_geoms) + 500
    cy1 = max(g.bounds[3] for g in c_geoms) + 500

    with rasterio.Env(**GDAL_COG_ENV):
        with rasterio.open(f"{SCENE_BASE}/SCL.tif") as scl_src:
            c_win = from_bounds(cx0, cy0, cx1, cy1, transform=scl_src.transform).round_offsets().round_lengths()
            c_scl = scl_src.read(1, window=c_win)
            c_tf = window_transform(c_win, scl_src.transform)
            c_bounds = rasterio.windows.bounds(c_win, scl_src.transform)
            ext_c = (c_bounds[0], c_bounds[2], c_bounds[1], c_bounds[3])
        with rasterio.open(f"{SCENE_BASE}/B04.tif", overview_level=0) as r_src:
            rw = from_bounds(*c_bounds, transform=r_src.transform).round_offsets().round_lengths()
            r_arr = r_src.read(1, window=rw, out_shape=c_scl.shape, resampling=Resampling.average).astype(np.float64) * 0.0001
        with rasterio.open(f"{SCENE_BASE}/B02.tif", overview_level=0) as b_src:
            bw = from_bounds(*c_bounds, transform=b_src.transform).round_offsets().round_lengths()
            b_arr = b_src.read(1, window=bw, out_shape=c_scl.shape, resampling=Resampling.average).astype(np.float64) * 0.0001

    c_inside = ~geometry_mask(c_bufs, out_shape=c_scl.shape, transform=c_tf, all_touched=False)
    c_water = c_inside & (c_scl == 6) & (b_arr > 0.001)
    ratio_r_b = np.full(c_scl.shape, np.nan, dtype=np.float64)
    ratio_r_b[c_water] = r_arr[c_water] / b_arr[c_water]

    ax = axes[1, 0]
    ax.set_facecolor("#1B1F24")
    im = ax.imshow(ratio_r_b, extent=ext_c, origin="upper", cmap="turbo", vmin=0.25, vmax=1.05)
    for lid, g in zip(cascade_ids, c_geoms):
        _plot_polygon_outline(ax, g, color="white", lw=0.8, alpha=0.8)
        pt = g.representative_point()
        ax.text(
            pt.x,
            pt.y,
            meta[lid]["name_ru"],
            color="white",
            fontsize=10,
            fontweight="bold",
            ha="center",
            va="center",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="black", alpha=0.65, edgecolor="none"),
        )
    cbar = fig.colorbar(im, ax=ax, fraction=0.042, pad=0.02)
    cbar.set_label("Отношение B04/B02 (красный / синий)", fontsize=9)
    ax.set_title(
        "C. Оптический градиент каскада (01.07.2026, только SCL==6)\n"
        "Нарочь (чистая ~0,42) → Мястро (~0,71) → Баторино (эвтрофное ~0,91)",
        fontsize=10.8,
        fontweight="bold",
    )
    ax.set_xticks([])
    ax.set_yticks([])

    # --- Панель D: Увеличение отсеянного озера Черток (6 га) — почему отсеяли! ---
    chertok = geoms_utm["chertok"]
    chertok_buf = chertok.buffer(-25.0)
    kx0, ky0, kx1, ky1 = chertok.bounds
    k_bounds = (kx0 - 160, ky0 - 160, kx1 + 160, ky1 + 160)
    rgb_ch, ext_ch = _read_rgb_window(k_bounds, overview_level=None)  # 10 м RGB

    with rasterio.Env(**GDAL_COG_ENV):
        with rasterio.open(f"{SCENE_BASE}/SCL.tif") as scl_src:
            kw = from_bounds(*k_bounds, transform=scl_src.transform).round_offsets().round_lengths()
            k_scl = scl_src.read(1, window=kw)
            k_tf = window_transform(kw, scl_src.transform)
            kb = rasterio.windows.bounds(kw, scl_src.transform)

    k_in = ~geometry_mask([chertok_buf], out_shape=k_scl.shape, transform=k_tf, all_touched=False)
    k_water = k_in & (k_scl == 6)

    ax = axes[1, 1]
    ax.imshow(rgb_ch, extent=ext_ch, origin="upper")
    # Полупрозрачная подсветка каждого 20-метрового пикселя SCL==6 внутри буфера
    overlay = np.zeros((*k_scl.shape, 4), dtype=np.float64)
    overlay[k_water] = [0.0, 0.9, 1.0, 0.45]
    overlay[k_in & ~k_water] = [1.0, 0.3, 0.0, 0.50]
    ax.imshow(overlay, extent=(kb[0], kb[2], kb[1], kb[3]), origin="upper", interpolation="nearest")
    # Рисуем 20-метровую сетку пикселей
    for gx in np.arange(kb[0], kb[2] + 1, 20.0):
        ax.axvline(gx, color="white", alpha=0.22, lw=0.5)
    for gy in np.arange(kb[1], kb[3] + 1, 20.0):
        ax.axhline(gy, color="white", alpha=0.22, lw=0.5)
    _plot_polygon_outline(ax, chertok, color="white", lw=1.6)
    _plot_polygon_outline(ax, chertok_buf, color="#FFE600", lw=1.6, ls="--")
    ax.set_title(
        f"D. Почему отсеяно оз. Черток (6 га) — сетка пикселей 20 м\n"
        f"Воды всего {int(k_water.sum())} пикс. (оранжевые = камыш), лес рядом даёт ИК-засветку B08/B03=1,00",
        fontsize=10.5,
        fontweight="bold",
    )
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(
        handles=[
            mpatches.Patch(color=(0.0, 0.9, 1.0, 0.6), label=f"Чистая вода SCL==6 ({int(k_water.sum())} пикс.)"),
            mpatches.Patch(color=(1.0, 0.3, 0.0, 0.7), label=f"Заросшие/неводные внутри буфера ({int((k_in & ~k_water).sum())})"),
        ],
        loc="lower right",
        facecolor="black",
        labelcolor="white",
        fontsize=8.2,
    )

    out_path = DIAG_DIR / "diag_2_pixel_extraction_autopsy.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    print(f"  -> готово за {time.time() - t0:.1f} с: {out_path}", flush=True)
    return out_path


def make_fig3_real_datasets() -> Path:
    """Рисунок 3: Сводка реальных данных Этапов 3 и 5 (NASA POWER, БГУ 2018–2019, GLORIA)."""
    print("[VIS 3/3] Генерация графиков реальных данных NASA POWER, БГУ и GLORIA...", flush=True)
    t0 = time.time()

    meteo = pd.read_parquet(METEO_PARQUET_PATH)
    lab = pd.read_parquet(LAB_CLEAN_PARQUET_PATH)
    naroch_app = pd.read_csv(NAROCH_APP_CSV_PATH)
    gloria = pd.read_parquet(GLORIA_PARQUET_PATH)

    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.2), dpi=160)

    # --- A: NASA POWER 2018–2026 (Нарочь) ---
    ax = axes[0]
    m_n = meteo[meteo["water_body_id"] == "naroch"].copy()
    m_n["dt"] = pd.to_datetime(m_n["date"])
    m_n["t14"] = m_n["temp_mean_c"].rolling(14, min_periods=1).mean()
    ax.plot(m_n["dt"], m_n["temp_mean_c"], color="#7FA8D9", alpha=0.35, lw=0.6, label="Суточная T (°C)")
    ax.plot(m_n["dt"], m_n["t14"], color="#D94E2A", lw=1.4, label="Скользящая T за 14 дн.")
    ax.axhline(22.0, color="#B00020", ls="--", lw=1.0, label="Порог жары 22 °C")
    ax.set_title("A. Реальная погода NASA POWER (оз. Нарочь)\n2018–2026: 3 241 день подряд, 0 пропусков", fontsize=10.5, fontweight="bold")
    ax.set_ylabel("Температура воздуха на 2 м, °C")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8.2, loc="lower left")

    # --- B: Реальные лабораторные пробы БГУ 2018–2019 ---
    ax = axes[1]
    styles = {
        "naroch": ("Нарочь (олиго-мезотр.)", "#1E88E5", "o"),
        "myastro": ("Мястро (мезотр.)", "#43A047", "s"),
        "batorino": ("Баторино (эвтроф.)", "#E53935", "^"),
    }
    lab["dt"] = pd.to_datetime(lab["sample_date"])
    for lid, (lbl, col, mk) in styles.items():
        sub = lab[lab["water_body_id"] == lid].sort_values("dt")
        ax.plot(sub["dt"], sub["chl_a_lab_ug_l"], color=col, marker=mk, lw=1.8, ms=5.5, label=lbl)
    ax.axhline(15.0, color="#F57C00", ls="--", lw=1.0, label="Порог внимания (15 мкг/л)")
    ax.set_title(
        f"B. Реальные анализы БГУ 2018–2019 (Табл. 2.17)\n"
        f"36 проб (0,4 мкм) + {len(naroch_app)} проб Нарочи 2004–2018",
        fontsize=10.5,
        fontweight="bold",
    )
    ax.set_ylabel("Лабораторный хлорофилл-а, мкг/л")
    ax.grid(alpha=0.25)
    ax.tick_params(axis="x", rotation=25)
    ax.legend(fontsize=8.0, loc="upper right")

    # --- C: Мировой эталон GLORIA (3 368 внутренних озёр) ---
    ax = axes[2]
    gl_lakes = gloria[gloria["is_inland_lake"]].copy()
    sc = ax.scatter(
        gl_lakes["ndci"],
        gl_lakes["chl_a_ug_l"],
        c=np.clip(gl_lakes["b04_b02_ratio"], 0.2, 2.5),
        cmap="viridis",
        s=9,
        alpha=0.55,
        edgecolors="none",
    )
    ax.set_yscale("log")
    ax.set_xlim(-0.45, 0.75)
    ax.set_ylim(0.1, 450)
    cbar = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.03)
    cbar.set_label("Отношение B04/B02", fontsize=8.5)
    ax.set_title(
        f"C. Мировой эталон GLORIA ({len(gl_lakes):,} внутренних озёр)\n"
        f"Цвет воды Sentinel-2 (NDCI + B04/B02) → Chl-a",
        fontsize=10.5,
        fontweight="bold",
    )
    ax.set_xlabel("Индекс NDCI = (B05 − B04) / (B05 + B04)")
    ax.set_ylabel("Лабораторный Chl-a (мкг/л, лог. шкала)")
    ax.grid(alpha=0.25, which="both")

    out_path = DIAG_DIR / "diag_3_meteo_lab_gloria_data.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    print(f"  -> готово за {time.time() - t0:.1f} с: {out_path}", flush=True)
    return out_path


def main() -> None:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    meta, geoms_utm = load_all_15_utm()
    make_fig1_overview(meta, geoms_utm)
    make_fig2_autopsy(meta, geoms_utm)
    make_fig3_real_datasets()
    print(f"\n[VIS] Все 3 диагностические картинки сохранены в {DIAG_DIR}")


if __name__ == "__main__":
    main()
