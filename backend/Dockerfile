FROM python:3.12-slim

# Системные зависимости (для asyncpg, Pillow, etc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала requirements (для кэша слоёв)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом весь код
COPY . .

# Health-check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
