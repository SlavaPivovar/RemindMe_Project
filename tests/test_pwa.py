"""Ярлик на головному екрані: маніфест і іконки доступні без входу, решта — ні."""

from fastapi.testclient import TestClient

from tesla_ro.web import create_app


def test_manifest_and_icons_are_public(tmp_path):
    web = TestClient(create_app(orders_dir=tmp_path, password="secret-pass"), follow_redirects=False)
    m = web.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["name"] == "DK-TeslaService" and data["short_name"] == "DK-Tesla"
    assert data["display"] == "standalone" and data["start_url"] == "/"
    assert {i["sizes"] for i in data["icons"]} == {"192x192", "512x512"}
    assert any(i.get("purpose") == "maskable" for i in data["icons"])
    for icon in data["icons"]:
        r = web.get(icon["src"])
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert web.get("/icons/apple-touch-icon.png").status_code == 200
    assert web.get("/favicon.ico").content.startswith(b"\x89PNG")
    assert web.get("/icons/..%2F..%2Fweb.py").status_code == 404
    assert web.get("/icons/nope.png").status_code == 404
    # усе інше, як і раніше, лише після входу
    assert web.get("/").status_code == 303
    assert web.get("/api/state").status_code == 401
    login = web.get("/login").text
    assert 'rel="manifest"' in login and 'content="DK-Tesla"' in login and "apple-touch-icon" in login


def test_index_links_manifest(tmp_path):
    web = TestClient(create_app(orders_dir=tmp_path))
    page = web.get("/").text
    assert 'rel="manifest"' in page and 'name="theme-color"' in page
    assert web.get("/api/state").json()["short_name"] == "DK-Tesla"
