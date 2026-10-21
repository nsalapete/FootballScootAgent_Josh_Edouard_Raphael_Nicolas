# Graph modeling

Owner: A. Why the graph looks the way it does. The contract itself (labels, keys, properties, indexes) is
`docs/schema.md`; the one-line ingestion choices are in `docs/decisions/a.md`.

The graph answers recruitment questions: who could replace a player, which clubs sell to which, who played with whom,
what the press says and whether it came true. Every modeling choice below starts from one of these questions.

## 1. The schema in one picture

Same content as `docs/schema.drawio` (open it in [draw.io](https://app.diagrams.net)). Solid boxes are facts from
Transfermarkt, dashed edges are derived from the facts, orange elements are hypotheses (press and model).

```mermaid
flowchart LR
    Player([Player])
    Club([Club])
    Competition([Competition])
    Country([Country])
    Transfer([Transfer])
    Valuation([Valuation])
    Article([Article])
    Rumour([Rumour])

    Player -->|MADE| Transfer
    Transfer -->|FROM| Club
    Transfer -->|TO| Club
    Player -->|PLAYS_FOR| Club
    Player -->|HAS_VALUATION| Valuation
    Player -->|CITIZEN_OF| Country
    Club -->|"PLAYS_IN {season}"| Competition
    Competition -->|IN_COUNTRY| Country

    Player -.-|"PLAYED_WITH {matches, first_season, last_season}"| Player
    Club -.->|"SENDS_TO {count, total_fee_eur}"| Club

    Article -->|REPORTS| Rumour
    Rumour -->|ABOUT| Player
    Rumour -->|TARGET| Club
    Rumour -->|CONFIRMED_BY| Transfer
    Player -.->|"PREDICTED_MOVE {prob, model_version}"| Club
    Club -.->|"PREDICTED_LINK {prob, model_version}"| Club

    classDef fact fill:#dae8fc,stroke:#6c8ebf,color:#000
    classDef hypothesis fill:#ffe6cc,stroke:#d79b00,color:#000
    class Player,Club,Competition,Country,Transfer,Valuation fact
    class Article,Rumour hypothesis
```

| Layer | Elements | Source | Shown by the agent as |
| --- | --- | --- | --- |
| Facts | `Player`, `Club`, `Competition`, `Country`, `Transfer`, `Valuation` and their relationships | Transfermarkt | verified |
| Derived | `PLAYED_WITH`, `SENDS_TO`, `pagerank`, `community_id`, `embedding` | computed from the facts | verified (computed) |
| Hypotheses | `Article`, `Rumour`, `PREDICTED_MOVE`, `PREDICTED_LINK` | press + LLM, link prediction model | rumour or predicted |

## 2. Modeling choices and rejected alternatives

### 2.1 A transfer is a node, not a relationship

| Option | Verdict |
| --- | --- |
| `(:Player)-[:TRANSFERRED {date, fee}]->(:Club)` | rejected |
| `(:Club)-[:TRANSFERRED {player_id, date, fee}]->(:Club)` | rejected |
| `(:Player)-[:MADE]->(:Transfer)-[:FROM]->(:Club)`, `(:Transfer)-[:TO]->(:Club)` | **kept** |

A transfer involves three entities (player, origin club, destination club) and a relationship has only two ends:
the player-to-club edge loses the origin, the club-to-club edge turns the player into a plain id that cannot be
traversed. A transfer is also the **target** of other facts: a confirmed rumour points to it with
`CONFIRMED_BY`, and a relationship cannot point to a relationship in Neo4j. As a node it carries its own key
(`transfer_id`, a stable hash), so `MERGE` makes the ingestion idempotent, and an index on `Transfer(date)` serves
the "pipeline since" queries.

Cost: one more hop (`(p)-[:MADE]->(t)-[:TO]->(c)`). Each `Transfer` has exactly three relationships, so the extra
expansion is constant and cheap. For the club-to-club questions, which are the frequent ones, `SENDS_TO` (2.3) removes
the hop entirely.

### 2.2 Appearances are aggregated into `PLAYED_WITH`

| Option | Verdict |
| --- | --- |
| `(:Player)-[:APPEARED_IN]->(:Game)<-[:APPEARED_IN]-(:Player)` with 1.9 M appearances and 89 k games | rejected |
| `(:Player)-[:PLAYED_WITH {matches, first_season, last_season}]-(:Player)` computed before loading | **kept** |

The agent asks "did A and B play together, how often, when" and "how far is A from B through teammates". With
`Game` nodes, every such question becomes a two-hop join through games, recomputed at each request, and a 6-hop
teammate path becomes a 12-hop path through millions of nodes. The aggregation (self-join of appearances on game and
club, `a < b`) runs **once** in DuckDB, a columnar engine made for it, and Neo4j only stores the 360 k pairs.
Each pair is stored once, from the smaller to the larger id, and read without direction (`-[:PLAYED_WITH]-`).

What we give up: individual games (minutes, goals, lineups). No question of the project needs them, and the DuckDB
file stays the place to read them. Only league games of the scope count, so `matches` compares like with like.

### 2.3 `SENDS_TO` is materialised

| Option | Verdict |
| --- | --- |
| Aggregate `Transfer` nodes at query time | rejected for the API and GDS |
| `(:Club)-[:SENDS_TO {count, total_fee_eur}]->(:Club)` rebuilt at the end of every ingestion | **kept** |

"Which clubs sell to this club", "top sellers", PageRank, communities and link prediction all read a weighted
club-to-club network. Materialising it turns each of those into one hop, and the GDS projection becomes a plain
native projection of one relationship type. It is safe because it is never written by hand: the ingestion deletes
and rebuilds the `SENDS_TO` of the clubs it loads from the `Transfer` nodes, so it cannot drift from the facts.
Unknown fees are skipped by the sum, never counted as zero. Questions that need dates or loans still read `Transfer`.

### 2.4 The league depends on the season

| Option | Verdict |
| --- | --- |
| `Club.league` property, or one `(:Club)-[:PLAYS_IN]->(:Competition)` | rejected |
| `Competition` node per season (`Premier League 2022`) | rejected |
| `(:Club)-[:PLAYS_IN {season}]->(:Competition)`, one relationship per season played | **kept** |

Clubs are promoted and relegated: in scope, a club that went down in 2018 would be attached to the wrong league, or
to none, with a single property. The pipeline queries ("where do this league's players go") need the league of the
destination club **at the time of the transfer**, which is `PLAYS_IN {season: t.season}`. A competition node per
season would multiply nodes (7 x 12) for no gain: the season is an attribute of the participation, not of the league.

### 2.5 Hypotheses are kept apart from facts

| Option | Verdict |
| --- | --- |
| A rumour as a `Transfer` with `status = 'rumour'`, or a prediction as a `SENDS_TO` with a flag | rejected |
| Separate labels and types: `Article`, `Rumour`, `PREDICTED_MOVE`, `PREDICTED_LINK` | **kept** |

The agent must never present a rumour or a prediction as a fact. With a flag, every query on facts would need
`WHERE status = 'fact'`, and one forgotten filter would leak a rumour into an answer. With separate labels and types,
a query on `Transfer` or `SENDS_TO` cannot see a hypothesis at all. The link between both worlds is explicit and
one-way: a `Rumour` becomes true only when the confirmation job finds the matching `Transfer` and adds
`CONFIRMED_BY`; the rumour is never turned into a transfer. Predictions carry `model_version`, so a new model run
replaces its own edges without touching anything else.

### 2.6 Smaller choices

- **`Valuation` as a node** rather than a list property on `Player`: one node per (player, date), indexed by date,
  so the agent can read the trend and a value at a given date. The latest value is also copied to
  `Player.market_value_eur`, where the budget filter and its index need it.
- **`Country` as a node**: shared by players (`CITIZEN_OF`) and competitions (`IN_COUNTRY`), so "players of the
  same nationality as the league" is a pattern, not a string comparison. Codes are FIFA trigrams (Transfermarkt
  separates England, Scotland, Wales and Northern Ireland, ISO does not).
- **`PLAYS_FOR` is only the current club**: the history is the chain of `Transfer` nodes; storing it twice would
  allow the two to disagree.
- **Light clubs**: the other side of a scope transfer (a Saudi or MLS club) is a `Club` with id and name only, so
  every transfer keeps both ends; they have no `PLAYS_IN`.
- **Natural keys**: Transfermarkt ids for players and clubs, a stable hash for transfers, `<player_id>-<date>` for
  valuations. Ids survive a re-run, so `MERGE` updates instead of duplicating.

## 3. Data sources

### 3.1 Datasets

| Source | What we take | Link | License and terms |
| --- | --- | --- | --- |
| transfermarkt-datasets (David Cariboo) | the facts: competitions, clubs, games, appearances, players, transfers, market values | [GitHub](https://github.com/dcaribou/transfermarkt-datasets), [Kaggle](https://www.kaggle.com/datasets/davidcariboo/player-scores), [DuckDB file](https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/transfermarkt-datasets.duckdb) | CC0-1.0 (public domain dedication). The data is scraped from [Transfermarkt](https://www.transfermarkt.com); we cite it as the origin and only use it for teaching. |
| The Guardian, football transfers RSS | press articles for rumours (role D) | [feed](https://www.theguardian.com/football/transfers/rss) | Guardian terms: the graph keeps the URL, title, date and one quoted sentence, never the full text. |
| BBC Sport, football RSS | same | [feed](https://feeds.bbci.co.uk/sport/football/rss.xml) | BBC terms, same use. |
| GDELT DOC 2.0 API | article search across outlets | [API](https://api.gdeltproject.org/api/v2/doc/doc) | free and open, with citation of the GDELT Project. |

Version used: the DuckDB file of transfermarkt-datasets at commit `e44f186d` (201 MB, 13 tables), downloaded by
`make data`. The upstream project has paused its updates: the data is current to **6 July 2026**, which is also the
date of the last game in the file. Transfers dated after the ingestion day are planned moves (ends of loan up to
2030) and are dropped (`docs/decisions/a.md`, A3).

Rumour extraction from the press uses an LLM (role D, `docs/guardrails.md`); the confirmation of rumours against the
Transfermarkt facts is deterministic Cypher (`scripts/confirm_rumours.py`).

### 3.2 Scope kept

| Setting | Value | Why |
| --- | --- | --- |
| `SCOPE_COMPETITIONS` | `GB1, ES1, IT1, L1, FR1, PO1, NL1` (England, Spain, Italy, Germany, France, Portugal, Netherlands) | the "big five" plus the two main selling leagues of Europe, so the network shows where clubs buy |
| `SCOPE_FROM_SEASON` | `2015` | ten full seasons: enough history for link prediction, small enough to load in about a minute |
| Clubs | 223 that played a league game of the scope, plus 2,009 light clubs on the other side of a transfer | read from `games`, not from the current league of `clubs`, so relegated clubs are kept |
| Players | 17,856 | appeared for a scope club, made a scope transfer, or still play for a scope club |
| Games | 26,421 league games, 771,697 appearances, not loaded | aggregated into 359,707 `PLAYED_WITH` pairs |
| Valuations | from 1 January 2015 on | same time window as the transfers |

Both settings are environment variables (`.env.example`): the same code loads a smaller scope for a quick demo.

### 3.3 Ingestion numbers

Run of 2026-10-20 on the full file, empty local Neo4j 5 (2 GB heap), `make seed`.

| Nodes | Count | Relationships | Count |
| --- | --- | --- | --- |
| `Player` | 17,856 | `PLAYED_WITH` | 359,707 |
| `Club` | 2,232 | `HAS_VALUATION` | 250,988 |
| `Transfer` | 34,744 (17,833 flagged as loans) | `MADE`, `FROM`, `TO` | 34,744 each |
| `Valuation` | 250,988 | `SENDS_TO` | 22,713 |
| `Country` | 166 | `PLAYS_FOR` | 17,850 |
| `Competition` | 7 | `CITIZEN_OF` | 17,718 |
| | | `PLAYS_IN` | 1,468 |
| | | `IN_COUNTRY` | 7 |
| **Total** | **305,993** | **Total** | **774,683** |

Duration: **1 min 12 s** (DuckDB extraction 3.8 s, facts 39 s, derived relationships 27 s). A second run gives
exactly the same counts (idempotent). Data quality on the scope: 5,005 transfers outside the seasons, 24 in the
future, 1,088 with a placeholder club ("Without Club", "Retired"...) and 11 duplicates dropped; 4,270 kept
transfers have an unknown fee, stored as `null`.
