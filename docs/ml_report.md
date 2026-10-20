# Link prediction report (role C)

Question: **which pairs of clubs that never traded will trade within the next two seasons?** The answer feeds
`PREDICTED_LINK` (club to club) and `PREDICTED_MOVE` (player to club). Both are hypotheses of a model, never facts.

Reproduce: `make data`, then `python -m ml.train_link_prediction --no-write` (about 1 minute, no Neo4j needed);
without `--no-write` the predictions are written to the Neo4j of `.env`. Numbers below: run of 2026-10-20.

## 1. Data and universe

Read straight from the Transfermarkt DuckDB file, with the ingestion code of role A (`scripts/seed_data.py`,
`scripts/data_quality.py`) so that the ML and the graph share one universe:

- **scope clubs**: 223 clubs that played a league game of `SCOPE_COMPETITIONS` (GB1, ES1, IT1, L1, FR1, PO1, NL1)
  since `SCOPE_FROM_SEASON` (2015): the `Club` nodes with `PLAYS_IN`;
- **transfers**: with a scope club on at least one side, cleaned like the graph (loans kept and marked, duplicates,
  placeholders such as "Without Club", future dates and seasons before 2015 dropped): the transfers behind `SENDS_TO`;
- **edges**: one row per (seller, buyer, season) with the number of transfers; the other side of a transfer may be
  a "light" club outside the scope (1,796 clubs in the graph up to 2022).

A **sample is a directed pair of scope clubs** (seller, buyer), like `SENDS_TO` and `PREDICTED_LINK`.

## 2. Temporal protocol

| Window | Feature graph | Labels: new pairs during | Sampled set (1:5) | Full set (every open pair) | Used for |
| --- | --- | --- | --- | --- | --- |
| fit | 2015-2018 | 2019-2020 | 1,435 pos. | - | model selection: fit |
| train | 2015-2020 | 2021-2022 | 1,546 pos. + 7,730 neg. | 44,988 pairs, 1,546 pos. (3.4 %) | model selection: validation; final fit for evaluation |
| test | 2015-2022 | 2023-2024 | 1,576 pos. + 7,880 neg. | 43,442 pairs, 1,576 pos. (3.6 %) | **evaluation, once**; refit for the predictions |

- A **positive** is a pair of scope clubs, both present in the feature graph, that trades during the label window and
  never traded up to the end of the feature window. The reverse of an existing pair is a new pair.
- A **negative** (sampled sets) is a random pair of scope clubs of the graph that does not trade up to the end of the
  label window; 5 per positive, `random.Random(42)`.
- **No leakage**: every feature is computed on the feature graph only; later seasons only give labels. A unit test
  rebuilds the features after deleting every future edge and gets the same table.
- **Model selection is temporal too**: each model is fit on the "fit" window and validated on the full "train"
  window; the best validation AUC-PR wins. A first version used 5-fold random cross-validation on the train window:
  it preferred gradient boosting (0.502 vs 0.494), which is the worse model one window later (see section 4).
  Random folds measure interpolation inside one period, not the step to future seasons.
- **Evaluation on the full test set**, not the sampled one: precision@k and AUC-PR depend on the share of positives,
  and the real share is 3.6 %, not 1 in 6.

## 3. Features

| Feature | Graph | Definition |
| --- | --- | --- |
| `common_neighbours` | undirected | clubs that traded with both clubs |
| `adamic_adar` | undirected | sum of 1 / log(degree) of the common neighbours |
| `preferential_attachment` | undirected | degree(seller) x degree(buyer) |
| `same_country` | - | both scope leagues in the same country (latest scope league of each club) |
| `pagerank_gap` | directed, weighted by `count` | abs(PageRank(seller) - PageRank(buyer)), networkx |

Models: logistic regression (log1p of the features, standardised) and gradient boosting (scikit-learn defaults,
100 trees of depth 3, seed 42). Baseline: the raw number of common neighbours as a score.

## 4. Results (full test set: 43,442 open pairs, 1,576 new links in 2023-2024)

| Model | Validation AUC-PR | Test AUC-PR | Test ROC AUC | Precision@50 | Precision@100 |
| --- | --- | --- | --- | --- | --- |
| Random ranking | - | 0.036 | 0.500 | 0.036 | 0.036 |
| Common neighbours (baseline) | 0.115 | 0.126 | 0.795 | 0.26 | 0.23 |
| **Logistic regression** (chosen) | **0.152** | **0.160** | **0.819** | 0.26 | **0.31** |
| Gradient boosting | 0.117 | 0.126 | 0.794 | 0.28 | 0.23 |

Honest reading:

- The task is hard: AUC-PR 0.16 means that most top-ranked pairs do **not** trade within two seasons. Still,
  among the 100 most probable new pairs, 31 did trade in 2023-2024, against 3.6 expected at random (8.6 times).
- Logistic regression beats the baseline by 27 % in AUC-PR (0.160 vs 0.126) and at precision@100 (31 vs 23).
  At precision@50 it only ties the baseline (13 hits of 50): the very top of the ranking is driven by common
  neighbours in both cases.
- Gradient boosting does no better than the baseline on later seasons: with five correlated features it fits the
  training window, and the gain does not carry over.
- Coefficients of the chosen model (standardised log features): common neighbours +0.65, same country +0.48,
  Adamic-Adar +0.33, preferential attachment +0.13, PageRank gap -0.09. Shared trading partners and the domestic
  market carry the signal; the PageRank gap barely helps.

## 5. From scores to the graph

1. The chosen model is refit on the sampled test window (most recent labels), then scores every open pair of scope
   clubs on the graph of all known seasons (2015 to today).
2. **Probability correction**: the model learnt on 1 positive in 6, the real rate is 3.6 %. Probabilities are
   corrected with the odds ratio: odds x (0.036 / 0.964) / (1/6 / 5/6). Without it, every probability would be
   about 5.3 times too high in odds. The ranking is unchanged.
3. `PREDICTED_LINK {prob, model_version}`: the 5 most probable destinations of each scope club (written with
   `MATCH` on both clubs, never `MERGE` of a club, so an unknown club is skipped).
4. `PREDICTED_MOVE {prob, model_version}`: for each player whose current club (`PLAYS_FOR`) has predicted links,
   the 3 most probable destinations. **Probability of a move = probability of the pair (current club, destination).**
   This is a club-level score: it says where players of this club tend to go next, not that this player will move
   (age, contract and form are not in the model). The API and the agent must say so.
5. `model_version` = `lp-<model>-<yyyymmdd>`; a new run deletes the older `lp-` predictions, never the `fixture`
   ones of the test graph.

## 6. How predictions are labelled "predicted"

- Schema: `PREDICTED_*` relationships are a separate layer (`docs/schema.md`), never stored in `SENDS_TO` or `Rumour`.
- API: `GET /api/players/{id}/predicted-moves` returns `PredictedMove` objects with `nature: "predicted"`,
  `probability` and `model_version`.
- Agent: the `predicted_moves` tool tags its result `nature="predicted"` with evidence `PREDICTED_MOVE:<player>-><club>`;
  the verifier forces any claim citing it to `predicted`.

## 7. Limits

- Cold start: a club with no transfer in the feature window cannot be ranked (all features 0); promoted clubs suffer.
- Pair-level, not player-level: `PREDICTED_MOVE` gives the same destinations to every player of a club.
- Horizon of two seasons only; the last window (2025-2026) is not labelled yet, so the live predictions are not
  evaluated.
- Five topological features only: no fees, no league level, no player profile. Precision stays low in absolute terms.
- Probability correction assumes the real rate of the next two seasons is close to the one of 2023-2024 (3.6 %).
