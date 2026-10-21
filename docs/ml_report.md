# Link prediction report (role C)

Question: **which pairs of clubs that never traded will trade within the next two seasons?** The answer feeds
`PREDICTED_LINK` (club to club) and `PREDICTED_MOVE` (player to club). Both are hypotheses of a model, never facts.

Reproduce: `make data`, then `python -m ml.train_link_prediction --no-write` (about 1 minute, no Neo4j needed);
without `--no-write` the predictions are written to the Neo4j of `.env`. `python -m ml.fastrp_experiment` reruns the
embedding experiment (section 5). Every number below comes from these two commands, with the data cut at
`AS_OF = 2026-10-20` (pinned in `ml/dataset.py`, so that a later run on the same DuckDB file gives the same numbers).
The notebook `demo.ipynb` shows the same results with charts.

## 1. Data and universe

Read straight from the Transfermarkt DuckDB file, with the ingestion code of role A (`scripts/seed_data.py`,
`scripts/data_quality.py`) so that the ML and the graph share one universe:

- **scope clubs**: 223 clubs that played a league game of `SCOPE_COMPETITIONS` (GB1, ES1, IT1, L1, FR1, PO1, NL1)
  since `SCOPE_FROM_SEASON` (2015): the `Club` nodes with `PLAYS_IN`;
- **transfers**: with a scope club on at least one side, cleaned like the graph (loans kept and marked; duplicates,
  placeholders such as "Without Club", transfers dated after `AS_OF` and seasons before 2015 dropped): the
  transfers behind `SENDS_TO`;
- **edges**: one row per (seller, buyer, season) with the number of transfers; the other side of a transfer may be
  a "light" club outside the scope (1,796 clubs in the graph up to 2022).

A **sample is a directed pair of scope clubs** (seller, buyer), like `SENDS_TO` and `PREDICTED_LINK`.

## 2. Temporal protocol

| Window | Feature graph (4 seasons) | Labels: new pairs during | Sampled set (1:5) | Full set (every open pair) | Used for |
| --- | --- | --- | --- | --- | --- |
| fit | 2015-2018 | 2019-2020 | 1,435 pos. + 7,175 neg. | - | model selection: fit |
| train | 2017-2020 | 2021-2022 | 1,546 pos. + 7,730 neg. | 44,988 pairs, 1,546 pos. (3.4 %) | model selection: validation; fit for the evaluation |
| test | 2019-2022 | 2023-2024 | 1,576 pos. + 7,880 neg. | 43,442 pairs, 1,576 pos. (3.6 %) | **evaluation, once**; refit for the predictions |

- A **positive** is a pair of scope clubs that trades during the label window and never traded between 2015 and the
  cut-off (the *full* history decides what is new). The reverse of an existing pair is a new pair.
- A **negative** (sampled sets) is a random pair of scope clubs that does not trade up to the end of the label
  window; 5 per positive, `random.Random(42)`.
- **Features use the 4 seasons before the cut-off**, the same length for every window (the fit window cannot have
  more). With growing windows (6 then 8 seasons), count features of the test set were shifted upwards compared with
  the ones the model learnt. Validation AUC-PR: 0.1526 with 4 seasons, 0.1517 with growing windows, 0.153 with 3,
  0.152 with 2: a tie, so the window was chosen for the principle, not on the test.
- **No leakage**: features only use seasons up to the cut-off; later seasons only give labels. A unit test rebuilds
  the features after deleting every future edge and gets the same table.
- **Model selection is temporal too**: each model is fit on the "fit" window and validated on the full "train"
  window; the best validation AUC-PR wins. A first version used 5-fold random cross-validation on one window: it
  preferred gradient boosting, the worse model one window later. Random folds measure interpolation inside one
  period, not the step to future seasons.
- **Evaluation on the full test set**, not the sampled one: AUC-PR and precision@k depend on the share of positives,
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
100 trees of depth 3, seed 42). **Baseline: the raw number of common neighbours** used as a score.

## 4. Results (full test set: 43,442 open pairs, 1,576 new links in 2023-2024)

| Model | Validation AUC-PR | Test AUC-PR | Test ROC AUC | Precision@50 | Precision@100 |
| --- | --- | --- | --- | --- | --- |
| Random ranking | - | 0.036 | 0.500 | 0.036 | 0.036 |
| Common neighbours (baseline) | 0.109 | 0.126 | 0.794 | 0.16 | 0.26 |
| **Logistic regression** (chosen) | **0.153** | **0.166** | **0.823** | **0.34** | 0.29 |
| Gradient boosting | 0.132 | 0.146 | 0.810 | 0.26 | **0.31** |

Honest reading:

- The task is hard: AUC-PR 0.17 means that most top-ranked pairs do **not** trade within two seasons.
- Still, among the 50 most probable new pairs, the model finds 17 that did trade in 2023-2024: twice the baseline
  (8) and 9 times chance (1.8). AUC-PR is 32 % above the baseline and 4.6 times random.
- At precision@100 the gap narrows (29 vs 26 for the baseline), and gradient boosting does slightly better there
  (31) although it is worse in AUC-PR and was not chosen: we report it rather than switch models after looking at
  the test.
