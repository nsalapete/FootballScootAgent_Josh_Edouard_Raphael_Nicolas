# Defense sheet · Role C (GDS, ML)

Goal: defend every line of role C without having written it. One-line choices are in `docs/decisions/c.md`;
this sheet gives the why, the rejected alternatives, the cost and the likely questions.

## 1. What is delivered (session 1: Monday 19 and Tuesday 20)

| Task | Content | Files |
| --- | --- | --- |
| C1 | Two GDS projections, weighted PageRank, Louvain, FastRP written back to the graph; `hubs`, `communities`, `similar_players` | `app/services/gds.py`, `tests/test_c_gds.py` |
| C2 | Temporal link-prediction dataset read straight from DuckDB: edges per season, leak-free splits, 5 negatives per positive, 5 topological features | `ml/dataset.py`, `tests/test_c_dataset.py` |

Still to do: C3 (models, metrics, `PREDICTED_LINK` / `PREDICTED_MOVE`, `predicted_moves`), C4 (FastRP feature),
C5 (notebook), C6 (final ML report).

## 2. GDS concepts

### In-memory projection

GDS does not run on the stored graph. `gds.graph.project` copies the chosen labels, relationship types and
properties into a compact in-memory structure (adjacency lists, arrays of doubles), optimised for parallel algorithms.
The stored graph is not touched until an algorithm runs in `write` mode and writes a property back.

| Projection | Nodes | Relationships | Weight | Used by |
| --- | --- | --- | --- | --- |
| `clubs-<id>` | `Club` | `SENDS_TO` twice: `NATURAL` and `UNDIRECTED` | `count` (number of transfers) | PageRank (directed copy), Louvain (undirected copy) |
| `teammates-<id>` | `Player` | `PLAYED_WITH`, `UNDIRECTED` | `matches` played together | FastRP |

Why drop them after use: a projection lives in the heap of the Neo4j server until it is dropped. Keeping it would
cost memory for nothing and would be stale as soon as the graph changes. The drop is in a `finally` block, so a failing
algorithm cannot leak a projection. A random suffix in the name lets two refreshes run at the same time.

Why one transaction per step: the shared client cuts every transaction at `NEO4J_QUERY_TIMEOUT_S` (10 s). Six short
transactions (project, PageRank, Louvain, project, FastRP, drop x2) each stay under it on the real graph.

### Why PageRank rather than degree

Degree counts trading partners; PageRank also counts **who** they are. A club gets a high score if players come to it
from clubs that are themselves destinations of many players. It is computed iteratively (damping 0.85: with
probability 0.15 the random walker jumps to a random club). Weighted by `count`, a pair with 5 transfers passes more
score than a pair with 1.

Direction to defend: on `SENDS_TO` (seller to buyer), the score flows towards buyers. A "hub" is therefore a
destination club, not a seller. On the mini graph SC Alvorada, which sells the most (4 transfers out), ranks 7th of 8,
and AS Montclair ranks 1st. A "best seller" ranking would need the reversed projection (`REVERSE` orientation).

### What Louvain maximises, and its limits

Louvain maximises **modularity**: the share of the weight inside communities minus what a random graph with the same
degrees would put there. It alternates two phases: move each node to the neighbour community that increases
modularity most, then merge each community into one node, and repeat until no gain.

Limits: resolution limit (small communities can be swallowed by large ones), results depend on the order of nodes
(we run it on one thread for reproducibility), communities can be internally disconnected (Leiden fixes this), and
modularity is defined on undirected graphs, hence the undirected copy of `SENDS_TO`.

Mini graph result: {AS Montclair, Stade Brumaire, Wexley Athletic}, {Harrowgate FC, SC Alvorada, CD Mares},
{Kingsbridge United, Olympique Rivage}.

### How FastRP builds an embedding

Fast Random Projection: every player starts with a sparse random vector (64 dimensions here). Each iteration averages
the vectors of the neighbours (weighted by `matches`), then normalises. The final embedding is a weighted sum of the
iterations: `iterationWeights [0, 1, 1]` keeps the 1-hop and 2-hop averages and drops the random start vector.
By the Johnson-Lindenstrauss lemma, random projection approximately preserves distances, so two players with similar
teammate neighbourhoods end up with close vectors. It is linear in the number of relationships: far cheaper than
node2vec (random walks + training).

