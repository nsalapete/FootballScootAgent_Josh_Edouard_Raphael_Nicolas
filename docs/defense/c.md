# Defense sheet · Role C (GDS, ML)

Goal: defend every line of role C without having written it. One-line choices are in `docs/decisions/c.md`,
metrics and protocol in `docs/ml_report.md`; this sheet gives the why, the rejected alternatives, the cost and the
likely questions.

## 1. What is delivered

| Task | Content | Files | PR |
| --- | --- | --- | --- |
| C1 | Two GDS projections, weighted PageRank, Louvain, FastRP written back; `hubs`, `communities`, `similar_players` | `app/services/gds.py`, `tests/test_c_gds.py` | #10 |
| C1 fix | Both projection drops always attempted; `refresh` on a 600 s client | `app/services/gds.py`, `app/routers/network.py` | #25 |
| C2 | Temporal link-prediction dataset read straight from DuckDB | `ml/dataset.py`, `tests/test_c_dataset.py` | #11 |
| C2 fix | Same universe as the graph (A's scope and cleaning code) | `ml/dataset.py` | #23 |
| C3 | Logistic regression and gradient boosting vs common neighbours; predictions written to Neo4j; `predicted_moves` | `ml/train_link_prediction.py`, `tests/test_c_link_prediction.py`, `docs/ml_report.md` | #27 |
| C4 | FastRP club embedding as a feature: measured, no gain, not kept | `ml/fastrp_experiment.py`, `tests/test_c_fastrp.py` | #28 |

Still to do: C5 (notebook), C6 (final ML report).

## 2. GDS concepts

### In-memory projection

GDS does not run on the stored graph. `gds.graph.project` copies the chosen labels, relationship types and
properties into a compact in-memory structure (adjacency lists, arrays of doubles), optimised for parallel algorithms.
The stored graph is not touched until an algorithm runs in `write` mode and writes a property back.

| Projection | Nodes | Relationships | Weight | Used by |
| --- | --- | --- | --- | --- |
| `clubs-<id>` | `Club` (2,232 on the full graph) | `SENDS_TO` twice: `NATURAL` and `UNDIRECTED` (68,139 projected) | `count` | PageRank (directed copy), Louvain (undirected copy) |
| `teammates-<id>` | `Player` (17,856) | `PLAYED_WITH`, `UNDIRECTED` (719,414 projected, 2 per stored relationship) | `matches` | FastRP |

Why drop them after use: a projection lives in the heap of the Neo4j server until it is dropped; keeping it would
cost memory and be stale after the next ingestion. Each drop has its own `try/except`, so both are attempted and the
caller still sees the original error. A random suffix in the name lets two refreshes run at the same time.

### Cost of `refresh` (measured on the full graph, local Docker)

| Step | Time |
| --- | --- |
| project clubs | 5.2 s |
| PageRank | 0.9 s |
| Louvain | 2.5 s (85 communities, modularity 0.48) |
| project teammates | 1.1 s |
| FastRP | 2.6 s |
| drops | 0.3 s |
| **total** | **12.6 s** |

Each step is its own transaction. The API answers reads with a 10 s limit; `refresh` uses its own client with
`GDS_TIMEOUT_S = 600` (the nginx limit on `/api/admin/`), because 5.2 s leaves little margin on a slower machine.

### Why PageRank rather than degree

Degree counts trading partners; PageRank also counts **who** they are. A club gets a high score if players come to it
from clubs that are themselves destinations of many players. It is computed iteratively (damping 0.85: with
probability 0.15 the random walker jumps to a random club). Weighted by `count`, a pair with 5 transfers passes more
score than a pair with 1.

Direction to defend: on `SENDS_TO` (seller to buyer), the score flows towards buyers. A "hub" is a destination club,
not a seller. On the mini graph SC Alvorada, the biggest seller, ranks 7th of 8. On the full graph the top 10 are
Genoa, Hellas Verona, Inter, Fiorentina, Atalanta, Roma, Nottingham Forest, Benfica, Udinese, Sporting: clubs that
buy a lot from clubs that buy a lot, all scope clubs (light clubs abroad are in the projection but not in the top).

### What Louvain maximises, and its limits

Louvain maximises **modularity**: the share of the weight inside communities minus what a random graph with the same
degrees would put there. It alternates two phases: move each node to the neighbour community that increases
modularity most, then merge each community into one node, and repeat until no gain.

Limits: resolution limit (small communities can be swallowed by large ones), results depend on the order of nodes
(we run it on one thread for reproducibility), communities can be internally disconnected (Leiden fixes this), and
modularity is defined on undirected graphs, hence the undirected copy of `SENDS_TO`.

Full graph: modularity 0.48, and the largest communities are close to the national markets (Portugal around
Benfica / Sporting / Porto, Italy, France, the Netherlands, England, Germany), with their foreign partners.

### How FastRP builds an embedding

Fast Random Projection: every player starts with a sparse random vector (64 dimensions). Each iteration averages
the vectors of the neighbours (weighted by `matches`) and normalises. The final embedding is a weighted sum of the
iterations: `iterationWeights [0, 1, 1]` keeps the 1-hop and 2-hop averages and drops the random start vector.
By the Johnson-Lindenstrauss lemma, random projection approximately preserves distances, so two players with similar
teammate neighbourhoods end up with close vectors. It is linear in the number of relationships: far cheaper than
node2vec (random walks + training). A player without teammates gets a zero vector and no similar player.

### `similar_players` and its cost

Cosine similarity between the player's embedding and every other embedded player, sorted, `LIMIT k`. Measured on
the full graph: **p50 307 ms, p95 1.1 s** (20 players, k = 10). Rejected for now: a vector index (needs a line in
`constraints.cypher`, owned by A; worth it if the latency becomes a problem) and GDS KNN writing `SIMILAR`
relationships (not in the schema contract).

## 3. The link-prediction dataset (C2)

### The question and the universe

For a directed pair of clubs (seller, buyer) that never traded up to a season, will they trade in the next two
seasons? This is what `PREDICTED_LINK` stores.

The universe is the graph's: the code of A is imported (read-only) to select the 223 scope clubs (a league game of a
scope competition since 2015) and clean the transfers (loans, duplicates, placeholders, future dates). Features are
computed on the graph of all cleaned transfers (light clubs included, like `SENDS_TO`); samples only join two scope
clubs, the ones for which a prediction can be written. A first version used DuckDB's club snapshot
(`domestic_competition_id`, the *current* league): B's review showed it lost relegated clubs and kept lower-league
history, so ML and graph disagreed.

