FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN mkdir -p /app/data
ENV PYTHONUNBUFFERED=1
CMD ["python3", "bot.py"]