A player without teammates (Samuel Okafor, id 16) has nothing to average: zero vector, undefined cosine. He is filtered
out and gets no similar player.

### `similar_players` and its cost

Cosine similarity between the player's embedding and every other embedded player, sorted, `LIMIT k`.
Cost: O(players x 64) per call, a few hundred ms on the real graph. Rejected alternatives: GDS KNN writing
`SIMILAR` relationships (not in the schema contract), a vector index (needs a change to `constraints.cypher`, owned by A).

## 3. The link-prediction dataset (C2)

### The question

For a directed pair of clubs (seller, buyer) that never traded up to a season, will they trade in the next seasons?
This is what `PREDICTED_LINK` stores.

### Why a temporal split, and what a leak is

A random split would put transfers from 2023 in the training graph and ask the model to "predict" 2022 links from
them: the model would see the future. A **leak** is any information in the features that would not be available at
prediction time. Here every feature is computed on the graph of seasons up to the cut-off only; later seasons only
give labels. A test checks it: rebuilding the features after deleting every future edge gives exactly the same table.

| Split | Feature graph | Labels (new pairs) | Size |
| --- | --- | --- | --- |
| train | seasons up to 2020 (239 clubs, 5,197 directed pairs) | first trade in 2021 or 2022 | 1,501 positives + 7,505 negatives |
| test | seasons up to 2022 (239 clubs, 6,698 directed pairs) | first trade in 2023 or 2024 | 1,562 positives + 7,810 negatives |

The model is fit on the train split and evaluated on the test split: the label windows never overlap.

### Negatives

There are about 239 x 238 = 57,000 possible directed pairs and only ~1,500 new links: positives are rare.
We sample 5 random pairs per positive (fixed seed 42), among clubs of the graph, that never traded up to the end of
the target window. Ratio 1:5 keeps the class imbalance visible (a model that says "no" everywhere gets 83% accuracy,
which is why accuracy is not the metric).

### Features

| Feature | Formula | Intuition |
| --- | --- | --- |
| Common neighbours | number of clubs that traded with both | shared contacts, same market |
| Adamic-Adar | sum over common neighbours w of 1 / log(degree(w)) | a common partner that trades with everyone says less |
| Preferential attachment | degree(seller) x degree(buyer) | busy clubs make new links |
| Same country | 1 if both domestic leagues are in the same country | domestic market, no work permit, same language |
| PageRank gap | abs(PageRank(seller) - PageRank(buyer)), directed graph weighted by `count` | clubs of very different standing |

First signal on real data (test split, mean of positives vs negatives): common neighbours 11.7 vs 4.9,
Adamic-Adar 3.07 vs 1.27, same country 41% vs 7%. PageRank gap is about the same for both classes (0.0024 vs 0.0026):
probably a weak feature, to be confirmed in C3.

## 4. Likely questions

- **Why are GDS results written to nodes instead of computed per request?** PageRank and Louvain touch the whole graph;
  reading a stored property is an index-free scan of ~240 clubs. `POST /api/admin/gds/refresh` (protected by API key)
  recomputes them after an ingestion.
- **Are the results reproducible?** Yes: Louvain and FastRP run on one thread, FastRP with `randomSeed: 42`,
  negatives with `random.Random(42)`.
- **Why are the tests skipped without GDS?** The fixture calls `gds.version()` first and skips if the procedure is
  missing; unit tests with a fake client still check that projections are always dropped and that graph names are
  passed as parameters.
- **Do the tests leave the mini graph intact?** Yes: after the module, `REMOVE c.pagerank, c.community_id` and
  `REMOVE p.embedding`. No node or relationship is created.
- **Why not include clubs from other leagues in the dataset?** They have no country in DuckDB's `clubs` table and are
  not in the Neo4j scope: predictions could not be written back.
- **Why a directed pair?** A transfer has a direction and `SENDS_TO` / `PREDICTED_LINK` are directed. The reverse of an
  existing pair is a new event for a scout (the buyer becomes a seller).
- **Why must both clubs exist in the training graph?** A club with no past transfer has all features at 0: no
  topological method can rank it (cold start).
