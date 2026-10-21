# Football Scout Agent

[![lint](https://github.com/nsalapete/FootballScootAgent_Josh_Edouard_Raphael_Nicolas/actions/workflows/lint.yml/badge.svg)](https://github.com/nsalapete/FootballScootAgent_Josh_Edouard_Raphael_Nicolas/actions/workflows/lint.yml)
[![tests](https://github.com/nsalapete/FootballScootAgent_Josh_Edouard_Raphael_Nicolas/actions/workflows/test.yml/badge.svg)](https://github.com/nsalapete/FootballScootAgent_Josh_Edouard_Raphael_Nicolas/actions/workflows/test.yml)
[![publish](https://github.com/nsalapete/FootballScootAgent_Josh_Edouard_Raphael_Nicolas/actions/workflows/publish.yml/badge.svg)](https://github.com/nsalapete/FootballScootAgent_Josh_Edouard_Raphael_Nicolas/actions/workflows/publish.yml)

A Neo4j knowledge graph of European football transfers (Transfermarkt, 7 leagues since 2015), a FastAPI API on top
of it, graph data science and link prediction, and a **LangGraph recruitment agent**.
Graph Databases project, AIDAMS 3rd year, level 3+ (agentic / GraphRAG).

The agent answers questions such as *"Which left-backs under 23 cost less than 10 M€?"*, *"Where does SC Alvorada
sell its players?"* or *"Which club is Gabriel Rocha predicted to join?"*. Each statement in an answer is labelled
with one of three **levels of truth**, which are never mixed:

| Level | Comes from | Shown as |
| --- | --- | --- |
| Facts | Transfermarkt data (players, clubs, transfers, valuations) and what is computed from them | `verified` |
| Predictions | link-prediction model (`PREDICTED_MOVE`, `PREDICTED_LINK`) | `predicted` |
| Rumours | press articles read by an LLM (`Rumour`), until confirmed by a real transfer | `rumour` |

Every claim carries graph ids as evidence (`Player:12`, `Transfer:t01`, `Rumour:r1`). A deterministic verifier
removes any claim whose evidence or figures cannot be found in the graph.

## Contents

- [Quick start](#quick-start)
- [Make commands](#make-commands)
- [API](#api)
- [Architecture and performance](#architecture-and-performance)
- [Graph model and data](#graph-model-and-data)
- [Graph data science and link prediction](#graph-data-science-and-link-prediction)
- [Agent, guardrails and rumours](#agent-guardrails-and-rumours)
- [Quality](#quality)
- [Repository layout](#repository-layout)
- [Team and contributions](#team-and-contributions)

## Quick start

Requirements: Docker (with Compose), Python 3.11+, `make` (on Windows: Git Bash plus `make`, e.g.
`choco install make`), and about 2 GB of free RAM for Neo4j.

```bash
make env            # creates .env from .env.example: set NEO4J_PASSWORD, API_KEY and the LLM settings
make install        # virtual environment + dependencies
make data           # downloads the Transfermarkt DuckDB file (about 200 MB) into data/
make docker-up      # proxy (:80), api, neo4j (+ GDS), ingest (loads the graph once, about 1 min 15 s)
```

Then open Swagger at <http://localhost/docs>.

**LLM settings in `.env`** (`app/llm/client.py`):

| `LLM_PROVIDER` | Also set | Notes |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY`, `LLM_MODEL_SMALL`, `LLM_MODEL_LARGE` | Claude |
| `openai_compatible` | `LLM_BASE_URL`, `OPENAI_API_KEY`, and the two models | any OpenAI-compatible API (the evaluation used NanoGPT) |
| `fake` | nothing | deterministic, no network; used by the tests and the CI |

The small model routes questions and extracts rumours; the large one plans and writes the answers.

**Production switches:**
- `API_KEY` protects the routes that write to the graph or spend LLM credit (`X-API-Key` header).
- `AGENT_REQUIRE_API_KEY=true` extends that protection to the agent.

The published image can replace a local build:
`API_IMAGE=ghcr.io/nsalapete/football-scout-api:latest docker compose up -d --no-build`
(see [`docs/architecture.md`](docs/architecture.md#image-docker-publiée)).

**Without Docker for the API:** `make run` starts uvicorn on `:8000` against the Neo4j set in `.env`, and
`make seed` loads the graph.

**Offline jobs:**
- `python -m scripts.confirm_rumours` confirms, contradicts or expires pending rumours against the transfers.
- `python -m ml.train_link_prediction` trains the link-prediction model and writes the predictions.
- `python -m scripts.import_articles` imports press rumours (spends LLM credit).

## Make commands

| Command | What it does |
| --- | --- |
| `make help` | List the commands |
| `make env` | Create `.env` from `.env.example` (never overwrites) |
| `make venv` / `make install` | Create the virtual environment / install `requirements.txt` |
| `make data` | Download the Transfermarkt DuckDB file into `data/` |
| `make seed` | Load DuckDB into the Neo4j of `.env` (`scripts.seed_data`, idempotent) |
| `make run` | API in development mode on `:8000` |
| `make docker-build` / `docker-run` / `docker-up` / `docker-logs` / `docker-down` | Build, run in the foreground, run in the background, follow the logs, stop the stack |
| `make test-db` / `make test-db-stop` | Disposable test Neo4j (+ GDS) on port 7688, loaded with the mini graph by pytest |
| `make test` | pytest with coverage |
| `make lint` | pylint on `app scripts ml` (threshold 9.5) |
| `make format` | black |
| `make bench-db` / `bench-data` / `bench` / `bench-db-stop` | Benchmark Neo4j on port 7689, synthetic 50,000-player graph, p50/p95 and `PROFILE` plans |
| `make clean` / `make tree` | Remove caches / show the tree |

## API

Base URL: `http://localhost` behind the proxy, or `http://localhost:8000` with `make run`. 🔑 marks the routes that
require `X-API-Key`. Full examples with real outputs for each route:
[`docs/architecture.md`](docs/architecture.md#exemples-curl-de-chaque-endpoint).

| Route | Purpose | Role |
| --- | --- | --- |
| `GET /api/health` | API and Neo4j status | B |
| `GET /api/players/search` | Players by position, sub-position, max age, max value, competition (cursor pagination) | B |
| `GET /api/players/resolve`, `GET /api/clubs/resolve` | Names to ids through the full-text indexes (tolerates a typo) | B |
| `GET /api/players/{id}`, `GET /api/players/{id}/valuations` | One player; market-value history | B |
| `GET /api/players/path?from=&to=` | Shortest chain of teammates (6 hops at most) | B |
| `GET /api/clubs/{id}/pipeline` | Where a club sells its players, by destination competition | B |
| `GET /api/clubs/hubs`, `GET /api/network/communities` | PageRank hubs, Louvain communities | C |
| `GET /api/players/{id}/similar`, `GET /api/players/{id}/predicted-moves` | FastRP neighbours; predicted destinations | C |
| `POST /api/admin/gds/refresh` 🔑 | Recompute the projections and algorithms | C |
| `GET /api/players/{id}/rumours` | Press rumours with source, status and confidence | D |
| `POST /api/rumours/import` 🔑 | Import articles and extract rumours with the LLM | D |
| `POST /api/agent/ask` (🔑 if `AGENT_REQUIRE_API_KEY=true`) | Ask the agent; the answer exposes claims, removed claims and the tool trace | D |

```bash
BASE=http://localhost
curl "$BASE/api/players/search?position=Defender&sub_position=Left-Back&max_age=23&max_value_eur=15000000&limit=2"
curl "$BASE/api/players/path?from=1&to=4"
curl "$BASE/api/clubs/7/pipeline"
curl -X POST "$BASE/api/agent/ask" -H "Content-Type: application/json" \
     -d '{"question": "How did Lucas Marchand'"'"'s market value evolve?"}'
```

## Architecture and performance

Owner: B. Details in [`docs/architecture.md`](docs/architecture.md) (diagram: `docs/architecture.drawio`) and
[`docs/perf.md`](docs/perf.md).

- **Docker Compose stack.** Four services:
  - `proxy` (nginx), the only published port (80);
  - `api` (FastAPI);
  - `neo4j` (Neo4j 5 + GDS), bound to `127.0.0.1` only;
  - `ingest`, which loads the graph once.

  Healthchecks order the start-up.
- **Layers.** Routers → services → `Neo4jClient`. Routers hold no Cypher, and every query is parameterised and
  runs in a read or write transaction with a timeout. The agent calls the same services as the API.
- **Security.**
  - nginx limits requests to 20 per second per IP, and to 10 per minute for the agent, the GDS refresh and the
    rumour import.
  - Security headers are set.
  - `X-API-Key` is compared in constant time.
  - Secrets live only in `.env`.
- **Published image.** Each push on `main` publishes the API image to GHCR (`latest`, `sha-<commit>`, `v1.0`).
- **Performance.** Measured on a synthetic graph of 50,000 players and 774,000 relationships:
  - every service method answers in under 35 ms at p50 and 70 ms at p95;
  - every query starts from a uniqueness constraint or an index;
  - `docs/perf.md` shows `PROFILE` plans before and after the indexes, cursor against `SKIP` pagination, and
    bounded against unbounded shortest paths.

## Graph model and data

Owner: A. The schema contract is [`docs/schema.md`](docs/schema.md) (constraints and indexes:
`scripts/constraints.cypher`). The modeling rationale, data sources and anti-genericity appendix are in
`docs/modeling.md`, in review in PR #32.

```
(:Player)-[:MADE]->(:Transfer)-[:FROM|TO]->(:Club)-[:PLAYS_IN {season}]->(:Competition)-[:IN_COUNTRY]->(:Country)
(:Player)-[:PLAYS_FOR]->(:Club)   (:Player)-[:HAS_VALUATION]->(:Valuation)   (:Player)-[:CITIZEN_OF]->(:Country)
(:Player)-[:PLAYED_WITH {matches}]-(:Player)            (:Club)-[:SENDS_TO {count, total_fee_eur}]->(:Club)
(:Article)-[:REPORTS]->(:Rumour)-[:ABOUT]->(:Player)    (:Rumour)-[:TARGET]->(:Club)   (:Rumour)-[:CONFIRMED_BY]->(:Transfer)
(:Player)-[:PREDICTED_MOVE {prob}]->(:Club)             (:Club)-[:PREDICTED_LINK {prob}]->(:Club)
```

Key choices:
- **A transfer is a node.** It has three ends, and a confirmed rumour points to it.
- **Appearances are not loaded.** They are aggregated in DuckDB into `PLAYED_WITH` pairs.
- **`SENDS_TO` is materialised** for the club network and GDS.
- **The league depends on the season** (`PLAYS_IN {season}`), because clubs are promoted and relegated.
- **Hypotheses use their own labels and relationship types**, so a query on facts can never see them.

**Data.** [transfermarkt-datasets](https://github.com/dcaribou/transfermarkt-datasets) (CC0, DuckDB file), with the
scope set by `SCOPE_COMPETITIONS` (`GB1, ES1, IT1, L1, FR1, PO1, NL1`) and `SCOPE_FROM_SEASON` (`2015`). Press
articles come from the Guardian and BBC RSS feeds and the GDELT DOC API.

**Ingestion** (`scripts/seed_data.py`, rules in `scripts/data_quality.py`):
- idempotent, extraction in DuckDB, then `MERGE` in batches of 5,000;
- data-quality rules for loans, placeholder clubs, duplicates and future-dated moves;
- a full load takes about 1 min 15 s.

Decisions: [`docs/decisions/a.md`](docs/decisions/a.md).

## Graph data science and link prediction

Owner: C. Details in [`docs/ml_report.md`](docs/ml_report.md) and the notebook [`demo.ipynb`](demo.ipynb).

**GDS** (`app/services/gds.py`) projects the club network and the teammate network, then runs:
- weighted PageRank on `SENDS_TO`, for transfer hubs;
- Louvain, for communities of clubs (transfer pipelines);
- FastRP, for player embeddings and the "similar players" route.

The scores are written back to the nodes, so reads stay cheap. A full refresh takes a few seconds (`docs/perf.md`).

**Link prediction** (`ml/`) asks which club pairs that never traded will trade within two seasons:
- the protocol is temporal, so no future information leaks into training;
- the features are common neighbours, Adamic-Adar, preferential attachment, same country and PageRank gap;
- the chosen logistic regression reaches a test AUC-PR of 0.160 against 0.126 for the common-neighbours baseline,
  with 31 hits in the top 100 against 3.6 expected at random.

The predictions are written as `PREDICTED_LINK` / `PREDICTED_MOVE`, always labelled `predicted`.

## Agent, guardrails and rumours

Owner: D. Details in [`docs/guardrails.md`](docs/guardrails.md), [`docs/agent_eval.md`](docs/agent_eval.md) and
[`docs/decisions/d.md`](docs/decisions/d.md).

```
question → router (rules, then small model) → planner (large model + tools, ≤ 6 calls) → writer → verifier → answer
```

- **Router.** It refuses level judgements, betting, scores, write requests and off-topic questions; obvious cases
  are caught by rules, with no LLM call. On an ambiguous name ("Gabriel"), it asks which player is meant instead
  of guessing.
- **Tools.**
  - The agent has ten parameterised tools, each calling a service method: `resolve_entity`, `search_players`,
    `player_profile`, `club_pipeline`, `teammate_path`, `value_trend`, `similar_players`, `predicted_moves`,
    `rumours` and `cypher_fallback`.
  - `cypher_fallback` is the last resort. Its query must pass `CypherValidator`: read-only, schema allow-lists,
    `LIMIT ≤ 100`, paths of 6 hops at most, a single statement, strings and comments ignored. It then runs in a
    read transaction.
- **Verifier.** It is deterministic. Each claim keeps its place only if:
  - its evidence ids exist in the graph and were returned by a tool;
  - every number it cites appears in a tool result;
  - its nature matches its source (a rumour or a prediction can never be `verified`).

  Otherwise the claim moves to `removed_claims` with a reason, and the answer is rebuilt without it.
- **Rumours.**
  - **Extraction.** The small LLM reads RSS or GDELT articles. The extracted quote must appear in the article, and
    names are resolved against the graph: a name that does not match exactly one node is counted, never guessed.
  - **Confirmation.** A rumour stays `pending` until `scripts/confirm_rumours.py` (A) links it to a real transfer.
  - **Confidence.** The displayed confidence is the extraction confidence × the smoothed reliability of the
    source.
- **Evaluation.** 40 questions on the mini graph, 10 of them traps, with NanoGPT models:

  | | Run 1 | Run 2 |
  | --- | --- | --- |
  | Correct answers | 26 / 40 | **39 / 40** |
  | Traps handled | 10 / 10 | 10 / 10 |
  | Claims removed | 34 | 5 |
  | Mean latency | 49 s | 40 s |
  | Cost of the run | $0.12 | $0.11 |

  `docs/agent_eval.md` explains what changed between the two runs. Re-run with `python -m scripts.agent_eval`.

## Quality

- **CI** (GitHub Actions) runs three workflows:
  - `lint`: pylint ≥ 9.5 on `app scripts ml` (currently 10.00);
  - `tests`: pytest against a Neo4j 5 + GDS service container loaded with the mini graph;
  - `publish`: the Docker image on GHCR.
- **Mini graph.** [`tests/fixtures/mini_graph.cypher`](tests/fixtures/mini_graph.cypher) holds fictitious data
  whose expected answers are listed in [`tests/fixtures/README.md`](tests/fixtures/README.md). Every role tests
  against it, and tests that write create and remove their own data.
- **No real LLM in tests.** `LLM_PROVIDER=fake` is forced by `tests/conftest.py`: the agent is tested with a
  scripted model that returns tool calls and structured outputs.
- **Defence sheets.** Each member's choices, rejected alternatives and likely questions are in
  [`docs/defense/`](docs/defense).

## Repository layout

```
app/
  main.py, config.py, security.py      FastAPI app, settings, API key (B)
  database/neo4j.py                    Neo4jClient: parameterised reads and writes with a timeout (A)
  models/  routers/  services/         Pydantic models, HTTP routes, Cypher queries (one file per area)
  agent/                               LangGraph agent, tools, Cypher validator, evidence checks (D)
  llm/client.py                        chat models: anthropic, openai_compatible, fake (D)
scripts/                               ingestion, data quality, rumour confirmation (A), benchmarks (B),
                                       article import, agent evaluation (D)
ml/                                    link-prediction dataset and training (C)
tests/                                 pytest, one file per role and area; fixtures/ = mini graph
docs/                                  architecture, perf, schema, ML report, guardrails, evaluation, decisions, defense
nginx/  Dockerfile  docker-compose.yml  Makefile  .github/workflows/
```

## Team and contributions

| Role | Member | Scope | Commits (`git shortlog -sn --no-merges main`) | Merged PRs |
| --- | --- | --- | ---: | --- |
| A · data, lead | Raphael Hazan ([@raphaelhazan](https://github.com/raphaelhazan)) | schema, ingestion, data quality, rumour confirmation, CI | 12 | #13, #14, #15, #16 |
| B · API, Docker | Nicolas Salapete ([@nsalapete](https://github.com/nsalapete)) | API, services, Docker, nginx, image publishing, benchmarks | 27 | #5, #6, #7, #8, #9, #17, #18, #33, #34 |
| C · GDS, ML | Edouard Souied ([@edouard-sd](https://github.com/edouard-sd)) | GDS algorithms, link prediction, notebook | 17 | #10, #11, #12, #23, #25, #27, #28, #29 |
| D · agent, rumours | Josh Soussan ([@joshsssn](https://github.com/joshsssn)) | LLM client, LangGraph agent, guardrails, rumours, evaluation, README | 22 | #1, #2, #3, #4, #19, #20, #21, #22, #24, #26, #30, #31, #35 |

Counts as of 21/10/2026, merge commits excluded. Josh's count includes the shared project skeleton, and Nicolas's
the initial commit.

License: [MIT](LICENSE).
