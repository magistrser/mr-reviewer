ARG DOCKER_REGISTRY
FROM ${DOCKER_REGISTRY}/python:3.14.3-slim AS build

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1
ENV PYPI_PROXY=https://nexus.office.st-falcon.ru/repository/pypi-proxy/simple/

RUN adduser --system --group app
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip || pip install --upgrade pip -i ${PYPI_PROXY}
RUN pip install uv || pip install uv -i ${PYPI_PROXY}

COPY pyproject.toml uv.lock ./
RUN uv pip install --no-cache -r pyproject.toml

COPY --chown=app:app . .

FROM build AS pipeline

RUN uv pip install --no-cache -r pyproject.toml --group dev

FROM build AS runtime

USER app

ENTRYPOINT exec fastapi run
