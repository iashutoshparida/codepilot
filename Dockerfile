# Runs the API as a non-root user. Mount the repositories to analyse under /workspace.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEPILOT_ALLOWED_ROOTS=/workspace \
    CODEPILOT_STATE_DIR=/home/codepilot/.codepilot

RUN useradd --create-home --uid 10001 codepilot
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[dev]" && mkdir -p /workspace && chown codepilot /workspace

USER codepilot
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health'); sys.exit(0)"
CMD ["uvicorn", "codepilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
