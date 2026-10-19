"""API-key protection for sensitive endpoints.

Owner: B. Use `Depends(require_api_key)` on any endpoint that writes to the graph or spends LLM credit.
"""

import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    provided: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject the request unless the `X-API-Key` header matches `API_KEY`."""
    if not settings.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEY n'est pas configurée sur le serveur")
    if not provided or not secrets.compare_digest(provided, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clé d'API absente ou invalide (en-tête X-API-Key)")
