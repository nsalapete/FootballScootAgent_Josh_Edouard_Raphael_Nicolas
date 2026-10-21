# Defense sheet · Role C (GDS, ML)

Goal: defend every line of role C without having written it. One-line choices are in `docs/decisions/c.md`,
metrics and protocol in `docs/ml_report.md`, the live demo in `demo.ipynb`. This sheet gives the why, the rejected
alternatives, the cost, the likely questions, and the outline of the talk (section 6).

## 1. What is delivered

| Task | Content | Files | PR |
| --- | --- | --- | --- |
| C1 | Two GDS projections, weighted PageRank, Louvain, FastRP written back; `hubs`, `communities`, `similar_players` | `app/services/gds.py`, `tests/test_c_gds.py` | #10, fix #25 |
| C2 | Temporal link-prediction dataset read straight from DuckDB, same universe as the graph | `ml/dataset.py`, `tests/test_c_dataset.py` | #11, fix #23 |
| C3 | Logistic regression and gradient boosting vs common neighbours; predictions written to Neo4j; `predicted_moves` | `ml/train_link_prediction.py`, `tests/test_c_link_prediction.py` | #27, fix #38 |
| C4 | FastRP club embeddings as features (cosine, Hadamard, directed): measured, no gain, not kept | `ml/fastrp_experiment.py`, `tests/test_c_fastrp.py` | #28, #38 |
| C5 | Notebook executed end to end with charts | `demo.ipynb` | #40, #42 |
| C6 | Final ML report | `docs/ml_report.md` | #41 |
| Bonus | Betweenness of gateway clubs, `GET /api/clubs/gateways`, GDS routes tested end to end | `app/services/gds.py`, `app/routers/network.py` | #42 |

Every review comment of B on these PRs was answered, by a fix or a measured experiment.

## 2. GDS concepts

### In-memory projection

GDS does not run on the stored graph. `gds.graph.project` copies the chosen labels, relationship types and
properties into a compact in-memory structure (adjacency lists, arrays of doubles), optimised for parallel algorithms.
The stored graph is not touched until an algorithm runs in `write` mode and writes a property back.

| Projection | Nodes | Relationships | Weight | Used by |
| --- | --- | --- | --- | --- |
| `clubs-<id>` | `Club` (2,232 on the full graph) | `SENDS_TO` twice: `NATURAL` and `UNDIRECTED` (68,139 projected) | `count` | PageRank and betweenness (directed copy), Louvain (undirected copy) |
| `teammates-<id>` | `Player` (17,856) | `PLAYED_WITH`, `UNDIRECTED` (719,414 projected, 2 per stored relationship) | `matches` | FastRP |

Why drop them after use: a projection lives in the heap of the Neo4j server until it is dropped; keeping it would
cost memory and be stale after the next ingestion. Each drop has its own `try/except`, so both are attempted and the
caller still sees the original error. A random suffix in the name lets two refreshes run at the same time.

### Cost of `refresh` (full graph, local Docker)

| Step | First run after a Neo4j start | Next runs |
| --- | --- | --- |
| project clubs | 5.2 s | 0.23 s |
| PageRank | 0.9 s | 0.24 s |
| Louvain | 2.5 s | 0.58 s |
| betweenness | - | 0.66 s |
| project teammates | 1.1 s | 0.27 s |
| FastRP | 2.6 s | 0.43 s |
| **total** | **12.6 s** (without betweenness) | **1.4 to 1.8 s** |

What to say: the first refresh after a restart pays a one-off GDS warm-up (JVM, code loading); the next ones are
about 8 times faster. Each step is its own transaction, and the route uses its own client with a 600 s limit (the
nginx limit on `/api/admin/`), so even a cold run on a slower machine fits.

### Why PageRank rather than degree

Degree counts trading partners; PageRank also counts **who** they are. A club gets a high score if players come to it
from clubs that are themselves destinations of many players. It is computed iteratively (damping 0.85: with
probability 0.15 the random walker jumps to a random club). Weighted by `count`, a pair with 5 transfers passes more
score than a pair with 1.

