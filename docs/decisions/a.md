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
