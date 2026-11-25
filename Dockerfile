# Контейнер для переносимого запуска Части 1 (ETL, Sentinel-2 COG, OSM, NASA POWER, PostGIS)
# и CLI графа знаний без ручной установки GDAL/Rasterio на ноутбуках команды.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала ставим зависимости (кэшируется Docker-слоем)
COPY requirements.txt pyproject.toml ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Минимальная runtime-библиотека libexpat1, нужная для wheel rasterio/GDAL в python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Копируем исходный код, панель и конфигурацию (папки data/ и configs/ также можно монтировать томом)
COPY compose.yaml ./
COPY configs ./configs
COPY dags ./dags
COPY frontend ./frontend
COPY src ./src
COPY tests ./tests

CMD ["python", "-m", "src.etl.load_to_postgis"]
