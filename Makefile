.ONESHELL:
SHELL=/bin/bash

.PHONY: run-migrations build initiate-dev build-image upload-image start-dev \
        start-dev-example restart-dev stop-dev build-docs clean-docs \
        start-geoserver-db stop-geoserver-db

config ?= .env

# Überprüfen und erstellen der .env-Datei, falls sie nicht existiert
$(shell [ -f $(config) ] || cp .env.example $(config))

include $(config)
export $(shell sed 's/=.*//' $(config))

# Note that the extra activate is needed to ensure that the activate floats env to the front of PATH
CONDA_ACTIVATE=source $$(conda info --base)/etc/profile.d/conda.sh ; conda activate; conda activate

GIT_COMMIT := $(shell git rev-parse --short HEAD)

run migrations:
	PYTHONPATH=src UMP_DATABASE_URL=postgresql+asyncpg://ump:ump@api-db:5432/ump .venv/bin/alembic upgrade head

initiate-dev:
	@if [ ! -d ./.venv ]; then \
		echo 'Creating conda environment in ./.venv'; \
		conda env create -f environment.yaml -p ./.venv; \
	else \
		echo 'Conda environment (./.venv) already present'; \
    fi

	@if [ ! -f providers.yaml ]; then \
		cp providers.yaml.example providers.yaml \
		echo 'Creating providers.yaml from providers.yaml.example'; \
	else \
		echo 'providers.yaml already present'; \
	fi

	@if [ ! -f .env ]; then \
		cp .env.example .env \
		echo 'Creating .env from .env.example'; \
	else \
		echo '.env already present'; \
	fi

	@ echo 'Creating docker network for development'
	docker network create ump_dev

	@echo 'Installing app dependencies:'
	poetry install

build-image:
	@echo 'Building release ${CONTAINER_REGISTRY}/${CONTAINER_NAMESPACE}/$(IMAGE_NAME):$(IMAGE_TAG)'
# build your image
	docker compose -f docker-compose-build.yaml build \
	--build-arg SOURCE_COMMIT=$(GIT_COMMIT) \
	--build-arg TAG=$(IMAGE_TAG) \
	api

upload-image: build-image
	docker compose -f docker-compose-build.yaml push api

start-dev:
	@ echo 'Starting development environment containers: ump database, geoserver database, geoserver, keycloak, keycloak database'
	docker compose -f docker-compose-dev.yaml up -d api-db geoserver-db keycloak kc-db geoserver
	
	@ echo 'Waiting for databases to be ready'
	sleep 10

	@ echo 'Activating conda environment and running flask commands'
	$(CONDA_ACTIVATE) ./.venv && \
	FLASK_APP=src/ump/main.py flask db upgrade && \
	FLASK_APP=src/ump/main.py flask db current
	
	@ echo 'Starting Flask development server...'
	$(CONDA_ACTIVATE) ./.venv && \
	FLASK_APP=src/ump/main.py FLASK_ENV=development flask run --host=0.0.0.0 --port=5000 --debug


start-dev-example: start-dev
	@echo 'Starting development environment containers: ump database, geoserver, keycloak, keycloak database and an example modelserver'
	docker compose -f docker-compose-dev.yaml up -d modelserver

restart-dev:
	docker compose -f docker-compose-dev.yaml restart

stop-dev:
	docker compose -f docker-compose-dev.yaml stop

clean-dev:
	@echo 'Removing dev containers AND volumes. All data is lost!'
	docker compose -f docker-compose-dev.yaml down --volumes

build-docs:
	jupyter-book build docs

clean-docs:
	jupyter-book clean docs

# Update app version: bump major, minor, or patch
bump-app-version:
	@if [ -z "$(part)" ]; then \
		echo "Usage: make bump-app part={major|minor|patch}"; \
		exit 1; \
	fi; \
	bump-my-version bump $(part)

# Update app version: set to a specific version
set-app-version:
	@if [ -z "$(version)" ]; then \
		echo "Usage: make set-app-version version={version}"; \
		exit 1; \
	fi; \
	bump-my-version set $(version)

# Update chart version: bump major, minor, or patch
bump-chart-version:
	@if [ -z "$(part)" ]; then \
		echo "Usage: make bump-chart part={major|minor|patch}"; \
		exit 1; \
	fi; \
	(cd charts && bump-my-version bump $(part))