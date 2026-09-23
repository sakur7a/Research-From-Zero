FROM python:3.13.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend \
    RE0_HOST=0.0.0.0 RE0_PORT=8000 RE0_DB=/app/.data/re0.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 re0 && mkdir -p /app/.data && chown -R re0:re0 /app
COPY --chown=re0:re0 backend/re0 /app/backend/re0
COPY --chown=re0:re0 web /app/web
COPY --chown=re0:re0 scripts/backup.py /app/scripts/backup.py
COPY --chown=re0:re0 scripts/restore.py /app/scripts/restore.py
COPY --chown=re0:re0 run.py /app/run.py
USER re0
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" || exit 1
CMD ["python", "run.py"]