- Coefficients of the chosen model (standardised log features): common neighbours +0.96, same country +0.55,
  preferential attachment +0.26, Adamic-Adar -0.08 (it duplicates common neighbours), PageRank gap -0.06.
  Shared trading partners and the domestic market carry the signal.

## 5. Does a FastRP embedding of clubs help? (C4: no, not kept)

The `embedding` written by `GdsService.refresh` would leak (it is computed on every season, label windows included),
so FastRP is recomputed in numpy on each 4-season feature graph with the settings of `refresh` (weights 0, 1, 1,
weighted by transfer counts). Three ways to feed it to the model, each added to the five features:

| Variant | What the model gets | Validation AUC-PR (seeds 1 / 2 / 42) | Test AUC-PR (LR) |
| --- | --- | --- | --- |
| none | five features | 0.1526 | 0.166 |
| cosine | 1 feature: cosine of the two undirected embeddings | 0.1532 / 0.1531 / 0.1528 | 0.167 |
| Hadamard | 64 features: element-wise product of the two undirected embeddings | 0.137 / 0.144 / 0.146 | 0.161 |
| directed | 64 features: seller's embedding on its sales x buyer's embedding on its purchases | 0.134 / 0.143 / 0.143 | 0.162 |

Reading: the cosine does separate the classes on its own, but encodes the same
overlap of 1- and 2-hop neighbourhoods as common neighbours, so it adds nothing (+0.0005). The 64-feature variants
add noise with about 1,400 positives to learn from, and lose. **Not kept**: the model stays at five features.

## 6. From scores to the graph

1. The chosen model is refit on the sampled test window (most recent labels), then scores every open pair of scope
   clubs (never traded since 2015) with features on the 4 latest seasons (2023-2026).
2. **Probability correction**: the model learnt on 1 positive in 6, the real rate is 3.6 %. Probabilities are
   corrected with the odds ratio: odds x (0.036 / 0.964) / (1/6 / 5/6), about 1/5.3. Without it every probability
   would be about 5.3 times too high in odds. The ranking is unchanged.
3. `PREDICTED_LINK {prob, model_version, horizon_seasons, trained_until}`: the 5 most probable destinations of each
   scope club, written with `MATCH` on both clubs, never `MERGE` of a club (an unknown club is skipped).
4. `PREDICTED_MOVE {prob, model_version, horizon_seasons, trained_until}`: for each player whose current club
   (`PLAYS_FOR`) has predicted links, the 3 most probable destinations. **Probability of a move = probability of the
   pair (current club, destination).** It is a club-level score: it says where players of this club tend to go next,
   not that this player will move (age, contract and form are not in the model).
5. `model_version = lp-logistic_regression-20261020`, `horizon_seasons = 2` (the pair trades within 2 seasons),
   `trained_until = 2024` (last labelled season the model saw). A new run deletes older `lp-` predictions, never the
   `fixture` ones of the test graph.

Run on the full graph (ingestion of A: 2,232 clubs, 17,856 players), about 35 s including the write:
**1,115 `PREDICTED_LINK`** (corrected probability 0.002 to 0.61, median 0.28) and **41,961 `PREDICTED_MOVE`** for
13,987 players. Most probable new pairs: Hellas Verona <-> Como (0.61), Stade Rennais -> RC Lens (0.58),
Wolverhampton -> Burnley (0.57), Nottingham Forest -> Bournemouth (0.57). Example: Bradley Barcola
(Paris Saint-Germain) -> Olympique Marseille 0.51, RC Strasbourg 0.32, Paris FC 0.23.

## 7. How predictions are labelled "predicted"

- **Schema**: `PREDICTED_*` relationships are a separate layer (`docs/schema.md`), never stored in `SENDS_TO` or
  `Rumour`.
- **API**: `GET /api/players/{id}/predicted-moves` returns `PredictedMove` objects with `nature: "predicted"`,
  `basis: "club_pair"`, `probability`, `horizon_seasons`, `trained_until` and `model_version`.
- **Agent**: the `predicted_moves` tool tags its result `nature="predicted"` with evidence
  `PREDICTED_MOVE:<player>-><club>`; the verifier forces any claim citing it to `predicted`, and the answer shows it
  with a `[prediction]` tag.

## 8. Limits

- **No direction**: the five features are symmetric in (seller, buyer), so A -> B and B -> A get the same
  probability (Hellas Verona -> Como and Como -> Hellas Verona: 0.61 both). The directed FastRP variant did not
  fix it; directed counting features (out-degree of the seller, in-degree of the buyer, signed PageRank gap) are the
  next thing to try.
- **Pair-level, not player-level**: `PREDICTED_MOVE` gives the same destinations to every player of a club.
- **Cold start**: a club with no transfer in the 4-season window gets features at 0 (promoted clubs suffer).
- **Horizon of two seasons only**; the live predictions (seasons 2025-2026 onward) are not evaluated yet: their label
  window is the future.
- **Five topological features only**: no fees, league level, rivalries or player profiles. Precision stays low in
  absolute terms.
- **Probability correction** assumes the real rate of the next two seasons is close to the one of 2023-2024 (3.6 %).
