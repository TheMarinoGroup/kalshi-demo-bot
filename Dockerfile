FROM node:22-bookworm-slim AS hud
WORKDIR /src
COPY hud/package.json hud/package-lock.json* ./hud/
COPY hud ./hud
COPY kalshi_pbot ./kalshi_pbot
WORKDIR /src/hud
RUN npm install && npm run build

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY kalshi_pbot ./kalshi_pbot
COPY --from=hud /src/kalshi_pbot/hud_static ./kalshi_pbot/hud_static

RUN pip install --no-cache-dir .

EXPOSE 8080
CMD ["kalshi-pbot", "hud"]
