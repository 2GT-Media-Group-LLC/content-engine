# GUI-only container. The pipeline (engine collect / engine run) runs on the host
# so it can hit local Ollama; this container just serves the FastAPI feedback UI
# over the same SQLite DB (bind-mounted from the host).
FROM python:3.12-slim

WORKDIR /app

# System deps for SQLite + lancedb arrow backend.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# Install runtime deps. We do NOT install Ollama/composio here — this container
# is read-mostly over the shared DB.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    'fastapi>=0.110' 'uvicorn[standard]>=0.29' 'jinja2>=3.1' \
    'python-multipart>=0.0.9' 'pydantic>=2.6' 'httpx>=0.27' \
    'python-dotenv>=1.0' 'rich>=13.7' 'typer>=0.12'

COPY src/ ./src/
ENV PYTHONPATH=/app/src

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8080/health',timeout=2)" \
      || exit 1

CMD ["python", "-m", "uvicorn", "content_engine.web.app:app", \
     "--host", "0.0.0.0", "--port", "8080"]
