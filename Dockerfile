ARG $MAMBA_USER=mambauser

FROM python:3.14-bookworm AS base

ENV CACHE_DIR=/app/cache

WORKDIR /app

COPY environment.yaml ./
RUN --mount=type=cache,target=$CACHE_DIR apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && poetry_version=2.4.1 \
    && pip install poetry==$poetry_version

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/app/poetry_cache

COPY pyproject.toml ./
COPY alembic.ini ./
#poetry.lock
# The `k8s` extra pulls in the Kubernetes client, which is required only when
# UMP_RESULTSTORE_CONFIG_BACKEND=k8s (result storage writes ldproxy entity
# YAMLs as ConfigMaps instead of files).  It is installed unconditionally so
# one image can serve both backends — the module is imported lazily, so a
# deployment on the filesystem backend never loads it.
RUN poetry lock && poetry install --without=dev --no-root --extras k8s

# maybe needed for psycopg2
# RUN apt update \
#     && apt upgrade -y \
#     && apt install -qq -y --no-install-recommends \
#     libpq-dev gdal-bin libgdal-dev \
#     && apt clean

COPY src ./src
COPY migrations ./migrations
RUN touch README.md \
    && poetry build \
    && /app/.venv/bin/python -m pip install dist/*.whl 
    #--no-deps

FROM python:3.14-slim-bookworm AS runtime

ARG USER_UID=1000
ARG USERNAME=pythonuser
ARG USER_GID=2000
ARG SOURCE_COMMIT
ARG IMAGE_TAG=3.0.0-alpha8

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

LABEL maintainer="Urban Data Analytics" \
    name="analytics/urban-model-platform" \
    source_commit=$SOURCE_COMMIT \
    version=${IMAGE_TAG}

# add user and group
RUN groupadd --gid $USER_GID $USERNAME && \
    useradd --create-home --no-log-init --gid $USER_GID --uid $USER_UID --shell /bin/bash $USERNAME && \
    chown -R $USERNAME:$USERNAME /home/$USERNAME /usr/local/lib /usr/local/bin

USER $USERNAME
WORKDIR /app

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=base \
    --chmod=0755 \
    --chown=$USERNAME:$USERNAME \
    /app/.venv /app/.venv

COPY scripts/entrypoint.sh entrypoint.sh
COPY --from=base /app/migrations migrations
COPY --from=base /app/alembic.ini alembic.ini

EXPOSE 8000

ENTRYPOINT [ "/app/entrypoint.sh" ]
