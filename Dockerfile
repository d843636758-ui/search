FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt && python -m playwright install --with-deps chromium
COPY server.py /app/server.py
RUN mkdir -p /data/browser-profile
VOLUME ["/data"]
ENV PORT=8080 BROWSER_PROFILE_DIR=/data/browser-profile HEADLESS=true
EXPOSE 8080
CMD ["sh","-c","uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers"]
