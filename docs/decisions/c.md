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
