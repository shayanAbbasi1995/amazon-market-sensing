FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install .

COPY docker/entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# config.yaml, .env and data/ are mounted at runtime, never baked in.
ENV AMSENSE_CONFIG=/app/config.yaml
ENTRYPOINT ["/app/entrypoint.sh"]
