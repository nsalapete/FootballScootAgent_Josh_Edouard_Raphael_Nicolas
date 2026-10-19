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
