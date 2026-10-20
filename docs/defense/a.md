# Defense sheet · Role A (data, lead)

Goal: defend every line of role A without having written it. One-line choices are in `docs/decisions/a.md`;
this sheet gives the why, the rejected alternatives, the cost and the likely questions.

## 1. What is delivered (sessions 1 and 2: Monday 19 and Tuesday 20)

| Task | Content | Files |
| --- | --- | --- |
| A1 | DuckDB to Neo4j ingestion of the facts: countries, competitions, clubs, players, transfers, valuations | `scripts/seed_data.py`, `scripts/country_codes.py`, `tests/test_a_ingest.py` |
| A2 | Derived relationships at the end of the ingestion: `PLAYED_WITH` (aggregated in DuckDB), `SENDS_TO` (aggregated in Cypher) | same files |
| A3 | Data quality: loan rule, placeholders, duplicates, future transfers, pruning, report | `scripts/data_quality.py`, `tests/test_a_quality.py` |
| A4 | Rumour confirmation job: `confirmed` / `contradicted` / `expired`, `CONFIRMED_BY` | `scripts/confirm_rumours.py`, `tests/test_a_rumours.py` |

Still to do: A5 (`docs/modeling.md`, schema diagram, data sources), then the reviews and the presentation.

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

## 9. Data quality (A3)

transfermarkt-datasets has **no loan column**, and loans are half of the moves of big clubs. Rule:

> X -> Y, then the same player Y -> X within 760 days with no fee (0 or unknown) = a loan; both legs are flagged.

| Case checked by hand | Before the fix | After |
| --- | --- | --- |
| Mbappé Monaco -> PSG 2017 (loan), 2018 (180 M purchase) | loan, purchase | loan, purchase |
| Lukaku Inter -> Chelsea 2021 (113 M), then Chelsea -> Inter 2022 and back | 113 M flagged as loan | only the 2022-2023 move is a loan |
| Morata Chelsea -> Atlético 2019-2020 (18 months), Atlético -> Juventus 2020-2022 (2 years) | missed | loans |

Two lessons: pairs must be matched **from the latest move backwards** (a return closes only one loan), and the window
must cover two seasons (returns peak at 5-6, 10-12, 17-18 and 22-24 months). Known limits: an ongoing loan with no
return recorded yet, and a loan converted into a purchase without an "end of loan" record, are not detected.

Other rules, in order (first match wins), with the real counts on the scope:

| Rule | Why | Real file |
| --- | --- | --- |
| Season before 2015 or missing | outside the scope | 5,005 |
| Dated after the ingestion day | Transfermarkt lists planned ends of loan up to 2030: not facts yet | 24 |
| Same club on both sides | meaningless edge | 0 (8 in the whole file) |
| Placeholder club (Without Club, Unknown, Retired, Career break) | not a club; "Without Club" would be the top hub of PageRank | 1,088 |
| Same route within 30 days | one move recorded twice; keep the known, highest fee | 11 |

Unknown fees stay `null` (4,270 kept transfers): replacing them by 0 or by the market value would invent a fact.
A re-run deletes what an older run loaded before a rule excluded it (1,123 transfers on the first run with A3).

## 10. How a rumour becomes a fact (A4)

A `Rumour` stays a hypothesis until the job finds what really happened, in the `Transfer` facts:

| Within 180 days after the article | After 365 days | Status |
| --- | --- | --- |
| the player moves to the target club | | `confirmed` + `(:Rumour)-[:CONFIRMED_BY]->(:Transfer)` |
| he moves elsewhere, never to the target | | `contradicted` |
| no move | no move | `expired` |
| no move | not yet | stays `pending` |

The rumour node is never turned into a transfer: the fact comes from Transfermarkt, the rumour only points to it,
so the three levels of truth stay separate. The job only reads `pending` rumours (index `rumour_status`) and its
write re-checks the status, so a decision is never overwritten and it can run after every ingestion.
Demo on the real graph: "Mbappé to Real Madrid" dated 2024-02-15 is confirmed by PSG -> Real Madrid on 2024-07-01.

## 11. Real numbers of the ingestion

Run of 2026-10-20 on the real file (7 leagues from 2015, with the A3 rules), clean local Neo4j 5 (2 GB heap).

| Nodes | Count | Relationships | Count |
| --- | --- | --- | --- |
| `Player` | 17,856 | `PLAYED_WITH` | 359,707 |
| `Club` | 2,232 (223 scope + 2,009 light) | `HAS_VALUATION` | 250,988 |
| `Transfer` | 34,744 (17,833 loans) | `MADE`, `FROM`, `TO` | 34,744 each |
| `Valuation` | 250,988 | `SENDS_TO` | 22,713 |
| `Country` | 166 | `PLAYS_FOR` | 17,850 (6 players without club) |
| `Competition` | 7 | `CITIZEN_OF` | 17,718 (138 players without nationality) |
| | | `PLAYS_IN` | 1,468 |
| | | `IN_COUNTRY` | 7 |

Duration: extraction 3.8 s (DuckDB), facts 39 s, derived relationships 27 s, **1 min 12 s** in total on an empty
database; a second run gives exactly the same counts (idempotence checked on real data).

Two figures worth knowing for the defense:
- 52% of the outgoing transfers of scope clubs go to a club with no `PLAYS_IN` that season (lower or foreign league):
  they are in the graph but not in the per-competition pipeline of B (measured before A3).
- Sanity check: `shortestPath` between Messi and Cristiano Ronaldo is 2 hops (both played with Arthur Melo).

## 12. Likely questions

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
- **Why not use the market value as the fee when it is unknown?** It is an estimate of the player, not a price paid;
  the agent would present an invented amount as a fact. `null` is honest, and sums skip it.
- **Why flag both legs of a loan?** The end of a loan is a move too (it appears in `SENDS_TO`); flagging it lets a
  query exclude loans entirely with `WHERE NOT t.is_loan`.
- **What if a rumour cites the right club but the move is a loan?** It is confirmed: the reported deal happened.
- **Why 180 and 365 days?** Given by the team contract: one transfer window and a half for the deal, one year
  before a rumour is considered dead.
