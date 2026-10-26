FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    ORDERS_DIR=/data/orders \
    TZ=Europe/Kyiv

# Pango/HarfBuzz — для PDF (WeasyPrint); DejaVu — шрифт із кирилицею.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tesla_ro ./tesla_ro
RUN pip install --no-cache-dir . && mkdir -p /data/orders

EXPOSE 8000
CMD ["python", "-m", "tesla_ro.web"]
