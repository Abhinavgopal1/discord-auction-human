FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUCTION_DATA_DIR=/data \
    ENABLE_KEEP_ALIVE=false

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY auction_engine.py bot.py classic_modes.py game_engine.py game_modes.py keep_alive.py storage.py ./
COPY players/ ./players/

RUN mkdir -p /data

CMD ["python", "bot.py"]
