FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates && \
    curl -L https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-linux-64.zip -o /tmp/xray.zip && \
    unzip -o /tmp/xray.zip -d /usr/local/bin xray geoip.dat geosite.dat && \
    chmod +x /usr/local/bin/xray && \
    rm /tmp/xray.zip && \
    apt-get purge -y unzip curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
