import pytest

GUARDED_ROUTES = [
    # Router-level guards: /api/build, /api/feeds, /api/ai
    ("GET", "/api/build/links"),
    ("POST", "/api/build/links"),
    ("GET", "/api/feeds"),
    ("POST", "/api/feeds"),
    ("GET", "/api/ai/providers"),
    ("POST", "/api/ai/providers"),
    ("GET", "/api/ai/chats"),
    ("POST", "/api/ai/ask"),
    # Explicitly guarded routes in api/catalog.py
    ("POST", "/api/catalog/refresh"),
    ("POST", "/api/updates/nonexistent/start"),
    ("POST", "/api/updates/start-all"),
    # api/system.py
    ("GET", "/api/logs"),
    ("GET", "/api/logs/download"),
    ("POST", "/api/logs/clear"),
    ("POST", "/api/queues/builds/pause"),
    ("PUT", "/api/queues/builds/concurrency"),
    ("POST", "/api/queues/builds/jobs/999/cancel"),
    # api/downloads.py
    ("POST", "/api/downloads"),
    ("POST", "/api/downloads/clear-finished"),
    ("POST", "/api/downloads/999/pause"),
    ("DELETE", "/api/downloads/999"),
    # api/access.py
    ("GET", "/api/mcp/keys"),
    ("POST", "/api/mcp/keys"),
    ("DELETE", "/api/mcp/keys/999"),
    # api/network.py
    ("POST", "/api/network/change-ip"),
    ("POST", "/api/network/test"),
    # api/library.py
    ("PATCH", "/api/books/nonexistent"),
    ("DELETE", "/api/books/nonexistent"),
    ("POST", "/api/library/rescan"),
    ("GET", "/api/library/leftovers"),
    ("DELETE", "/api/search/history/999"),
    ("DELETE", "/api/search/history"),
    ("PUT", "/api/settings"),
    # api/organise.py
    ("POST", "/api/categories"),
    ("PUT", "/api/categories/nonexistent"),
    ("DELETE", "/api/categories/nonexistent"),
    ("POST", "/api/icons/link/999"),
    ("DELETE", "/api/icons/link/999"),
]


@pytest.mark.parametrize(("method", "path"), GUARDED_ROUTES)
def test_guarded_routes_require_admin_token(make_client, method: str, path: str):
    client = make_client(admin_token="top-secret-token")

    # Missing token -> 401
    r_no_auth = client.request(method, path)
    assert r_no_auth.status_code == 401

    # Wrong token -> 401
    r_wrong = client.request(method, path, headers={"Authorization": "Bearer wrong-token"})
    assert r_wrong.status_code == 401

    # Correct token passes auth (not 401 and not 500)
    r_ok = client.request(method, path, headers={"Authorization": "Bearer top-secret-token"})
    assert r_ok.status_code not in (401, 500)


def test_mcp_accepts_admin_token_and_rejects_wrong_token(make_client):
    client = make_client(admin_token="top-secret-token")

    assert client.post("/mcp", json={}).status_code == 401
    assert (
        client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer wrong-token"},
        ).status_code
        == 401
    )

    # Admin token unlocks /mcp (reaches JSON-RPC validation -> 400, not 401)
    assert (
        client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer top-secret-token"},
        ).status_code
        == 400
    )

    # MCP key also still works alongside admin_token
    key = client.post(
        "/api/mcp/keys",
        json={"name": "agent"},
        headers={"Authorization": "Bearer top-secret-token"},
    ).json()["created"]["key"]
    assert (
        client.post(
            "/mcp",
            json={},
            headers={"Authorization": f"Bearer {key}"},
        ).status_code
        == 400
    )
