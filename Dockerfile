FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY kalshi_pbot ./kalshi_pbot

RUN pip install --no-cache-dir .

CMD ["kalshi-pbot", "run", "--dry-run", "--mock"]
