# Образ веб-приложения приёмной комиссии ФАКТ МФТИ.
# Python 3.9 — как в разработке (совместимость pandas/xlrd проверена).
FROM python:3.9-slim

# libgomp1 нужен numpy/pandas в slim-образе.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Корень проекта = /app, чтобы project_root() находил config/ и data/.
WORKDIR /app

# Сначала зависимости (кэш слоёв): ставим пакет в editable-режиме.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -e .

# Конфиги (без секретов) и папки данных (реальные данные монтируются томом).
COPY config ./config
RUN mkdir -p data/input data/output data/db

EXPOSE 8000
# waitress слушает 0.0.0.0:8000 (см. admissions.webapp:main)
CMD ["admissions-web"]