### Why a temporal split, and what a leak is

A random split would put transfers from 2023 in the feature graph and ask the model to "predict" 2022 links from
them: the model would see the future. A **leak** is any information in the features that would not be available at
prediction time. Every feature is computed on the graph up to the cut-off; later seasons only give labels. A test
rebuilds the features after deleting every future edge and gets the same table.

| Window | Feature graph | New pairs during | Used for |
| --- | --- | --- | --- |
| fit | 2015-2018 | 2019-2020 | model selection: fit |
| train | 2015-2020 | 2021-2022 | model selection: validation, then fit for the evaluation |
| test | 2015-2022 | 2023-2024 | evaluation, once (43,442 open pairs, 1,576 new links) |

### Negatives

About 222 x 221 = 49,000 possible directed pairs, ~1,550 new links per window: 3.5 % positives. Training samples 5
random negatives per positive (seed 42) among pairs that never trade up to the end of the window; evaluation uses
**every** open pair, because a 1-in-6 sample would inflate AUC-PR and precision@k.

### Features

| Feature | Formula | Intuition |
| --- | --- | --- |
| Common neighbours | number of clubs that traded with both | shared contacts, same market |
| Adamic-Adar | sum over common neighbours w of 1 / log(degree(w)) | a common partner that trades with everyone says less |
| Preferential attachment | degree(seller) x degree(buyer) | busy clubs make new links |
| Same country | both latest scope leagues in the same country | domestic market, no work permit, same language |
| PageRank gap | abs(PageRank(seller) - PageRank(buyer)), directed, weighted | clubs of very different standing |

## 4. The model (C3, C4)

### Why AUC-PR when positive links are rare

With 3.6 % positives, a model that says "no" everywhere has 96 % accuracy, and ROC AUC stays flattering because it
rewards ranking the huge mass of easy negatives below the positives. **AUC-PR** (average precision) only looks at the
pairs the model puts on top: of the pairs it would show a scout, how many really happen? A random ranking scores the
prevalence (0.036), so 0.16 is 4.4 times random. **Precision@k** answers the scout's question directly: among the k
best predictions, how many happened?

### Results (full test set)

| Model | Validation AUC-PR | Test AUC-PR | ROC AUC | P@50 | P@100 |
| --- | --- | --- | --- | --- | --- |
| Random | - | 0.036 | 0.5 | 0.036 | 0.036 |
| Common neighbours (baseline) | 0.115 | 0.126 | 0.795 | 0.26 | 0.23 |
| **Logistic regression** (chosen) | **0.152** | **0.160** | **0.819** | 0.26 | **0.31** |
| Gradient boosting | 0.117 | 0.126 | 0.794 | 0.28 | 0.23 |

