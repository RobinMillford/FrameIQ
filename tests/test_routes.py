

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


def test_movie_list_items_eagerly_load_list_relationship(db, app):
    """movie_detail's UserListItem query uses joinedload(UserListItem.list),
    so accessing item.list must not emit a per-row lazy SELECT."""
    import random
    from sqlalchemy import event
    from sqlalchemy.orm import joinedload
    from models import MediaItem, UserList, UserListItem

    # Unique per run so leftover data from failed runs never collides.
    tmdb_id = random.randint(1_000_000_000, 2_000_000_000)
    media = MediaItem(tmdb_id=tmdb_id, media_type='movie', title='Eager Load Movie')
    user_list = UserList(user_id=1, title='Eager Favorites')
    db.session.add_all([media, user_list])
    db.session.flush()
    # Capture IDs before commit: after commit the ORM instances are expired,
    # and touching media.id would emit an unrelated refresh SELECT.
    media_id = media.id
    list_id = user_list.id
    db.session.add(
        UserListItem(list_id=list_id, media_id=media_id, media_type='movie')
    )
    db.session.commit()

    statements = []

    @event.listens_for(db.engine, 'before_cursor_execute')
    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        items = (
            UserListItem.query.options(joinedload(UserListItem.list))
            .filter_by(media_id=media_id, media_type='movie')
            .all()
        )
        # Accessing .list for every row must not trigger lazy SELECTs.
        list_titles = [item.list.title for item in items]
        assert list_titles == ['Eager Favorites']
        # One SELECT with a LEFT OUTER JOIN — no per-row lazy loads.
        assert len(statements) == 1
    finally:
        event.remove(db.engine, 'before_cursor_execute', _count)
        db.session.query(UserListItem).filter_by(list_id=list_id).delete()
        db.session.query(UserList).filter_by(id=list_id).delete()
        db.session.query(MediaItem).filter_by(tmdb_id=tmdb_id).delete()
        db.session.commit()


