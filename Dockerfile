FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV DEPO_DOSYA=/data/tefas-depo.json
VOLUME ["/data"]
EXPOSE 3000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-3000}"]
