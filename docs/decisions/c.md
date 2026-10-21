# Decisions of role C (GDS, ML)

One line per choice not settled by `CLAUDE.md`, in the order it was made.

- **Windows setup**: `git config core.autocrlf false` then a fresh checkout of the tracked files (CRLF made pylint
  report -1.93/10 instead of 10/10); no `make` on this machine, Makefile commands run by hand.
- **C1 · projection names**: `clubs-<random 8 hex>` and `teammates-<random 8 hex>`, passed as `$graph` parameters,
  so two concurrent refreshes never collide; both are dropped in a `finally` block, even if an algorithm fails.
- **C1 · one transaction per step**: project, PageRank, Louvain, project, FastRP, drop are six separate writes, so each
  step stays under `NEO4J_QUERY_TIMEOUT_S` (10 s by default) instead of the whole refresh.
- **C1 · two copies of `SENDS_TO`** in the `clubs` projection: `NATURAL` for PageRank (direction = where players go),
  `UNDIRECTED` for Louvain (modularity is defined on undirected graphs). Both weighted by `count`.
- **C1 · PageRank direction**: on `SENDS_TO`, score flows from seller to buyer, so a hub is a club that *receives*
  players from other central clubs (on the mini graph SC Alvorada, the biggest seller, ranks 7th of 8). Kept as is:
  it is what the contract asks for; a "best seller" ranking would need the reversed projection.
- **C1 · reproducibility**: Louvain and FastRP run with `concurrency: 1`, FastRP with `randomSeed: 42`
  (both are only deterministic on one thread).
- **C1 · FastRP settings**: 64 dimensions, `iterationWeights [0, 1, 1]` (1- and 2-hop neighbourhood, no self term),
  weighted by `matches`. A player without teammates gets a zero vector.
- **C1 · `similar_players`**: brute-force cosine (`gds.similarity.cosine`) between the player and every embedded
  player, `LIMIT k`; no KNN relation written (it is not in the schema). Zero vectors give NaN and are filtered out,
  so an isolated player has no neighbour and an unknown player returns `[]`.
- **C1 · `communities`**: clubs listed by decreasing PageRank, at most 100 names per community and 100 communities
  (rule "LIMIT on every list"); `size` is always the true member count.
- **C1 · `hubs`**: ties broken by `club_id`; before any refresh (no `pagerank`) it returns `[]`.
- **C2 · scope**: both clubs of a transfer must play in `SCOPE_COMPETITIONS` (club's `domestic_competition_id` in
  DuckDB), the same scope as the ingestion; transfers to or from other leagues, self-loops and missing seasons dropped.
  About 240 clubs and 9,000 directed pairs.
- **C2 · season**: Transfermarkt label `22/23` mapped to its start year 2022, like `Transfer.season` in the schema.
- **C2 · sample = directed pair (seller, buyer)**, like `SENDS_TO` and `PREDICTED_LINK`. Reverse of an existing pair
  counts as a new pair.
- **C2 · positives** need both clubs in the training graph: a club with no past transfer has no neighbourhood, every
  topological feature would be 0 (cold start, out of scope).
- **C2 · negatives**: uniform random pairs of training-graph clubs, never traded up to the end of the target window,
  5 per positive, `random.Random(42)`; capped by the number of free pairs.
- **C2 · features**: common neighbours, Adamic-Adar and preferential attachment on the *undirected* training graph
  (a shared trading partner matters in both directions); same country from the domestic competition; PageRank gap =
  absolute difference of PageRank on the *directed* graph weighted by `count` (networkx).
- **C2 · two temporal splits**: test = graph up to 2022, new pairs of 2023-2024 (the protocol of the checklist);
  train = same protocol two seasons earlier (graph up to 2020, new pairs of 2021-2022), so the model is fit and
  evaluated on disjoint label windows and no feature ever sees its own label period.
