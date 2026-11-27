"""
Часть 4: REST API на FastAPI.
Запуск:  uvicorn src.api.main:app --reload     Документация: http://127.0.0.1:8000/docs
Панель:  http://127.0.0.1:8000/dashboard/

Маршруты по PROJECT_GUIDE.md:
  GET  /lakes                          — водоёмы, паспорта и GeoJSON-полигоны
  GET  /predict/{lake_id}?date&mode    — прогноз + интерпретация риска (satellite|lab)
  GET  /history/{lake_id}?mode&limit   — реальные наблюдения + прогнозы для графика
  GET  /metrics                        — метрики моделей и отчёт качества данных
  POST /lab/upload                     — добавить лабораторную пробу с проверкой диапазонов
"""
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from src.api import adapters, config, pipeline
from src.api.pipeline import HORIZON_DAYS, build_components, modes_for, refresh_part1, run_pipeline
from src.api.schemas import (AlertResponse, HistoryResponse, LabUploadRequest, LabUploadResponse,
                             Mode, WaterBodyInfo)

LAB_COLUMNS = ["water_body_id", "sample_date", "chl_a_lab_ug_l", "secchi_depth_m",
               "total_phosphorus_ug_l", "source_note"]
_upload_lock = threading.Lock()


def comps(request: Request) -> dict:
    """Компоненты; реестр, витрины и полигоны перечитываются, если файлы изменились (добавлено озеро)."""
    c = request.app.state.c
    refresh_part1(c)
    return c


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Модели и граф грузим ОДИН раз при старте; реестр/витрины обновляются при изменении файлов
    app.state.c = build_components()
    yield


app = FastAPI(title="Мониторинг эвтрофикации водоёмов РБ", version="1.0.0", lifespan=lifespan)
# CORS: панель, открытая как файл, обращается к API; POST нужен для загрузки лабораторной пробы
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/")
def root(request: Request):
    c = comps(request)
    return {"service": "eutrophication-monitor", "status": "ok", "docs": "/docs",
            "dashboard": "/dashboard/", "horizon_days": HORIZON_DAYS,
            "components": c["status"], "modes": pipeline.mode_catalog(c),
            "files": c.get("paths"), "reload_error": c["reload_error"]}


def _lake_info(c: dict, wb_id: str) -> dict:
    return {**c["registry"][wb_id], "modes": modes_for(c, wb_id)}


@app.get("/lakes", response_model=List[WaterBodyInfo])
def lakes(request: Request):
    """Водоёмы с паспортами и геометрией для карты (алиас /water-bodies для совместимости)."""
    c = comps(request)
    return [_lake_info(c, wb_id) for wb_id in c["registry"]]


@app.get("/water-bodies", response_model=List[WaterBodyInfo], include_in_schema=False)
def water_bodies(request: Request):
    return lakes(request)


@app.get("/timeline")
def timeline_endpoint(request: Request,
                      mode: Mode = Query(Mode.satellite, description="satellite | lab")):
    """Полная линия времени 2018–2026 по всем водоёмам для плавной интерактивной карты."""
    c = comps(request)
    try:
        return pipeline.timeline(c, mode.value)
    except adapters.ModeUnavailable as e:
        raise HTTPException(404, str(e))


@app.get("/metrics")
def metrics(request: Request):
    """Метрики моделей (как есть из model_metrics.json) + отчёт качества + доступность режимов."""
    c = request.app.state.c
    base = c["metrics"] if isinstance(c["metrics"], dict) else {"model_metrics": c["metrics"]}
    out = dict(base)
    out["data_quality"] = c.get("quality")
    out["modes"] = pipeline.mode_catalog(c)
    out["horizon_days"] = HORIZON_DAYS
    out["metrics_file"] = c["metrics_path"].name if c.get("metrics_path") else None
    return out


@app.get("/predict/{water_body_id}", response_model=AlertResponse)
def predict(water_body_id: str, request: Request,
            date: Optional[Date] = Query(None, description="YYYY-MM-DD; по умолчанию — дата по умолчанию"),
            mode: Mode = Query(Mode.satellite, description="satellite — Sentinel-2, lab — лабораторные пробы")):
    c = comps(request)
    if water_body_id not in c["registry"]:
        raise HTTPException(404, f"Водоём '{water_body_id}' не найден. Доступны: {list(c['registry'])}")

    requested = date or config.DEFAULT_DATE
    try:
        res = run_pipeline(c, water_body_id, requested, mode.value)
    except adapters.ModeUnavailable as e:
        raise HTTPException(404, str(e))
    if res is None:
        lo, hi = c["provider"].date_range(water_body_id) if c["provider"] and c["provider"].supports(water_body_id) \
            else ("-", "-")
        raise HTTPException(404, f"Нет данных на {requested}. Диапазон: {lo} .. {hi}")
    return AlertResponse(**res)