Direction to defend: on `SENDS_TO` (seller to buyer), the score flows towards buyers. A "hub" is a destination club,
not a seller. On the mini graph SC Alvorada, the biggest seller, ranks 7th of 8. On the full graph the top 10 are
Genoa, Hellas Verona, Inter, Fiorentina, Atalanta, Roma, Nottingham Forest, Benfica, Udinese, Sporting.

### Betweenness: the gateway clubs

Betweenness counts the shortest seller-to-buyer paths that pass through a club. PageRank says where players end
up; betweenness says where they pass through. Top gateways on the full graph: Genoa, Udinese, Benfica, Boavista,
Famalicão, Hellas Verona, Santa Clara, Sporting, Estoril, Gil Vicente. These are Portuguese and Italian clubs that
bridge markets, for example South America to the big five leagues. Unweighted, because GDS reads a weight as a
distance, not a strength. Computed in `refresh` (0.66 s) and stored, so the public route never starts a projection.

### What Louvain maximises, and its limits

Louvain maximises **modularity**: the share of the weight inside communities minus what a random graph with the same
degrees would put there. It alternates two phases: move each node to the neighbour community that increases
modularity most, then merge each community into one node, and repeat until no gain.

Limits: resolution limit (small communities can be swallowed by large ones), results depend on the order of nodes
(we run it on one thread for reproducibility), communities can be internally disconnected (Leiden fixes this), and
modularity is defined on undirected graphs, hence the undirected copy of `SENDS_TO`.

Full graph: modularity 0.48, 85 communities, 7 with at least 2 clubs, close to the national markets (Portugal,
Italy, France, the Netherlands, England, Germany, Spain) with their foreign partners.

### How FastRP builds an embedding

Fast Random Projection: every player starts with a sparse random vector (64 dimensions). Each iteration averages
the vectors of the neighbours (weighted by `matches`) and normalises. The final embedding is a weighted sum of the
iterations: `iterationWeights [0, 1, 1]` keeps the 1-hop and 2-hop averages and drops the random start vector.
By the Johnson-Lindenstrauss lemma, random projection approximately preserves distances, so two players with similar
teammate neighbourhoods end up with close vectors. It is linear in the number of relationships: far cheaper than
node2vec (random walks + training). A player without teammates gets a zero vector and no similar player.

### `similar_players` and its cost

Cosine similarity between the player's embedding and every other embedded player, sorted, `LIMIT k`. Full graph,
warm: **p50 92 ms, p95 195 ms** (307 ms / 1.1 s on a cold server). It means "shared teammate circles", not the same
position or style.

Alternative measured by B (`docs/perf.md`, benchmark graph of 50,000 players): a vector index
(`db.index.vector.queryNodes`) answers in 8.5 ms p50 instead of 176 ms, with recall@10 = 0.95 against the exact
neighbours. Not adopted: it is approximate, it needs an index in `constraints.cypher` (A's contract), and the current
latency is acceptable. It is the first thing to switch on if the graph grows.

## 3. The link-prediction dataset

### The question and the universe

For a directed pair of clubs (seller, buyer) that never traded, will they trade within the next two seasons? This is
what `PREDICTED_LINK` stores.

The universe is the graph's: the code of A is imported (read-only) to select the 223 scope clubs (a league game of a
scope competition since 2015) and clean the transfers (loans, duplicates, placeholders, future dates). Samples only
join two scope clubs, the ones for which a prediction can be written. Data cut pinned at `AS_OF = 2026-10-20`, so a
run is reproducible as long as the DuckDB file does not change.

### Why a temporal split, and what a leak is

A random split would put transfers from 2023 in the feature graph and ask the model to "predict" 2022 links from
them: the model would see the future. A **leak** is any information in the features that would not be available at
prediction time. Every feature is computed on seasons up to the cut-off; later seasons only give labels. A test
rebuilds the features after deleting every future edge and gets the same table. (Same reason why the stored FastRP
`embedding` cannot be a feature: it is computed on every season.)