What to say: the task is hard and the numbers are low in absolute terms (most top pairs do not trade within two
seasons), but the model beats the baseline by 27 % in AUC-PR and finds 31 real new links in its top 100, against
23 for the baseline and 3.6 at random. At precision@50 it only ties the baseline.

Coefficients (standardised log features): common neighbours +0.65, same country +0.48, Adamic-Adar +0.33,
preferential attachment +0.13, PageRank gap -0.09.

### Why the model is chosen on a temporal validation, not k-fold

A first version picked the model by 5-fold random cross-validation on the train window: gradient boosting won
(0.502 vs 0.494), and was the worse model on the test seasons. Random folds mix pairs of the same period, so they
measure interpolation; the real use is predicting the next seasons. Fitting on 2015-2018 and validating on the full
2021-2022 window picks logistic regression clearly (0.152 vs 0.117), consistent with the test. Gradient boosting
overfits one window with five correlated features.

### FastRP as a feature (C4): measured, not kept

The stored `embedding` cannot be used (computed on every season: leakage), so FastRP is recomputed in numpy on each
feature graph. The cosine of the two club embeddings separates the classes (0.39 vs 0.22), but adds nothing on top of
common neighbours: validation 0.150 to 0.152 over 9 runs (3 sizes x 3 seeds) vs 0.152 without; test 0.159 vs 0.160.
It encodes the same overlap of 1- and 2-hop neighbourhoods. Kept out; the experiment is reproducible.

### From scores to the graph

- The chosen model is refit on the most recent labelled window and scores every open pair of the current graph.
- **Probability correction**: trained at 1 positive in 6, real rate 3.6 %, so the odds are multiplied by
  (0.036 / 0.964) / (1/6 / 5/6), about 1/5.3. Same ranking, honest values.
- `PREDICTED_LINK`: the 5 best destinations of each scope club (1,115 links, probability 0.06 to 0.78).
  `MATCH` on both clubs, never `MERGE`: an unknown club is skipped, never created.
- `PREDICTED_MOVE`: for each player, the 3 best links of his current club (41,961 moves, 13,987 players).
  **Probability of a move = probability of the pair (current club, destination)**: a club-level score, the model
  knows nothing about the player (age, contract, form).
- `model_version = lp-logistic_regression-20261020`; a new run deletes older `lp-` versions only.
- Labelled "predicted" everywhere: separate relationship types, `nature: "predicted"` in `PredictedMove`, the agent's
  `predicted_moves` tool tags its result `predicted` and the verifier forces any claim citing it to `predicted`.

## 5. Likely questions

- **What is the weak point of your model?** Its features are symmetric: A -> B and B -> A get the same score
  (Benfica -> Porto = Porto -> Benfica = 0.78). And it does not know rivalries: Benfica, Sporting and Porto share
  many partners but rarely sell to each other. Directed features (out-degree of the seller, in-degree of the buyer,
  signed PageRank gap) are the next step.
- **Why not a GNN / node2vec / GDS link-prediction pipeline?** With five features the baseline is already hard to
  beat (gradient boosting does not); a heavier model would need more signal, not more capacity. FastRP, the
  embedding we have, brings nothing measurable. Simple, explainable, reproducible first.
- **Are the predictions evaluated?** The model is, on 2023-2024. The live predictions (graph up to 2026) are not
  yet: their label window is the future.
- **What happens if a step of `refresh` times out?** It runs on a 600 s client; if it still fails, both projections
  are dropped and the error reaches the caller (500).
- **Are the results reproducible?** Yes: Louvain and FastRP on one thread, FastRP seed 42, negatives
  `random.Random(42)`, gradient boosting `random_state=42`; DuckDB is read-only.
- **Do the tests leave the test database intact?** GDS tests remove the properties they write; write-back tests
  create their own clubs and players (negative ids) and delete them; nothing touches the mini graph's
  `fixture` predictions.
- **How many clubs does PageRank run on?** 2,232 `Club` nodes: the 223 scope clubs plus light clubs (only id and
  name) that appear in a transfer. Light clubs take part in the ranking (a foreign buyer can be central), but on the
  current data the top 10 are all scope clubs.
- **Why must both clubs exist in the training graph?** A club with no past transfer has all features at 0: no
  topological method can rank it (cold start, e.g. some promoted clubs).
