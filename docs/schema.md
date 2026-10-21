# Schéma du graphe (contrat d'équipe)

Owner : A. Ce fichier est le contrat partagé : on peut **ajouter** une propriété, jamais renommer ni supprimer
un label, une relation ou une propriété listés ici sans prévenir toute l'équipe.

## Trois couches de vérité

| Couche | Éléments | Origine | Affichée comme |
| --- | --- | --- | --- |
| Faits | `Player`, `Club`, `Competition`, `Country`, `Transfer`, `Valuation` et leurs relations | Transfermarkt | vérifié |
| Dérivés | `PLAYED_WITH`, `SENDS_TO`, propriétés `pagerank`, `community_id`, `embedding` | calculés depuis les faits | vérifié (calculé) |
| Hypothèses | `Article`, `Rumour`, `PREDICTED_MOVE`, `PREDICTED_LINK` | presse + LLM, modèle ML | rumeur ou prédit |

## Nœuds

| Label | Clé unique | Propriétés |
| --- | --- | --- |
| `Player` | `player_id` (int, id Transfermarkt) | `name`, `position` (Goalkeeper, Defender, Midfield, Attack), `sub_position`, `date_of_birth` (date), `market_value_eur` (int), `foot`, `height_cm` ; écrites par C : `embedding` (liste de flottants) |
| `Club` | `club_id` (int) | `name` ; écrites par C : `pagerank` (float), `community_id` (int) |
| `Competition` | `competition_id` (str, ex. `FR1`) | `name` |
| `Country` | `code` (str, ex. `FRA`) | `name` |
| `Transfer` | `transfer_id` (str : hash joueur + date + origine + destination) | `date` (date), `season` (int, année de début), `fee_eur` (int ou null), `is_loan` (bool) |
| `Valuation` | `valuation_id` (str : `<player_id>-<date>`) | `date` (date), `value_eur` (int) |
| `Article` | `url` | `source`, `title`, `published_at` (date) |
| `Rumour` | `rumour_id` (str) | `status` (pending, confirmed, expired, contradicted), `confidence` (0 à 1), `quote` (phrase source), `extracted_at` (datetime) ; écrite par A : `resolved_on` (date où le statut a quitté `pending`) |

## Relations

```
(:Player)-[:MADE]->(:Transfer)
(:Transfer)-[:FROM]->(:Club)
(:Transfer)-[:TO]->(:Club)
(:Player)-[:PLAYS_FOR]->(:Club)                                   // club actuel
(:Player)-[:HAS_VALUATION]->(:Valuation)
(:Player)-[:CITIZEN_OF]->(:Country)
(:Club)-[:PLAYS_IN {season}]->(:Competition)
(:Competition)-[:IN_COUNTRY]->(:Country)

(:Player)-[:PLAYED_WITH {matches, first_season, last_season}]->(:Player)   // stockée une fois, du plus petit id vers le plus grand ; à parcourir sans direction
(:Club)-[:SENDS_TO {count, total_fee_eur}]->(:Club)                         // agrégée depuis Transfer, montants nuls exclus des sommes

(:Article)-[:REPORTS]->(:Rumour)
(:Rumour)-[:ABOUT]->(:Player)
(:Rumour)-[:TARGET]->(:Club)
(:Rumour)-[:CONFIRMED_BY]->(:Transfer)
(:Player)-[:PREDICTED_MOVE {prob, model_version}]->(:Club)                 // C : destination probable d'un joueur
(:Club)-[:PREDICTED_LINK {prob, model_version}]->(:Club)                   // C : nouvelle paire de clubs prédite
```

Diagramme : `docs/schema.drawio` (draw.io) et sa version Mermaid dans `docs/modeling.md`.

## Contraintes et index

Définis dans `scripts/constraints.cypher` (idempotent) : 8 contraintes d'unicité (une par clé ci-dessus),
index composite `Player(position, date_of_birth)`, index `Player(market_value_eur)`, `Transfer(date)`,
`Rumour(status)`, et deux index full-text dont les **noms sont un contrat** : `player_name_ft` et `club_name_ft`.

## Choix de modélisation (résumé, détail dans `docs/modeling.md`)

- Le transfert est un nœud : il porte montant, prêt et date, et une rumeur confirmée pointe vers lui.
- Les 1,8 M d'apparitions ne sont pas chargées : elles sont agrégées en `PLAYED_WITH`.
- `SENDS_TO` est matérialisée : utile aux endpoints, et la projection GDS devient triviale.
- La compétition dépend de la saison (`PLAYS_IN {season}`) : un club monte ou descend.
- Une prédiction n'est jamais stockée dans `SENDS_TO` ni dans `Rumour`.