@app.get("/history/{water_body_id}", response_model=HistoryResponse)
def history(water_body_id: str, request: Request,
            mode: Mode = Query(Mode.satellite),
            date: Optional[Date] = Query(None, description="Конец периода; по умолчанию — дата по умолчанию"),
            limit: int = Query(120, ge=12, le=400, description="Сколько декад назад показать")):
    c = comps(request)
    if water_body_id not in c["registry"]:
        raise HTTPException(404, f"Водоём '{water_body_id}' не найден. Доступны: {list(c['registry'])}")
    try:
        res = pipeline.history(c, water_body_id, mode.value, date or config.DEFAULT_DATE, limit)
    except adapters.ModeUnavailable as e:
        raise HTTPException(404, str(e))
    return HistoryResponse(**res)


@app.post("/lab/upload", response_model=LabUploadResponse)
def lab_upload(req: LabUploadRequest, request: Request):
    """Проверить и дописать лабораторную пробу в data/lab/lab_measurements.csv.
    В витрину Части 1 она попадёт после пересборки ETL — об этом честно сообщаем в ответе."""
    c = comps(request)
    if req.water_body_id not in c["registry"]:
        raise HTTPException(404, f"Водоём '{req.water_body_id}' не найден. Доступны: {list(c['registry'])}")
    if req.sample_date > Date.today():
        raise HTTPException(422, "Дата пробы не может быть в будущем")
    if req.sample_date.year < 2000:
        raise HTTPException(422, "Дата пробы раньше 2000 года — проверьте формат")

    path = config.find_lab_csv(for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(water_body_id=req.water_body_id, sample_date=req.sample_date.isoformat(),
                  chl_a_lab_ug_l=req.chl_a_lab_ug_l, secchi_depth_m=req.secchi_depth_m,
                  total_phosphorus_ug_l=req.total_phosphorus_ug_l, source_note=req.source_note)
    csv_record = {**record, "sample_type": "web_upload", "date_precision": "exact"}

    with _upload_lock:
        if path.is_file() and path.stat().st_size > 0:
            old = pd.read_csv(path)
            missing = [col for col in ("water_body_id", "sample_date") if col not in old.columns]
            if missing:
                raise HTTPException(409, f"Таблица проб {path.name} не по контракту: нет {missing}")
            dup = (old["water_body_id"].astype(str) == req.water_body_id) & \
                  (pd.to_datetime(old["sample_date"]).dt.date == req.sample_date)
            if dup.any():
                raise HTTPException(409, f"Проба {req.water_body_id} на {req.sample_date} уже есть в таблице")
            row = pd.DataFrame([csv_record]).reindex(columns=old.columns)
            row.to_csv(path, mode="a", header=False, index=False)
        else:
            pd.DataFrame([record], columns=LAB_COLUMNS).to_csv(path, index=False)

    comparison = None
    prov = c["providers"].get("satellite")
    if prov is not None and prov.supports(req.water_body_id) and pipeline._is_real(c, "part1"):
        f = prov.get_features(req.water_body_id, req.sample_date)
        if f and f.get("chl_a_current") is not None:
            chl = f["chl_a_current"]
            sat_pred = c["predictors"]["satellite"]["predictor"]
            if isinstance(sat_pred, adapters.TwoStageModelPredictor) and "_frame" in f:
                chl = round(sat_pred.estimate_current(f), 2)
            comparison = dict(satellite_date=f["date_used"].isoformat(), satellite_chl_a_ug_l=chl,
                              difference_ug_l=round(req.chl_a_lab_ug_l - chl, 2),
                              note="Спутник видит верхний слой воды; проба — интегральная. Расхождение ожидаемо.")
    return LabUploadResponse(
        saved=True, path=str(path), record=record, satellite_comparison=comparison,
        message="Проба сохранена. В витрину Части 1 она попадёт после пересборки ETL (run_pipeline).")


# Веб-панель: статические файлы frontend/ (index.html, style.css, app.js). Данных в них нет:
# страница сама запрашивает маршруты этого же API. Адрес: http://127.0.0.1:8000/dashboard/
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=FRONTEND_DIR, html=True), name="dashboard")
