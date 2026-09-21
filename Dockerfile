FROM python:3.12-slim-bookworm

ARG RELEASE_SHA=unknown

LABEL org.opencontainers.image.title="Commodity Research Platform" \
      org.opencontainers.image.revision="$RELEASE_SHA"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-production.txt pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements-production.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY seeds ./seeds
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps -e . \
    && groupadd --gid 10001 commodity \
    && useradd --uid 10001 --gid commodity --no-create-home --shell /usr/sbin/nologin commodity \
    && mkdir -p /app/data /var/lib/commodity-research-platform/data /var/lib/commodity-research-platform/exports \
    && chown -R commodity:commodity /app/data /var/lib/commodity-research-platform

COPY --chmod=0755 deploy/container/entrypoint.sh /usr/local/bin/commodity-entrypoint

USER commodity

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)"]

ENTRYPOINT ["commodity-entrypoint"]
CMD ["python", "-m", "uvicorn", "tin.web.app:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
