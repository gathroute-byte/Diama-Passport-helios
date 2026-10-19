FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    useradd --create-home --uid 10001 helios
COPY helios_backtest.py cloud_app.py ./
USER helios
EXPOSE 8080
CMD ["sh", "-c", "uvicorn cloud_app:app --host 0.0.0.0 --port ${PORT:-8080}"]
