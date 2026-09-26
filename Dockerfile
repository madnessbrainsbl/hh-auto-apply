FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HH_DATA_DIR=/data CHROME_BINARY=/usr/bin/chromium CHROMEDRIVER=/usr/bin/chromedriver
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver fonts-dejavu fonts-liberation xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 hh && mkdir /data /app && chown hh:hh /data /app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER hh
CMD ["python", "hh.py", "schedule", "--headless", "--every-minutes", "240"]
