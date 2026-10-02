# API Consumers (MVP)
PY ?= python3
CLUSTER ?= paas-arqlab
IMAGE ?= quay.io/ferlukobgal/bgal-api-consumers
TAG ?= $(shell $(PY) -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])" 2>/dev/null || echo dev)
CHART := deploy/charts/api-consumers
V := deploy/values
# Capas para un cluster: bg.tier/bg.domain/bg.env salen del bloque cluster de values/clusters/<c>.yaml
TIER   = $(shell awk '/^  tier:/{print $$2; exit}' $(V)/clusters/$(CLUSTER).yaml)
DOMAIN = $(shell awk '/^  domain:/{print $$2; exit}' $(V)/clusters/$(CLUSTER).yaml)
ENVN   = $(shell awk '/^  env:/{print $$2; exit}' $(V)/clusters/$(CLUSTER).yaml)
VALUES = $(foreach f,groups/all groups/tier-$(TIER) groups/domain-$(DOMAIN) groups/env-$(ENVN) profiles/apim-toolkit clusters/$(CLUSTER),$(if $(wildcard $(V)/$(f).yaml),-f $(V)/$(f).yaml))

.PHONY: install test lint fmt run-dev it-vault image push helm-lint helm-template

install:            ## dependencias de desarrollo
	$(PY) -m pip install -e '.[dev]'

test:               ## unit + contrato (sin infraestructura)
	$(PY) -m pytest -q

lint:
	ruff check src tests && ruff format --check src tests && shellcheck -S warning deploy/scripts/*.sh

fmt:
	ruff format src tests && ruff check --fix src tests

run-dev:            ## API local en :8081 (token "dev"): Vault en memoria, API Subscriber simulada
	APICON_VAULT_MODE=memory APICON_SUBSCRIBER_MODE=fake APICON_API_TOKENS=dev APICON_ROTATION_POLL_S=0.5 \
	$(PY) -m uvicorn api_consumers.main:create_app --factory --reload --port 8081

it-vault:           ## integración contra un Vault real (APICON_IT_VAULT_ADDR/ROLE_ID/SECRET_ID)
	$(PY) -m pytest -q tests/test_vault_integration.py

image:
	podman build -t $(IMAGE):$(TAG) -f Containerfile .

push:
	podman push $(IMAGE):$(TAG)

helm-lint:
	helm lint $(CHART) $(VALUES)

helm-template:      ## render con la jerarquía de 7 capas: make helm-template CLUSTER=paas-arqlab
	@echo "# capas: $(VALUES)" >&2
	helm template api-consumers $(CHART) $(VALUES)
