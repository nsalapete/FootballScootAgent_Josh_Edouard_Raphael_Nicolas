"""Tests of the demo interface served by the API (no browser, no database). Owner: D."""

import re
from pathlib import Path

from app.routers.ui import ASSETS, UI_DIR

APP_JS = UI_DIR / "app.js"


def test_root_redirects_to_the_interface(api_no_db):
    response = api_no_db.get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/ui"


def test_interface_and_its_assets_are_served(api_no_db):
    page = api_no_db.get("/ui")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    for name in ASSETS:
        assert f"/ui/{name}" in page.text, name
        asset = api_no_db.get(f"/ui/{name}")
        assert asset.status_code == 200 and asset.content, name


def test_only_whitelisted_files_are_reachable(api_no_db):
    for path in ("/ui/index.html", "/ui/..%2F..%2Fmain.py", "/ui/vendor", "/ui/secret.txt"):
        assert api_no_db.get(path).status_code == 404, path


def test_the_interface_is_not_in_the_api_schema(api_no_db):
    paths = api_no_db.get("/openapi.json").json()["paths"]
    assert "/ui" not in paths and "/" not in paths


def test_every_api_route_is_wired_into_the_interface(api_no_db):
    """Each public route of the API appears in app.js, and app.js calls no route that does not exist."""
    source = APP_JS.read_text(encoding="utf-8")
    spec = api_no_db.get("/openapi.json").json()["paths"]
    routes = {re.sub(r"\{[^}]+\}", "{}", path) for path in spec}
    called = {re.sub(r"\$\{[^}]+\}|\{[^}]+\}", "{}", match) for match in re.findall(r"/api/[A-Za-z0-9_/{}$.\-]+", source)}
    called = {path.rstrip("/") for path in called}
    missing = sorted(route for route in routes if not any(call == route or call.startswith(route) for call in called))
    assert not missing, f"routes not used by the interface: {missing}"
    unknown = sorted(call for call in called if not any(call == route or call.startswith(route) for route in routes))
    assert not unknown, f"interface calls unknown routes: {unknown}"


def test_no_api_data_reaches_inner_html():
    source = APP_JS.read_text(encoding="utf-8")
    assert "insertAdjacentHTML" not in source and "outerHTML" not in source
    for line in source.splitlines():
        if "innerHTML" in line:
            assert re.search(r"innerHTML\s*=\s*(''|\"\"|``)\s*;?", line), f"innerHTML with content: {line.strip()}"


def test_static_files_exist():
    for name, (filename, _) in ASSETS.items():
        assert (UI_DIR / filename).is_file(), name
    assert Path(UI_DIR / "index.html").is_file()
