FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY a.py .
RUN mkdir -p /data \
    && useradd --create-home --uid 10001 bot \
    && chown -R bot:bot /app /data

USER bot

ENV BOT_STATE_PATH=/data/bot-state.pkl

EXPOSE 10000

CMD ["python", "a.py"]
