# Build the package into a virtualenv, then carry only that venv into the runtime image, so
# neither pip's cache nor the build inputs end up in the shipped layer.
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY shannon ./shannon

# Dependencies come from the lock, not from resolving the ranges in pyproject.toml afresh.
# Every range here is a bare lower bound, so building without the lock ships whatever PyPI
# happens to serve that day: a different set from the one CI tested and the one pip-audit
# checked, which also makes the build provenance attestation describe something nobody can
# reproduce from the commit it names.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --python /opt/venv/bin/python --require-hashes -r /tmp/requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-deps .


FROM python:3.14-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SHANNON_API_HOST=0.0.0.0 \
    SHANNON_API_PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Migrations are not part of the installed package, so they are copied in for `alembic upgrade`.
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 shannon && chown -R shannon:shannon /app
USER shannon

EXPOSE 8000

# A plain TCP connect, because the service deliberately exposes no health route.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), 3).close()"

CMD ["python", "-m", "shannon.main"]
