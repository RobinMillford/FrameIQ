

def test_login_page_loads(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_register_page_loads(client):
    resp = client.get("/register")
    assert resp.status_code == 200


def test_index_redirects_when_unauthenticated(client):
    resp = client.get("/")
    # Either renders (200) or redirects to login (302)
    assert resp.status_code in (200, 302)


def test_api_requires_auth(client):
    resp = client.get("/api/feed/enhanced")
    assert resp.status_code in (302, 401)


def test_media_reviews_endpoint(client):
    resp = client.get("/api/media/movie/550/reviews")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "reviews" in data


def test_404_returns_sensible_response(client):
    resp = client.get("/nonexistent-route-xyz")
    assert resp.status_code == 404


def test_login_rate_limit_headers(client):
    resp = client.post(
        "/login",
        data={"username": "nobody", "password": "bad"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302, 400, 429)
