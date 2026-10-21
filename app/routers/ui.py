"""Demo web interface, served by the API itself (same origin: no CORS, works behind nginx). Owner: D.

Only the files listed in `ASSETS` can be served: a request can never read another path of the image.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse

UI_DIR = Path(__file__).resolve().parents[1] / "static" / "ui"

# Public name -> (file under UI_DIR, media type). Nothing else is reachable.
ASSETS = {
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "styles.css": ("styles.css", "text/css; charset=utf-8"),
    "d3.v7.9.0.min.js": ("vendor/d3.v7.9.0.min.js", "text/javascript; charset=utf-8"),
}

router = APIRouter(tags=["ui"], include_in_schema=False)


@router.get("/")
def root() -> RedirectResponse:
    """The site root opens the demo interface."""
    return RedirectResponse("/ui", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/ui")
def interface() -> FileResponse:
    """The single-page demo interface."""
    return FileResponse(UI_DIR / "index.html", media_type="text/html; charset=utf-8")


@router.get("/ui/{asset}")
def ui_asset(asset: str) -> FileResponse:
    """A whitelisted script or stylesheet of the interface."""
    if asset not in ASSETS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fichier inconnu")
    filename, media_type = ASSETS[asset]
    return FileResponse(UI_DIR / filename, media_type=media_type)
