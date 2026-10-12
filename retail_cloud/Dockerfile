FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8080 DATABASE_PATH=/data/platform.db
VOLUME /data
EXPOSE 8080
CMD ["sh", "-c", "mkdir -p /data && gunicorn -b 0.0.0.0:${PORT} app:app"]
