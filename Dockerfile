FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN mkdir -p /app/data
ENV PYTHONUNBUFFERED=1
# По умолчанию — долгоживущий процесс: команды в Telegram + проверка сделок.
# Для варианта «только бот по cron» переопределите команду: python3 bot.py --once
CMD ["python3", "worker.py"]
