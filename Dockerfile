FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    ORDERS_DIR=/data/orders

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tesla_ro ./tesla_ro
RUN pip install --no-cache-dir . && mkdir -p /data/orders

EXPOSE 8000
CMD ["python", "-m", "tesla_ro.web"]
