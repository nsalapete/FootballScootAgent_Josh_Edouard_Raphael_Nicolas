# Decisions of role A (data, lead)

One line per choice not settled by `CLAUDE.md`, in the order it was made.

## Step 0 · what the DuckDB file contains

`data/transfermarkt-datasets.duckdb` (201 MB, commit `e44f186d` of transfermarkt-datasets), schema `main`, 13 tables:

| Table | Rows | Columns used by the ingestion |
| --- | --- | --- |
| `competitions` | 65 | `competition_id`, `name` (slug, e.g. `premier-league`), `country_name` |
| `clubs` | 796 | `club_id` (**VARCHAR**), `name`, `domestic_competition_id` (current league only) |
| `games` | 88,958 | `game_id` (VARCHAR), `competition_id`, `season` (VARCHAR `'2015'`), `home/away_club_id`, `home/away_club_name` |
| `appearances` | 1,894,350 | `game_id` (INTEGER), `player_id`, `player_club_id` |
| `players` | 50,149 | `player_id`, `name`, `position`, `sub_position`, `date_of_birth` (TIMESTAMP), `foot`, `height_in_cm`, `market_value_in_eur`, `current_club_id` (VARCHAR), `current_club_name`, `country_of_citizenship`, `last_season` |
| `transfers` | 175,165 | `player_id`, `transfer_date`, `transfer_season` (label `22/23`), `from/to_club_id`, `from/to_club_name`, `transfer_fee` (DECIMAL) |
| `player_valuations` | 656,301 | `player_id`, `date`, `market_value_in_eur` |
| `game_lineups`, `game_events`, `club_games`, `countries`, `national_teams`, `version` | | not used |

Findings that shape the ingestion:

- No loan column in `transfers`. 96,085 fees are `0` and 61,526 are `NULL` (unknown). Pseudo clubs exist as ids:
  `515` Without Club (3,785 transfers), `75` Unknown, `123` Retired, `2113` Career break. 8 transfers have the same
  club on both sides, 475 are dated after 2026-10-19 (up to 2030), no exact duplicate (player, date, from, to).
- `countries.country_code` holds league codes (`GB1`, `L1`), not country codes, and only 124 of the 202 nationalities.
- `position` has a `Missing` value (586 players).
- Scope (7 leagues from 2015): 26,421 league games, 223 clubs, 771,697 appearances of 12,578 players,
  359,707 distinct teammate pairs.

## A1 · core ingestion

- **Country code**: FIFA trigram (`FRA`, `ENG`, `POR`, `BRA`, as in the mini graph) from a hand-written table in
  `scripts/country_codes.py` (new file, ingestion data so role A). FIFA rather than ISO because Transfermarkt splits
  England, Scotland, Wales and Northern Ireland. Territories without FIFA code use ISO alpha-3. `Türkiye`/`Turkey` and
  `Neukaledonien`/`New Caledonia` are folded. An unknown name keeps its own name as code (none in the current file).
- **Scope clubs** = clubs that played a league game of `SCOPE_COMPETITIONS` from `SCOPE_FROM_SEASON` on (taken from
  `games`, not from `clubs.domestic_competition_id`, which only gives the current league and misses relegated clubs).
  Name from `clubs`, or from `games` when the club is missing there.
- **`PLAYS_IN {season}`** = one relationship per (club, league, season) with at least one game, from `games`.
- **Scope players** = appeared for a scope club in a scope game, or current club in scope and `last_season` from the
  first scope season on (drops long-retired players), or made a scope transfer.
- **Scope transfers** = season (parsed from the label) from `SCOPE_FROM_SEASON` on, scope club on at least one side,
  player present in `players`. Nothing else is filtered in A1 (pseudo clubs, loans, future dates: A3).
- **Light clubs** = clubs cited by a scope transfer or as a current club, outside the scope: `MERGE ... ON CREATE SET
  name`, so a light club never overwrites the name of a scope club.
- **Fees**: `NULL` stays `null` (unknown), `0` stays `0` (free transfer); decimals rounded to whole euros.
- **`is_loan`**: `false` for every transfer in A1; the loan rule comes with A3 (no column marks loans).
- **Valuations** from January 1st of the first scope season on, id `<player_id>-<date>` as in the schema.
- **`position`**: `Missing` becomes `null` (the schema lists four values only).
- **Competition name**: slug turned into title case (`premier-league` -> `Premier League`).
- **Current club and nationality**: on a re-run, an outdated `PLAYS_FOR` / `CITIZEN_OF` is deleted before the new
  one is merged, so the graph always matches the latest file (still idempotent).
