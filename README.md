# Football Scout Agent

Graphe Neo4j des transferts du football européen et agent LangGraph d'aide au recrutement
(projet GraphDB, AIDAMS 3A, niveau 3+). Chaque affirmation de l'agent est étiquetée **vérifiée**
(données Transfermarkt), **prédite** (modèle de link prediction) ou **rumeur** (extraite de la presse).

> Squelette de départ : chaque rôle documente sa partie dans `docs/`, et D assemble ce README le 22/10 au soir.

## Démarrage rapide

```bash
make env          # crée .env, puis remplir les valeurs
make install      # environnement virtuel + dépendances
make data         # télécharge le fichier DuckDB Transfermarkt
make docker-run   # proxy (80), api, neo4j (7474, 7687), ingest
```

Swagger : http://localhost/docs

## Équipe et responsabilités

| Rôle | Membre | Périmètre |
| --- | --- | --- |
| A | _à compléter_ | modélisation, ingestion, CI |
| B | _à compléter_ | API, Docker, proxy, performance |
| C | _à compléter_ | GDS, link prediction, notebook |
| D | _à compléter_ | agent LangGraph, garde-fous, rumeurs, README final |

## Sections à compléter

- Description et architecture (B : `docs/architecture.md` et diagramme)
- Schéma du graphe et choix de modélisation, appendice anti-généricité (A : `docs/schema.md`, `docs/modeling.md`)
- Exemples d'API et captures (B)
- Performance, `EXPLAIN` / `PROFILE` (B : `docs/perf.md`)
- GDS et ML (C : `docs/ml_report.md`, `demo.ipynb`)
- Agent, garde-fous et évaluation (D : `docs/guardrails.md`, `docs/agent_eval.md`)
- Sources des données (A : section dans `docs/modeling.md`)
- Contributions (D, à partir de `git shortlog -sn`)