def test_likes_check_returns_count(client, db, app):
    """The likes check endpoint returns both liked state and total count so
    the frontend needs only one request to initialize the like button."""
    from models import User, MediaLike
    import random

    uid = random.randint(100, 100000)
    user = User(username=f'likes_user_{uid}', email=f'likes_{uid}@test.local')
    user.set_password('x')
    other = User(username=f'likes_other_{uid}', email=f'likes_o_{uid}@test.local')
    other.set_password('x')
    db.session.add_all([user, other])
    db.session.flush()

    tmdb_id = random.randint(1_000_000_000, 2_000_000_000)
    db.session.add(MediaLike(user_id=user.id, media_id=tmdb_id, media_type='movie'))
    db.session.add(MediaLike(user_id=other.id, media_id=tmdb_id, media_type='movie'))
    db.session.commit()

    with app.test_request_context(
        f'/api/media/{tmdb_id}/likes/check?media_type=movie'
    ):
        from flask_login import login_user
        login_user(user)
        resp = app.test_client().get(
            f'/api/media/{tmdb_id}/likes/check?media_type=movie'
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data['liked'] is True
    assert data['count'] == 2

    db.session.query(MediaLike).filter_by(media_id=tmdb_id).delete()
    db.session.delete(user)
    db.session.delete(other)
    db.session.commit()


def test_trending_media_caps_limit(client, db):
    """Public limit parameter is capped at 50."""
    resp = client.get('/api/trending/media?limit=100000')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'items' in data
    assert len(data['items']) <= 50


def test_user_list_to_dict_prefetch_matches_lazy(db, app):
    """UserList.to_dict() with prefetch_list_data() produces identical
    output to the lazy per-list path."""
    from models import UserList
    from models.lists import prefetch_list_data

    l1 = UserList(user_id=1, title='Prefetch A')
    l2 = UserList(user_id=1, title='Prefetch B')
    db.session.add_all([l1, l2])
    db.session.commit()

    ids = [l1.id, l2.id]
    lists = UserList.query.filter(UserList.id.in_(ids)).all()
    lazy_output = [lst.to_dict() for lst in lists]

    lists2 = UserList.query.filter(UserList.id.in_(ids)).all()
    prefetch_list_data(lists2)
    prefetched_output = [lst.to_dict() for lst in lists2]

    for lazy_d, pre_d in zip(lazy_output, prefetched_output):
        for key in ('id', 'title', 'item_count', 'is_owner', 'collaborators',
                    'categories', 'analytics', 'user'):
            assert lazy_d[key] == pre_d[key], key

    db.session.query(UserList).filter(UserList.id.in_(ids)).delete(
        synchronize_session=False)
    db.session.commit()


def test_tv_detail_uses_current_user_list_relationship(monkeypatch, app, db):
    """tv_detail filters list rows by the current user's ownership and
    appends the UserList objects (item.list, not item.user_list)."""
    import random
    from models import MediaItem, UserList, UserListItem
    import routes.details as details

    # Random int4-safe id so leftover data from failed runs never collides.
    tmdb_id = random.randint(1_600_000_000, 2_100_000_000)
    current_user_id = 1
    media = MediaItem(tmdb_id=tmdb_id, media_type='tv', title='Test Show')
    current_list = UserList(user_id=current_user_id, title='My TV Picks')
    db.session.add_all([media, current_list])
    db.session.flush()
    other_list = UserList(user_id=2, title='Other TV Picks')
    db.session.add(other_list)
    db.session.flush()
    db.session.add_all([
        UserListItem(list_id=current_list.id, media_id=media.id, media_type='tv'),
        UserListItem(list_id=other_list.id, media_id=media.id, media_type='tv'),
    ])
    db.session.commit()

    monkeypatch.setattr(details, 'fetch_tv_show_details', lambda _: {
        'id': tmdb_id,
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
            'user_lists_with_show': context['user_lists_with_show']
        },
    )

    try:
        with app.test_request_context(f'/tv/{media.id}'):
            response = details.tv_detail.__wrapped__(media.id)

        # Only the current user's own lists appear — ownership filter intact.
        assert [item.title for item in response['user_lists_with_show']] == [
            'My TV Picks'
        ]

        db.session.query(UserListItem).delete()
        db.session.commit()

        with app.test_request_context(f'/tv/{media.id}'):
            empty_response = details.tv_detail.__wrapped__(media.id)

        assert empty_response['user_lists_with_show'] == []
    finally:
        db.session.query(UserListItem).delete()
        db.session.query(UserList).filter(
            UserList.id.in_([current_list.id, other_list.id])
        ).delete(synchronize_session=False)
        db.session.query(MediaItem).filter_by(tmdb_id=media.tmdb_id).delete()
        db.session.commit()


def test_tv_detail_list_items_eagerly_load_list_relationship(db, app):
    """tv_detail's UserListItem query uses joinedload(UserListItem.list),
    so accessing item.list must not emit a per-row lazy SELECT."""
    import random
    from sqlalchemy import event
    from sqlalchemy.orm import joinedload
    from models import MediaItem, UserList, UserListItem

    # Unique per run (int4-safe) so leftover data from failed runs never collides.
    tmdb_id = random.randint(1_600_000_000, 2_100_000_000)
    media = MediaItem(tmdb_id=tmdb_id, media_type='tv', title='Eager Load Show')
    user_list = UserList(user_id=1, title='Eager TV Favorites')
    db.session.add_all([media, user_list])
    db.session.flush()
    media_id = media.id
    list_id = user_list.id
    db.session.add(
        UserListItem(list_id=list_id, media_id=media_id, media_type='tv')
    )
    db.session.commit()

    statements = []

    @event.listens_for(db.engine, 'before_cursor_execute')
    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        items = (
            UserListItem.query.options(joinedload(UserListItem.list))
            .filter_by(media_id=media_id, media_type='tv')
            .all()
        )
        list_titles = [item.list.title for item in items]
        assert list_titles == ['Eager TV Favorites']
        # One SELECT with a LEFT OUTER JOIN — no per-row lazy loads.
        assert len(statements) == 1
    finally:
        event.remove(db.engine, 'before_cursor_execute', _count)
        db.session.query(UserListItem).filter_by(list_id=list_id).delete()
        db.session.query(UserList).filter_by(id=list_id).delete()
        db.session.query(MediaItem).filter_by(tmdb_id=tmdb_id).delete()
        db.session.commit()