- **Transactions**: one write transaction per batch of 5,000 rows; the ingestion opens its own client with a 600 s
  limit instead of the 10 s API limit (`NEO4J_QUERY_TIMEOUT_S`), which a cold `MERGE` batch can exceed.
- **Season parsing** in Python (`parse_season`, unit-tested), not in SQL: `'99/00'` -> 1999, `'22/23'` -> 2022,
  a four-digit start year is kept as is, anything else -> `None` (transfer dropped).

## A2 · derived relationships

- **`PLAYED_WITH` source**: `appearances` (players who actually played, the 1.8 M rows the schema refers to), not
  `game_lineups`, which also lists unused substitutes. Two players are teammates in a game when both appear for the same
  club in the same scope league game.
- **`PLAYED_WITH` scope**: league games of the scope competitions from `SCOPE_FROM_SEASON` on, scope clubs only (same
  scope as `PLAYS_IN`); cup and European games are left out so `matches` counts comparable games.
- **`PLAYED_WITH` aggregation** in DuckDB (temp table of scope appearances, self-join on game and club with
  `a.player_id < b.player_id`): `matches` = distinct games, `first_season` / `last_season` = min / max game season.
  Only the aggregated pairs are sent to Neo4j, in batches of 5,000.
- **`SENDS_TO`**: built in Cypher by `build_sends_to(db, club_ids)`, which first deletes the outgoing `SENDS_TO` of
  the given clubs then recreates them from `Transfer` nodes, so a transfer removed by a later run leaves no stale count.
  Restricted to the source clubs passed as parameter (500 per transaction): a test only rebuilds its own clubs.
- **`SENDS_TO` content**: every transfer counts in `count` (loans included, as in the mini graph); `total_fee_eur` =
  `sum(t.fee_eur)`, which skips unknown fees and gives 0 when no fee of the pair is known.

## A3 · data quality (`scripts/data_quality.py`)

- **Loan rule** (no loan column): X -> Y then, for the same player, Y -> X within 760 days with no fee (0 or unknown)
  = a loan; both legs get `is_loan = true` (the loan and its end, so `SENDS_TO` counts both directions as before).
  760 days because returns peak at 5-6, 10-12, 17-18 and 22-24 months. Pairs are matched from the latest move
  backwards and a move is in one pair at most: a first version (13 months, pairs read forwards) flagged Lukaku's
  113 M purchase by Chelsea as a loan and missed Morata's 18-month and 2-year loans. Checked by hand on Mbappé,
  Mount, Lukaku, Morata. Known limits: an ongoing loan (no return yet) and a loan ended by a purchase
  (Mbappé 2017-2018 is caught because Transfermarkt records the end of loan before the purchase) are not detected.
- **Detection before filtering**: the loan rule sees every transfer of the player, even those dropped next, so a
  loan whose return is planned after the ingestion day is still a loan.
- **Future transfers**: dated after the ingestion day = not a fact yet, dropped (`today` is a parameter for tests).
- **Placeholders**: Transfermarkt ids 515 Without Club, 75 Unknown, 123 Retired, 2113 Career break are not clubs.
  A transfer to or from one is dropped (it would make "Without Club" a hub of the transfer network for PageRank),
  no `Club` node is created for them, and a player whose current club is one has no `PLAYS_FOR` (6 players).
- **Same club on both sides**: dropped (0 left in the scope after the season filter; 8 in the whole file).
- **Duplicates**: no exact duplicate exists, but the same route of the same player is sometimes recorded twice within
  a few days (end of loan then purchase, or a corrected date). Moves on one route within 30 days are one move; the
  record with a known fee is kept, then the highest fee, then the latest date.
- **Missing fees**: kept as `null` (unknown), never replaced by 0 or by the market value; reported with zero fees.
- **Reserve and youth teams** (Chelsea U23, RM Castilla) are separate Transfermarkt clubs: kept as light clubs, the
  move to the first team stays a transfer. Not filtered: no reliable id link between a club and its reserve team.
- **Pruning**: the ingestion deletes the `Transfer` nodes whose id a rule now drops and the placeholder clubs, so a
  graph loaded before these rules is cleaned by a re-run. Nodes that fall out of the scope only because of a new
  rule (5 players on the real file) stay until a clean reload: the ingestion never deletes a node it cannot prove it
  created (the test database shares the mini graph).
- **Report** printed at the end of every ingestion (`Data quality` block): raw, dropped per reason, kept, loans,
  unknown and zero fees, players without club.
