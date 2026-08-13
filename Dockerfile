# Shunkan in a container.
#
# Two things shape this image. It holds a live broker session, so it runs as a
# non-root user and the compose file publishes to 127.0.0.1 only. And Kite's
# OAuth redirect lands on the HOST at 127.0.0.1:8722, so that port is published
# too or `shunkan connect` can never complete.

FROM python:3.12-slim AS base

# curl is for the healthcheck; nothing here compiles, so no build toolchain.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SHUNKAN_HOME=/data

WORKDIR /app

# Dependencies first so a source edit does not re-resolve the whole tree.
COPY pyproject.toml README.md ./
COPY src/shunkan/__init__.py src/shunkan/__init__.py
RUN pip install --no-cache-dir -e . 2>/dev/null || true

COPY src/ src/
RUN pip install --no-cache-dir -e .

# Non-root. /data is the mounted volume holding credentials, the book and the
# parquet archive, so it must be writable by this user and nobody else.
RUN useradd --create-home --uid 10001 shunkan \
 && mkdir -p /data \
 && chown -R shunkan:shunkan /data /app
USER shunkan

EXPOSE 8720 8722

# 0.0.0.0 is required for Docker to route a published port. cmd_serve detects
# the container and allows it with a warning rather than refusing; see the
# compose file for the host-side binding that actually contains it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8720/api/status || exit 1

ENTRYPOINT ["shunkan"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8720", "--no-browser"]
