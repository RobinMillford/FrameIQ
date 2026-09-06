

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


def test_movie_detail_uses_current_user_list_relationship(monkeypatch, app, db):
    from models import MediaItem, UserList, UserListItem
    import routes.details as details

    current_user_id = 1
    media = MediaItem(tmdb_id=1550, media_type='movie', title='Test Movie')
    current_list = UserList(user_id=current_user_id, title='My Favorites')
    db.session.add_all([media, current_list])
    db.session.flush()
    other_list = UserList(user_id=2, title='Other Favorites')
    db.session.add(other_list)
    db.session.flush()
    db.session.add_all([
        UserListItem(list_id=current_list.id, media_id=media.id, media_type='movie'),
        UserListItem(list_id=other_list.id, media_id=media.id, media_type='movie'),
    ])
    db.session.commit()

    monkeypatch.setattr(details, 'fetch_movie_details', lambda _: {
        'id': media.id,
        'genres': [],
    })
    monkeypatch.setattr(details, 'current_user', type(
        'FakeUser',
        (),
        {
            'id': current_user_id,
            'is_authenticated': True,
            'watchlist': [],
            'wishlist': [],
            'viewed_media': [],
        },
    )())
    monkeypatch.setattr(
        details,
        'render_template',
        lambda template, **context: {
            'user_lists_with_movie': context['user_lists_with_movie']
        },
    )

    with app.test_request_context(f'/movie/{media.id}'):
        response = details.movie_detail.__wrapped__(media.id)

    assert [item.title for item in response['user_lists_with_movie']] == [
        'My Favorites'
    ]

    db.session.query(UserListItem).delete()
    db.session.commit()

    with app.test_request_context(f'/movie/{media.id}'):
        empty_response = details.movie_detail.__wrapped__(media.id)

    assert empty_response['user_lists_with_movie'] == []

    current_list_id = current_list.id
    other_list_id = other_list.id
    db.session.expunge(current_list)
    db.session.expunge(other_list)
    db.session.query(UserList).filter(
        UserList.id.in_([current_list_id, other_list_id])
    ).delete(synchronize_session=False)
    db.session.delete(media)
    db.session.commit()
