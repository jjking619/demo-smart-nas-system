FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY api_server.py /app/api_server.py

EXPOSE 8084
CMD ["python3", "/app/api_server.py"]
