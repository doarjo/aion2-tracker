FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AION2_DB_PATH=/data/aion2.db \
    AION2_HOST=0.0.0.0 \
    AION2_PORT=8080 \
    # Pinned explicitly: the package is installed into site-packages, so
    # package-relative path guessing does not find the repo's config dir.
    AION2_CONFIG_DIR=/app/config \
    AION2_PROJECT_ROOT=/app

WORKDIR /app

# Dependencies first so a code change does not invalidate the layer.
COPY pyproject.toml ./
COPY src/aion2/__init__.py src/aion2/__init__.py
RUN pip install --no-cache-dir .

COPY src/ src/
COPY config/ config/
RUN pip install --no-cache-dir --no-deps .

# SQLite lives on a mounted volume so snapshots survive a rebuild.
VOLUME ["/data"]
EXPOSE 8080

# Runs as a non-root user; /data is chowned by the compose file's init or by
# Unraid's volume permissions.
RUN useradd --create-home --uid 1000 aion2 \
    && mkdir -p /data && chown -R aion2:aion2 /data /app
USER aion2

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8).status==200 else 1)"

CMD ["python", "-m", "aion2", "serve"]
