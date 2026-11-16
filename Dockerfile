FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN pip install --no-cache-dir --upgrade pip uv

COPY pyproject.toml uv.lock README.md ./
COPY config/ ./config/
COPY examples/ ./examples/
COPY src/ ./src/
COPY tests/ ./tests/

RUN uv pip install --system -e ".[dev]" && \
    playwright install chromium

ENTRYPOINT ["python", "-m", "stays_automation"]
CMD ["--help"]
