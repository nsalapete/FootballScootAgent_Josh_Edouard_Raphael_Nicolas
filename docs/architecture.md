# Architecture

Owner : B.

## Image Docker publiée

Chaque push sur `main` construit l'image de l'API et la publie sur GitHub Container Registry
(workflow `.github/workflows/publish.yml`, authentifié par le `GITHUB_TOKEN` du workflow, droit `packages: write`) :

- `ghcr.io/nsalapete/football-scout-api:latest` : dernier état de `main` ;
- `ghcr.io/nsalapete/football-scout-api:sha-<commit>` : une image par commit, pour revenir en arrière ;
- `ghcr.io/nsalapete/football-scout-api:v1.0` : l'image du tag de rendu.

Page du paquet : <https://github.com/users/nsalapete/packages/container/package/football-scout-api>

Lancer la stack avec l'image publiée au lieu de la reconstruire :

```bash
docker pull ghcr.io/nsalapete/football-scout-api:latest
API_IMAGE=ghcr.io/nsalapete/football-scout-api:latest docker compose up -d --no-build
```
