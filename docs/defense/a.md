# Defense sheet · Role A (data, lead)

Goal: defend every line of role A without having written it. One-line choices are in `docs/decisions/a.md`;
this sheet gives the why, the rejected alternatives, the cost and the likely questions.

## 1. What is delivered (session 1: Monday 19 and Tuesday 20)

| Task | Content | Files |
| --- | --- | --- |
| A1 | DuckDB to Neo4j ingestion of the facts: countries, competitions, clubs, players, transfers, valuations | `scripts/seed_data.py`, `scripts/country_codes.py`, `tests/test_a_ingest.py` |
| A2 | Derived relationships at the end of the ingestion: `PLAYED_WITH` (aggregated in DuckDB), `SENDS_TO` (aggregated in Cypher) | same files |

Still to do: A3 (loans, missing fees, pseudo clubs, data-quality report), A4 (`confirm_rumours`), A5 (`docs/modeling.md`).

## 2. The pipeline in one picture

```
DuckDB (read-only)                                 Neo4j
  games ──► scope_games ──► scope_plays_in ──┐
                         └► scope_clubs ─────┼─► rows ──UNWIND $rows MERGE──► facts (batches of 5,000)
  transfers, players, valuations ───────────┘
  appearances ──► scope_appearances ──self-join──► PLAYED_WITH pairs ──► PLAYED_WITH
                                         Transfer nodes ──Cypher aggregation──► SENDS_TO
```

1. `scripts/constraints.cypher` runs first (8 uniqueness constraints, 4 range indexes, 2 full-text indexes).
2. DuckDB computes the scope in temporary tables (games of the 7 leagues from 2015, the clubs that played them,
   the players who appeared for them, moved to or from them, or still play there).
3. Python shapes the rows (season label to year, fee to integer, stable transfer id, country code).
4. Neo4j receives constant Cypher with `$rows`: nodes first, then the relationships that need them.
5. Derived relationships last: `PLAYED_WITH` from DuckDB pairs, `SENDS_TO` from the `Transfer` nodes.

## 3. Why the transfer is a node

A transfer has its own properties (date, season, fee, loan flag) **and** other things point to it: a confirmed rumour
links to it with `CONFIRMED_BY`. A relationship cannot be the target of another relationship in Neo4j, so a
`(:Player)-[:TRANSFERRED {fee}]->(:Club)` edge could never be confirmed by a rumour. It would also lose the origin
club (a relationship has only two ends: player and destination). Cost: one more hop
(`(p)-[:MADE]->(t)-[:TO]->(c)` instead of `(p)-[:TRANSFERRED]->(c)`), which is cheap because `Transfer` nodes have
exactly three relationships.

## 4. Why appearances are aggregated

`appearances` has 1.9 M rows (771,697 in scope). Loading them as nodes would make every teammate question a
two-hop join through a `Game` node, recomputed at each request. What the agent needs is "did A and B play together,
how often, when": exactly `PLAYED_WITH {matches, first_season, last_season}`. The aggregation (self-join on game and
club, `a < b`) runs once in DuckDB, a columnar engine made for this, and Neo4j only stores the 360 k pairs.
Each pair is stored **once**, from the smaller to the larger id: queries traverse it without direction
(`-[:PLAYED_WITH]-`), which halves storage and avoids double counting.

Rejected: `game_lineups` as source (lists unused substitutes, who did not play with anyone); cup and European games
(not comparable across clubs, kept out so `matches` means league games).

## 5. What `MERGE` does, and what it costs without a uniqueness constraint

`MERGE (p:Player {player_id: $id})` = "find a node with this label and key, create it if none". It is what makes
the ingestion idempotent: run it twice, nothing is duplicated.

- **With** the uniqueness constraint `player_id`, Neo4j has a backing index: the lookup is an index seek,
  O(log n), and concurrent `MERGE`s cannot create two nodes with the same key (the constraint takes a lock on the key).
- **Without** it, every `MERGE` scans all nodes of the label: O(n) per row, so loading n players costs O(n²)
  (17,861 players: about 160 M property comparisons instead of 17,861 seeks), and two concurrent transactions can
  both find nothing and both create the node: duplicates.

That is why `constraints.cypher` runs before anything else. Relationship `MERGE` between two matched nodes checks
the relationships of the node with fewer of them: cheap here.

`SET` after `MERGE` (not `ON CREATE SET`) updates the properties of a node that already exists, so a second run
with a newer file refreshes market values. Exception: light clubs use `ON CREATE SET name`, so the short name
of an out-of-scope club never overwrites the name of a scope club.

## 6. Which index serves which query

| Index | Kind | Query it serves |
| --- | --- | --- |
| 8 uniqueness constraints (`player_id`, `club_id`, ...) | range (backing) | every `MERGE` of the ingestion; every lookup by id (`GET /api/players/{id}`, agent tools) |
| `player_position_dob` on `Player(position, date_of_birth)` | composite range | `GET /api/players/search?position=Defender&max_age=23`: equality on the first property, range on the second |
| `player_value` on `Player(market_value_eur)` | range | budget filter `max_value_eur` |
| `transfer_date` on `Transfer(date)` | range | `GET /api/clubs/{id}/pipeline?since=2022-01-01` |
| `rumour_status` on `Rumour(status)` | range | `confirm_rumours`: pending rumours only |
| `player_name_ft`, `club_name_ft` | full-text (Lucene) | name resolution with typos or missing accents (`/resolve`, agent) |

