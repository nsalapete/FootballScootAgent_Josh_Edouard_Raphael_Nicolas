# Mini-graphe de test

Données **fictives** (`mini_graph.cypher`), conformes à `docs/schema.md`. Elles servent à tester chaque partie
sans attendre l'ingestion réelle. Chargées par `tests/conftest.py` (fixture `neo4j_db`) dans le Neo4j de test
(`make test-db`, port 7688), qui est vidé à chaque session de tests.

Owner : A. Personne d'autre ne modifie ce fichier. C peut ajouter `tests/fixtures/*gds*.cypher`, chargé après
celui-ci, à une condition : il ne fait que des `SET` de propriétés sur les nœuds existants, sans créer ni supprimer
de nœud ou de relation (sinon les comptes de `tests/test_a_fixture.py` cassent la CI de tout le monde).

Un test qui écrit dans la base crée ses propres données (ids négatifs, ou chaînes préfixées `test-<lettre>-`),
ne touche jamais aux données ci-dessous, et supprime ce qu'il a créé à la fin.
Chaque club joue dans le même championnat de 2019 à 2024 (une relation `PLAYS_IN` par saison).

## Contenu

- 4 pays, 3 compétitions (FR1, GB1, PO1), 8 clubs, 16 joueurs, 12 transferts, 5 valorisations,
  2 articles, 2 rumeurs, 1 `PREDICTED_MOVE`, 1 `PREDICTED_LINK`.
- 12 relations `SENDS_TO` (une par paire de clubs), 14 relations `PLAYED_WITH`.

## Réponses attendues (à utiliser dans les tests)

| Question | Réponse |
| --- | --- |
| Club qui vend le plus | SC Alvorada (7) : 4 transferts sortants (t01, t02, t06, t07) |
| Filière sortante de SC Alvorada | GB1 : 2 transferts, 50 000 000 € · FR1 : 1, 9 000 000 € · PO1 : 1, 0 € (prêt sans montant) |
| Chemin de coéquipiers 1 → 4 | 1, 6, 2, 3, 4 : 4 sauts |
| Chemin 13 → 8 | 13, 1, 6, 2, 3, 4, 8 : 6 sauts (la limite) |
| Chemin 14 → 7 | 7 sauts : **aucun résultat** avec la borne de 6 |
| Chemin 1 → 10 | aucun (composantes séparées) |
| Joueur sans coéquipier | Samuel Okafor (16) |
| Nom ambigu | « Gabriel » : Gabriel Souza (8) et Gabriel Rocha (9) |
| Latéraux gauches de moins de 23 ans au 2026-10-19 | Diogo Santos (5, 22 ans), Nathan Roux (14, 21 ans) |
| Latéraux gauches avec `max_age=23` au 2026-10-19 | les deux ci-dessus et Lucas Marchand (1, 23 ans) |
| Prêt | t07 (Andre Martins, 7 → 8), `fee_eur` nul, `is_loan` vrai |
| Valorisations de Lucas Marchand | 3 M€ (2022-06-30), 7 M€ (2023-06-30), 12 M€ (2024-12-15) |
| Rumeur en attente | r1 : Diogo Santos vers Wexley Athletic, confiance 0,6 |
| Rumeur confirmée | r2 : Joao Ferreira vers Kingsbridge United, `CONFIRMED_BY` t01 |
| Prédiction | Gabriel Rocha (9) vers Harrowgate FC (5), probabilité 0,42 |

Les âges dépendent de la date du jour : un test d'âge doit calculer la date de référence, pas la coder en dur.
