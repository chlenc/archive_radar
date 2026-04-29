FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./
COPY app ./app
COPY brands.yaml ./brands.yaml

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data /app/state /app/logs /app/debug

CMD ["python", "-m", "app", "worker"]