Why the composite index has `position` first: a composite index is only fully used with equality on the leading
properties; `position` is filtered by equality, `date_of_birth` by a range.

## 7. Batching

Rows go by batches of 5,000 in one `UNWIND $rows AS row MERGE ...` per transaction.
One transaction per row would pay a network round trip and a commit for each of the ~700 k rows;
one transaction for everything would hold every change in the heap until the commit. 5,000 is the usual compromise.
The ingestion opens its own client with a 600 s transaction limit: the API client stops a transaction after 10 s,
which a cold batch of relationship `MERGE`s can exceed.

The Cypher text is constant; values only travel in `$rows`. No f-string, no concatenation: no injection, and
Neo4j caches one plan per statement instead of compiling a new query per batch.

## 8. Choices on the data

- **Scope**: clubs that played a league game of the 7 competitions since 2015, taken from `games`. The `clubs` table
  only knows a club's current league, so a club relegated in 2018 would be missing.
- **Light clubs**: the other side of a scope transfer (a Saudi or MLS club) is created with id and name only,
  so the transfer keeps both ends. They have no `PLAYS_IN`.
- **Season** = start year of the label (`22/23` -> 2022). A January 2023 transfer belongs to season 2022, which is
  what `PLAYS_IN {season}` uses to find the destination club's league at that time.
- **Fees**: `NULL` = unknown (kept `null`), `0` = free transfer. `SENDS_TO.total_fee_eur` sums known fees only.
- **Country codes**: FIFA trigrams (Transfermarkt has England, Scotland, Wales, Northern Ireland, which ISO has not).
- **`transfer_id`** = first 16 hex digits of sha1(`player|date|from|to`): stable across runs, so `MERGE` finds the
  same node. 64 bits: collision probability for 36 k transfers about 10^-11.

## 9. How a rumour becomes a fact (A4, Wednesday)

A `Rumour` stays a hypothesis until `confirm_rumours` finds a `Transfer` of the same player to the target club
within 180 days after the article: then `status = confirmed` and `(:Rumour)-[:CONFIRMED_BY]->(:Transfer)`.
The rumour node is never turned into a transfer: the fact comes from Transfermarkt, the rumour only points to it.

## 10. Real numbers of the ingestion

Run of 2026-10-19 on the real file (7 leagues from 2015), local Neo4j 5 in Docker (2 GB heap).

| Nodes | Count | Relationships | Count |
| --- | --- | --- | --- |
| `Player` | 17,861 | `PLAYED_WITH` | 359,707 |
| `Club` | 2,237 (223 scope + 2,014 light) | `HAS_VALUATION` | 251,081 |
| `Transfer` | 35,867 | `MADE`, `FROM`, `TO` | 35,867 each |
| `Valuation` | 251,081 | `SENDS_TO` | 23,115 |
| `Country` | 167 | `PLAYS_FOR` | 17,861 |
| `Competition` | 7 | `CITIZEN_OF` | 17,723 (138 players without nationality) |
| | | `PLAYS_IN` | 1,468 |
| | | `IN_COUNTRY` | 7 |

Duration: extraction 3.3 s (DuckDB), facts 33.7 s, derived relationships 48.6 s, **1 min 28 s** in total.
A second run on the same file gives exactly the same counts (idempotence checked on real data), in 55 s.

Two figures worth knowing for the defense:
- 52% of the outgoing transfers of scope clubs (12,001 of 23,230) go to a club with no `PLAYS_IN` that season
  (lower or foreign league, "Without Club"): they are in the graph but not in the per-competition pipeline of B.
- Sanity check: `shortestPath` between Messi and Cristiano Ronaldo is 2 hops (both played with Arthur Melo).

## 11. Likely questions

- **Is the ingestion idempotent?** Yes: `MERGE` on unique keys, `SET` for properties, outdated `PLAYS_FOR` /
  `CITIZEN_OF` replaced, `SENDS_TO` rebuilt per club. A test runs it twice and compares every count; a second run on
  the real file leaves the counts unchanged.
- **Why not `LOAD CSV` or `neo4j-admin import`?** `neo4j-admin import` is faster but only works on an empty,
  stopped database (no incremental run, no idempotence). `LOAD CSV` would need CSV exports and gives less control
  over cleaning. The Python driver with `UNWIND` batches keeps all transformations testable in Python.
- **Why compute `PLAYED_WITH` in DuckDB and `SENDS_TO` in Cypher?** `PLAYED_WITH` comes from data that is never
  loaded (appearances), so it must be computed before loading. `SENDS_TO` is a function of `Transfer` nodes already in
  the graph: computing it in Cypher guarantees it matches them, and lets a test rebuild only its own clubs.
- **How do the tests avoid breaking the mini graph?** Negative ids and `test-a-` codes only, deleted at the end; a
  test checks that the 12 `SENDS_TO` of the mini graph are untouched when `build_sends_to` runs on a test club.
