"""Схемы ответов API (Pydantic). Это «контракт» между Частью 4 и остальными частями."""
from datetime import date as Date
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Mode(str, Enum):
    satellite = "satellite"
    lab = "lab"


class AlertLevel(str, Enum):
    GREEN = "GREEN"
    RED = "RED"


class BBox(BaseModel):
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float


class WaterBodyInfo(BaseModel):
    id: str
    name: str
    type: str = Field(description="reservoir | lake")
    lat: Optional[float] = None
    lon: Optional[float] = None
    bbox: Optional[BBox] = None
    geometry: Optional[dict] = Field(None, description="Полигон озера (GeoJSON Polygon/MultiPolygon)")
    buffer_geometry: Optional[dict] = Field(None, description="Внутренний буфер −25 м от берега и островов для расчёта Sentinel-2")
    # паспорт водоёма (из configs/water_bodies.yaml)
    group: Optional[str] = None
    basin: Optional[str] = None
    notes: Optional[str] = None
    area_km2: Optional[float] = None
    mean_depth_m: Optional[float] = None
    max_depth_m: Optional[float] = None
    volume_mln_m3: Optional[float] = None
    watershed_km2: Optional[float] = None
    watershed_ratio: Optional[float] = None
    protected_area: Optional[str] = Field(None, description="ООПТ / режим охраны, если задан в конфиге")
    has_lab_data: Optional[bool] = None
    chemical_treatment_allowed: Optional[bool] = None
    stratification_type: Optional[str] = None
    cascade_inflow_from: List[str] = Field(default_factory=list)
    cascade_outflow_to: Optional[str] = None
    attention_ug_l: float = 15.0
    bloom_ug_l: float = 25.0
    # какие режимы реально доступны для этого озера
    modes: List[Mode] = Field(default_factory=list, description="satellite и/или lab")


class ObservationInfo(BaseModel):
    """Метаданные последнего реального наблюдения: дата, статус, объём данных, источник."""
    date: Date
    obs_status: Optional[str] = None
    is_interpolated: bool = False
    scenes_in_decade: Optional[int] = None
    valid_water_pixels: Optional[int] = None
    water_coverage_pct: Optional[float] = None
    source: Optional[str] = Field(None, description="Откуда взято наблюдение (Sentinel-2 L2A / проба БГУ / CSV)")
    scene_id: Optional[str] = Field(None, description="Номер сцены Sentinel-2, если он есть в витрине")


class AlertResponse(BaseModel):
    water_body_id: str
    water_body_name: str
    mode: Mode = Mode.satellite
    requested_date: Date
    date_used: Date = Field(description="Реальная декада, по которой сделан прогноз")
    horizon_days: int = 10
    chl_a_current: Optional[float] = Field(description="Хлорофилл-a сейчас, мкг/л")
    obs_status: Optional[str] = Field(None, description="valid | interpolated_1step | cloudy_missing | ice_cover | insufficient_pixels")
    is_interpolated: Optional[bool] = None
    observed_tsi: Optional[float] = Field(None, description="Индекс Карлсона по фактическому наблюдению")
    observed_trophic_class: Optional[str] = Field(None, description="Трофический класс по фактическому наблюдению")
    chl_a_forecast: Optional[float] = Field(None, description="Прогноз хлорофилла-a через horizon_days, мкг/л (нет, если нет наблюдения)")
    pred_low_ug_l: Optional[float] = Field(None, description="Нижняя граница прогноза, мкг/л (±MAE модели)")
    pred_high_ug_l: Optional[float] = Field(None, description="Верхняя граница прогноза, мкг/л (±MAE модели)")
    uncertainty_ug_l: Optional[float] = Field(None, description="±MAE модели, взятая из model_metrics.json")
    bloom_probability: Optional[float] = Field(None, ge=0, le=1, description="Вероятность цветения (>=25 мкг/л)")
    attention_probability: Optional[float] = Field(None, ge=0, le=1, description="Вероятность уровня внимания (>=15 мкг/л), если модель её отдаёт")
    trophic_index: Optional[float] = Field(None, description="Индекс Карлсона")
    trophic_class: Optional[str] = None
    alert_level: Optional[AlertLevel] = Field(
        None,
        description="2 уровня тревоги (GREEN — ниже порога, RED — предупреждение ≥25 мкг/л); None, если на декаду нет наблюдения",
    )
    reasons: List[str]
    recommendations: List[str]
    temp_mean_14d: Optional[float] = None
    calm_fraction_7d: Optional[float] = None
    precip_sum_14d: Optional[float] = None
    wind_mean_7d: Optional[float] = None
    radiation_mean_7d: Optional[float] = None
    ndci_current: Optional[float] = None
    b04_b02_ratio: Optional[float] = None
    secchi_depth_m: Optional[float] = None
    total_phosphorus_ug_l: Optional[float] = None
    observation: Optional[ObservationInfo] = None
    sources: List[str] = Field(default_factory=list, description="Реальные источники, использованные для этого ответа")
    notes: List[str] = Field(default_factory=list, description="Оговорки: чего не хватило (например, нет MAE для интервала)")


class HistoryPoint(BaseModel):
    date: Date
    observed: Optional[float] = Field(None, description="Реальное наблюдение на эту декаду, мкг/л")
    obs_status: Optional[str] = None
    is_interpolated: bool = False
    forecast: Optional[float] = Field(None, description="Прогноз модели на +10 дней от этой декады, мкг/л")
    forecast_low: Optional[float] = None
    forecast_high: Optional[float] = None


class HistoryResponse(BaseModel):
    water_body_id: str
    mode: Mode
    horizon_days: int = 10
    points: List[HistoryPoint]
    sources: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class LabUploadRequest(BaseModel):
    water_body_id: str
    sample_date: Date
    chl_a_lab_ug_l: float = Field(gt=0, le=1000, description="Лабораторный хлорофилл-а, мкг/л (физический диапазон)")
    secchi_depth_m: Optional[float] = Field(None, gt=0, le=50, description="Прозрачность по диску Секки, м")
    total_phosphorus_ug_l: Optional[float] = Field(None, gt=0, le=1000, description="Общий фосфор, мкг/л")
    source_note: str = Field("Загружено через веб-панель", max_length=300)


class LabUploadResponse(BaseModel):
    saved: bool
    message: str
    path: str
    record: dict
    satellite_comparison: Optional[dict] = Field(None, description="Ближайшее спутниковое наблюдение для сверки, если есть")
