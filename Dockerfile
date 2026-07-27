FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole app (bot/, panel/, services/, storage/, texts/, data/).
# .dockerignore keeps secrets (.env), the SQLite DB, tests, and VCS out.
COPY . .

CMD ["python", "-m", "bot.main"]