| Window | Features (4 seasons) | New pairs during | Used for |
| --- | --- | --- | --- |
| fit | 2015-2018 | 2019-2020 | model selection: fit |
| train | 2017-2020 | 2021-2022 | model selection: validation, then fit for the evaluation |
| test | 2019-2022 | 2023-2024 | evaluation, once (43,442 open pairs, 1,576 new links) |

Why 4 seasons for every window: count features grow with the window length; with growing windows the test features
were larger than the ones the model learnt (B's review). "New" is still judged on the full history since 2015,
otherwise an old pair would count as new and the task itself would change.

### Negatives

About 222 x 221 = 49,000 possible directed pairs, around 1,550 new links per window: 3.5 % positives. Training
samples 5 random negatives per positive (seed 42). Evaluation uses **every** open pair, because a 1-in-6 sample
would inflate AUC-PR and precision@k.

### Features

| Feature | Formula | Intuition |
| --- | --- | --- |
| Common neighbours | number of clubs that traded with both | shared contacts, same market |
| Adamic-Adar | sum over common neighbours w of 1 / log(degree(w)) | a common partner that trades with everyone says less |
| Preferential attachment | degree(seller) x degree(buyer) | busy clubs make new links |
| Same country | both latest scope leagues in the same country | domestic market, no work permit, same language |
| PageRank gap | abs(PageRank(seller) - PageRank(buyer)), directed, weighted | clubs of very different standing |

## 4. The model

### Why AUC-PR when positive links are rare

With 3.6 % positives, a model that says "no" everywhere has 96 % accuracy, and ROC AUC stays flattering because it
rewards ranking the huge mass of easy negatives below the positives. **AUC-PR** (average precision) only looks at the
pairs the model puts on top: of the pairs it would show a scout, how many really happen? A random ranking scores the
prevalence (0.036). **Precision@k** answers the scout's question directly: among the k best predictions, how many
happened?

### Results (full test set)

| Model | Validation AUC-PR | Test AUC-PR | ROC AUC | P@50 | P@100 |
| --- | --- | --- | --- | --- | --- |
| Random | - | 0.036 | 0.5 | 0.036 | 0.036 |
| Common neighbours (baseline) | 0.109 | 0.126 | 0.794 | 0.16 | 0.26 |
| **Logistic regression** (chosen) | **0.153** | **0.166** | **0.823** | **0.34** | 0.29 |
| Gradient boosting | 0.132 | 0.146 | 0.810 | 0.26 | 0.31 |

What to say: the task is hard and the numbers are low in absolute terms (most top pairs do not trade within two
seasons). But among its 50 most probable new pairs, the model finds 17 real ones: twice the baseline (8) and 9 times
chance (1.8). AUC-PR is 32 % above the baseline. At precision@100 the gap narrows, and gradient boosting is slightly
better there: we report it rather than change models after looking at the test.

Coefficients (standardised log features): common neighbours +0.96, same country +0.55, preferential attachment +0.26,
Adamic-Adar -0.08 (redundant with common neighbours), PageRank gap -0.06.

### Why the model is chosen on a temporal validation, not k-fold

A first version picked the model by 5-fold random cross-validation on one window: gradient boosting won, and was the
worse model on the test seasons. Random folds mix pairs of the same period, so they measure interpolation; the real
use is predicting the next seasons. Fitting on 2015-2018 and validating on the full 2021-2022 window picks logistic
regression clearly (0.153 vs 0.132), consistent with the test.

### FastRP as a feature: measured three ways, not kept

| Variant | Validation AUC-PR |
| --- | --- |
| none | 0.1526 |
| cosine of the two embeddings | 0.153 (no gain) |
| Hadamard product, 64 features | 0.137 to 0.146 |
| directed (seller's sales x buyer's purchases), 64 features | 0.134 to 0.143 |

The cosine encodes the same overlap of neighbourhoods as common neighbours; the 64-feature variants add noise with
about 1,400 positives to learn from. Reproducible with `python -m ml.fastrp_experiment`.

### From scores to the graph

- Refit on the most recent labelled window, scores every open pair on the 4 latest seasons.
- **Probability correction**: trained at 1 positive in 6, real rate 3.6 %, so the odds are multiplied by about
  1/5.3. Same ranking, honest values.
- `PREDICTED_LINK`: the 5 best destinations of each scope club (1,115 links, probability 0.002 to 0.61).
  `MATCH` on both clubs, never `MERGE`: an unknown club is skipped, never created.
- `PREDICTED_MOVE`: for each player, the 3 best links of his current club (41,961 moves, 13,987 players).
  **Probability of a move = probability of the pair (current club, destination)**: a club-level score.
- Each relationship carries `prob`, `model_version` (`lp-logistic_regression-20261020`), `horizon_seasons = 2` and
  `trained_until = 2024`; the API adds `nature: "predicted"` and `basis: "club_pair"`.
- Example: Bradley Barcola (PSG) -> Olympique Marseille 0.51, RC Strasbourg 0.32, Paris FC 0.23. Say "players of
  PSG often go to Marseille within two seasons", never "Barcola will join Marseille".

## 5. Likely questions

- **What is the weak point of your model?** Its features are symmetric: A -> B and B -> A get the same score
  (Hellas Verona <-> Como, 0.61 both). The directed FastRP variant did not fix it; directed counting features
  (out-degree of the seller, in-degree of the buyer, signed PageRank gap) are the next step. Also: pair-level, not
  player-level, and no fees, league level or rivalries.
- **Why not a GNN / node2vec / GDS link-prediction pipeline?** With five features the baseline is already hard to beat
  (gradient boosting barely does); the embedding we have brings nothing measurable in three forms. More capacity
  needs more signal. Simple, explainable, reproducible first.
- **Are the predictions evaluated?** The model is, on 2023-2024. The live predictions are not yet: their label window
  is the future.
- **What does 0.51 mean?** The probability that a player of the current club's pipeline goes to that club within
  2 seasons, corrected to the real rate of new links (3.6 %). It is stored with its horizon and training season.
- **What happens if a step of `refresh` times out?** It runs on a 600 s client; if it still fails, both projections
  are dropped and the error reaches the caller (500).
- **Are the results reproducible?** Yes: `AS_OF` pinned, Louvain and FastRP on one thread with a seed, negatives
  `random.Random(42)`, gradient boosting `random_state=42`, DuckDB read-only.
- **Do the tests leave the test database intact?** GDS tests remove the properties they write; write-back tests
  create their own clubs and players (negative ids) and delete them; nothing touches the mini graph's `fixture`
  predictions.
- **How many clubs does PageRank run on?** 2,232 `Club` nodes: the 223 scope clubs plus light clubs (only id and
  name) that appear in a transfer. On the current data the top 10 hubs and the top 10 gateways are all scope clubs.
- **Why must a club appear in the feature window?** A club with no transfer in the last 4 seasons has all features at
  0: no topological method can rank it (cold start, e.g. some promoted clubs).

## 6. Outline of the talk (about 6 minutes for role C)

1. **The question** (30 s): from facts to computed values to predictions, never mixed. Three levels of truth.
2. **GDS on the graph** (1 min 30): projections in memory, PageRank hubs vs betweenness gateways, Louvain
   communities. Show the notebook's network chart (three communities) and the two top-10 lists.
3. **Similar players** (30 s): FastRP on teammates, what "similar" means, cost (92 ms) and the vector-index option.
4. **Link prediction protocol** (1 min 30): the temporal windows table, the leak, why AUC-PR, the full open-pair
   test set, temporal model selection (the k-fold story is a good anecdote).
5. **Results, honestly** (1 min): the metrics table and the precision-recall chart. 17 of the top 50 vs 8 for the
   baseline; FastRP tried three ways, not kept.
6. **Into the graph and the API** (30 s): `PREDICTED_LINK` / `PREDICTED_MOVE` with horizon, basis and correction; the
   Barcola example and how to phrase it.
7. **Limits and next steps** (30 s): symmetric features, pair-level moves, cold start, directed features next.

Live demo option: `GET /api/clubs/hubs`, `GET /api/clubs/gateways`, `GET /api/players/708265/predicted-moves` in
Swagger (`http://localhost/docs`), or run the notebook top to bottom (about 45 s).
