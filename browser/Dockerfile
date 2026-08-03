# Officielt Playwright-image med Chromium pre-installeret (linux/amd64 + linux/arm64)
# Playwright 1.49, Ubuntu Noble (24.04)
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir playwright==1.49.0 fastapi==0.115.0 "uvicorn[standard]==0.30.6" httpx==0.27.2 cryptography==42.0.8

COPY . .

EXPOSE 8030

CMD ["python3", "-u", "browser_server.py"]
