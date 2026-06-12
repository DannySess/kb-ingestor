FROM python:3.12-slim

# inotify tools for watchdog's inotify backend
RUN apt-get update && apt-get install -y --no-install-recommends \
    inotify-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingestor.py .

# Mount your NAS folder here
VOLUME ["/watch"]

CMD ["python", "-u", "ingestor.py"]
