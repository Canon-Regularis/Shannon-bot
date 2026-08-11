# Build the package into a virtualenv, then carry only that venv into the runtime image, so
# neither pip's cache nor the build inputs end up in the shipped layer.
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY shannon ./shannon
RUN pip install .


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