- **C2 fix · one universe with the graph** (review of B on #11): scope clubs and cleaned transfers now come from
  A's ingestion code (`SCOPE_*_SQL`, `TRANSFERS_SQL`, `shape_transfer`, `clean_transfers`, `parse_season`),
  imported read-only: clubs that played a scope league game since `SCOPE_FROM_SEASON` (223), transfers with a
  scope club on one side, from 2015, without placeholders, duplicates or future dates. Replaces the club snapshot
  (`clubs.domestic_competition_id`) and the raw transfers since 2002.
- **C2 fix · features on the `SENDS_TO` graph, samples among scope clubs**: the feature graph keeps light clubs
  (1,796 clubs up to 2022), like `SENDS_TO` and the GDS PageRank; candidate pairs only join two scope clubs, the
  only ones for which `PREDICTED_LINK` is written. Country of a scope club = country of its latest scope league.
- **C3 · evaluation on every open pair** of the test window (43,442 pairs, 3.6 % positives), not on the 1:5 sample:
  AUC-PR and precision@k depend on the share of positives, the sample would inflate them.
- **C3 · temporal model selection**: fit on graph up to 2018 / labels 2019-2020, validate on the full window graph
  up to 2020 / labels 2021-2022. Replaces 5-fold random CV, which chose gradient boosting, worse on later seasons.
- **C3 · baseline** = raw common-neighbour count; ties in precision@k broken by input order (stable sort).
- **C3 · logistic regression on log1p features**, standardised (degree products are heavy-tailed); gradient boosting
  with scikit-learn defaults and seed 42.
- **C3 · final model** refit on the most recent labelled window (sampled test split), scoring the graph of every
  known season.
- **C3 · probability correction** for the 1:5 sampling: odds multiplied by (real rate / (1 - real rate)) /
  (1/6 / 5/6), real rate = 3.6 % measured on the full test window. Ranking unchanged, values honest.
- **C3 · `PREDICTED_LINK`** = 5 best destinations per scope club (about 1,100 links), so every club has some;
  written with `MATCH` on both clubs, batches of 5,000, own client with a 600 s timeout (like the ingestion).
- **C3 · `PREDICTED_MOVE`** = 3 best links of the player's current club, probability = probability of the pair
  (club-level score, documented in `docs/ml_report.md`).
- **C3 · versions** `lp-<model>-<yyyymmdd>`; a run deletes older `lp-` predictions (by batches of 10,000), never
  other versions such as the `fixture` ones of the test graph.
- **C3 · `predicted_moves`**: reads the stored relations, `ORDER BY probability DESC, club_id LIMIT $limit`.
- **C1 fix · drops** (review of B on #10): each projection drop has its own `try/except (Neo4jError, DriverError)`
  and logs a warning, so both drops are attempted and the caller sees the algorithm's error. `ServiceUnavailable`
  is a `DriverError`, not a `Neo4jError`: catching only the latter would miss a lost connection.
- **C1 fix · refresh timeout**: `POST /api/admin/gds/refresh` uses its own cached client with `GDS_TIMEOUT_S = 600`
  (same as nginx on `/api/admin/`), a constant like `INGEST_TIMEOUT_S` of A because `app/config.py` belongs to B.
  The shared client is swapped only when it is the app's `get_client()`, so a test override of `get_db` still wins.
- **C1 · timings on the full graph** (2,232 clubs, 68,139 projected `SENDS_TO`, 17,856 players, 719,414 projected
  `PLAYED_WITH` relationships, local Docker): project clubs 5.2 s, PageRank 0.9 s, Louvain 2.5 s (85 communities,
  modularity 0.48), project teammates 1.1 s, FastRP 2.6 s, drops 0.3 s: 12.6 s in total, every step under 10 s but
  with little margin. `similar_players` p50 307 ms, p95 1.1 s (20 players, k = 10).
- **C4 · FastRP feature not kept**: recomputed in numpy on each feature graph (the stored `embedding` sees every
  season: leakage), pair feature = cosine of the club embeddings. Validation AUC-PR 0.150 to 0.152 over 9 runs
  (3 sizes x 3 seeds) vs 0.152 without; test 0.159 vs 0.160. It duplicates the common-neighbour signal. The
  experiment stays reproducible in `ml/fastrp_experiment.py`; the model keeps five features.
- **C3 fix · sliding feature window of 4 seasons** (review of B on #23): features on the 4 seasons ending at each
  cut-off, so every split sees the same number of seasons (the fit window 2015-2018 has only 4) and count features
  keep the scale the model learnt. "New pair" is still judged on the full history since 2015 (otherwise old pairs
  would count as new and the task would change). Validation AUC-PR 0.1526 (vs 0.1517 with growing windows; 3 and 2
  seasons give 0.153 and 0.152): chosen for the principle and a validation tie, not on the test.
- **C3 fix · pinned date** `AS_OF = 2026-10-20` instead of `date.today()` for the "future transfer" rule, and in
  `model_version`: a run only changes when the DuckDB file does.
- **C3 fix · horizon on predictions** (review of B on #27): `PREDICTED_LINK` / `PREDICTED_MOVE` also carry
  `horizon_seasons = 2` and `trained_until = 2024` (last labelled season the final model saw); `PredictedMove` gets the
  optional fields `horizon_seasons`, `trained_until` and `basis = "club_pair"` (same answer for every player of a club).
- **C4 fix · embeddings fed as vectors** (review of B on #28): Hadamard product of the two undirected embeddings (64
  features) and a directed variant (seller's embedding on its sales x buyer's embedding on its purchases): validation
  0.134 to 0.146 over 3 seeds, below 0.153 without; cosine 0.153 (no gain). FastRP stays out of the model.
- **C1 · warm refresh**: a second refresh on the full graph takes 1.8 s (project clubs 0.23 s, PageRank 0.24 s,
  Louvain 0.58 s, project teammates 0.27 s, FastRP 0.43 s); the 12.6 s first run pays the GDS warm-up of a fresh JVM.
  `similar_players` warm: p50 92 ms, p95 195 ms.
