FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при изменении requirements,
# а не при каждой правке кода.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

# Приложение работает не от root.
RUN useradd --create-home --uid 1000 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "app.bot.main"]
