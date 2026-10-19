.PHONY: help env venv install run docker-build docker-run docker-down seed data test-db test-db-stop bench-db bench-db-stop test lint format clean tree

TAG ?= football-scout-api:dev
VENV ?= .venv
PY := . $(VENV)/bin/activate &&
DUCKDB_PATH ?= data/transfermarkt-datasets.duckdb
DUCKDB_URL ?= https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/transfermarkt-datasets.duckdb
TEST_DB ?= scout-neo4j-test
BENCH_DB ?= scout-neo4j-bench

help:
	@echo "Commandes :"
	@echo "  make env            Crée .env à partir de .env.example (sans écraser)"
	@echo "  make venv           Crée l'environnement virtuel ($(VENV))"
	@echo "  make install        Installe requirements.txt dans $(VENV)"
	@echo "  make run            Lance l'API FastAPI en local sur :8000"
	@echo "  make data           Télécharge le fichier DuckDB Transfermarkt dans data/"
	@echo "  make seed           Charge les données dans Neo4j (scripts.seed_data)"
	@echo "  make docker-build   Construit les images Docker (TAG=$(TAG))"
	@echo "  make docker-run     Démarre la stack (proxy, api, neo4j, ingest)"
	@echo "  make docker-down    Arrête la stack"
	@echo "  make test-db        Démarre le Neo4j de test jetable sur le port 7688"
	@echo "  make test-db-stop   Arrête le Neo4j de test"
	@echo "  make bench-db       Démarre le Neo4j de benchmark sur le port 7689"
	@echo "  make bench-db-stop  Arrête le Neo4j de benchmark"
	@echo "  make test           Lance pytest avec couverture"
	@echo "  make lint           Lance pylint (seuil 9.5)"
	@echo "  make format         Formate le code avec black"
	@echo "  make clean          Supprime caches et fichiers temporaires"
	@echo "  make tree           Affiche l'arborescence (profondeur 3)"

env:
	@if [ ! -f .env ]; then cp .env.example .env; echo ".env créé : remplissez les valeurs"; \
	else echo ".env existe déjà"; fi

venv:
	@if [ ! -d "$(VENV)" ]; then \
	    python3 -m venv $(VENV); \
	    . $(VENV)/bin/activate && pip install --upgrade pip; \
	    echo "Environnement $(VENV) créé"; \
	else echo "$(VENV) existe déjà"; fi
	@echo "Pour l'activer : source $(VENV)/bin/activate"

install: venv
	@$(PY) pip install -r requirements.txt

run:
	@$(PY) uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

data:
	@mkdir -p data
	@if [ -f "$(DUCKDB_PATH)" ]; then echo "$(DUCKDB_PATH) existe déjà"; \
	else curl -L --fail -o "$(DUCKDB_PATH)" "$(DUCKDB_URL)"; fi

seed:
	@$(PY) python -m scripts.seed_data

docker-build: env
	docker compose build

docker-run: env
	docker compose up --build

docker-down:
	docker compose down

test-db:
	@if docker ps --format '{{.Names}}' | grep -q '^$(TEST_DB)$$'; then echo "$(TEST_DB) tourne déjà"; \
	else docker run -d --rm --name $(TEST_DB) -p 7688:7687 \
	    -e NEO4J_AUTH=neo4j/testpassword \
	    -e NEO4J_PLUGINS='["graph-data-science"]' \
	    -e NEO4J_dbms_security_procedures_unrestricted='gds.*' \
	    neo4j:5 && echo "Neo4j de test en démarrage sur bolt://localhost:7688 (environ 30 s)"; fi

test-db-stop:
	-docker stop $(TEST_DB)

bench-db:
	@if docker ps --format '{{.Names}}' | grep -q '^$(BENCH_DB)$$'; then echo "$(BENCH_DB) tourne déjà"; \
	else docker run -d --rm --name $(BENCH_DB) -p 7689:7687 \
	    -e NEO4J_AUTH=neo4j/benchpassword \
	    neo4j:5 && echo "Neo4j de benchmark en démarrage sur bolt://localhost:7689"; fi

bench-db-stop:
	-docker stop $(BENCH_DB)

test:
	@$(PY) pytest -q --cov=app --cov-report=term-missing --cov-report=xml

lint:
	@$(PY) pylint app scripts ml --fail-under=9.5

format:
	@$(PY) black app scripts ml tests -l 120

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} \; || true
	find . -type f -name "*.pyc" -delete || true
	rm -rf .pytest_cache htmlcov .coverage coverage.xml

tree:
	@if command -v tree >/dev/null 2>&1; then \
	    tree -L 3 -I "node_modules|dist|.git|.venv|__pycache__|data"; \
	else find . -maxdepth 3 -type d -not -path '*/\.*' | sort; fi
