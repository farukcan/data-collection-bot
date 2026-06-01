FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py questions.json ./

ENV DB_PATH=/data/answers.db
VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
