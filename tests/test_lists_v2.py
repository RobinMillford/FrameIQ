"""Lists V2 (Feature 9) — focused suite.

Covers: position ordering semantics, ranked/unranked mode, safe two-phase
reorder (incl. cross-list rejection and viewer blocking), cloning, likes,
comments, bulk remove/move, batch watched-state, discovery sorts, and the
security invariants from Phase 18.

Runs on the session temp-file SQLite (conftest, no per-test rollback), so
users and slugs use uuid-suffixed names (the test_statistics.py pattern) to
stay collision-free on the shared database. Media tmdb_ids use a module-local
counter in the 9_650_000 range. Auth uses the conftest auth_client pattern;
CSRF is disabled by conftest.
"""
import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers (module-local, deterministic IDs)
# ---------------------------------------------------------------------------

def _media(db, title, media_type='movie', tmdb_id=None, runtime=100):
    from models import MediaItem
    if tmdb_id is None:
        if not hasattr(_media, '_next_id'):
            _media._next_id = 9_650_000
        tmdb_id = _media._next_id
        _media._next_id += 1
    m = MediaItem(tmdb_id=tmdb_id, media_type=media_type, title=title,
                  runtime=runtime)
    db.session.add(m)
    db.session.flush()
    return m


def _user(db, username):
    """Create a user with a uuid-suffixed login (shared session DB)."""
    from models import User
    username = f"{username}_{uuid.uuid4().hex[:8]}"
    u = User(username=username, email=f'{username}@lists-v2.test',
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


def _list(db, user_id, title, slug=None, is_public=True, list_type=None):
    """Create a list; slug defaults to a uuid suffix (slug is unique)."""
    from models import UserList
    if slug is None:
        slug = f"v2-{uuid.uuid4().hex[:10]}"
    ul = UserList(user_id=user_id, title=title, slug=slug,
                  is_public=is_public)
    if list_type is not None:
        ul.list_type = list_type
    db.session.add(ul)
    db.session.flush()
    return ul


def _item(db, lst, media, position=None, note=None):
    from models import UserListItem
    if position is None:
        from routes.lists import _next_position
        position = _next_position(lst.id)
    li = UserListItem(list_id=lst.id, media_id=media.id,
                      media_type='tv' if media.media_type == 'tv' else 'movie',
                      position=position, note=note)
    db.session.add(li)
    db.session.flush()
    return li


def _login(client, username):
    client.post('/login', data={'username': username, 'password': 'TestPass1'},
                follow_redirects=True)


# ---------------------------------------------------------------------------
# MODEL
# ---------------------------------------------------------------------------

class TestModel:
    def test_position_exists_and_list_type_defaults(self, app, db):
        from models import UserList, UserListItem
        u = _user(db, 'v2_model_u1')
        lst = _list(db, u.id, 'Model Defaults', slug=f'v2-{uuid.uuid4().hex[:8]}')
        assert lst.list_type == UserList.TYPE_UNRANKED  # V2 default
        col = UserListItem.__table__.columns['position']
        assert not col.nullable, 'position must be NOT NULL'

    def test_position_assigned_on_add(self, app, db, auth_client):
        u = _user(db, 'v2_model_u2')
        lst = _list(db, u.id, 'Positions', slug=f'v2-{uuid.uuid4().hex[:8]}')
        m1 = _media(db, 'A')
        m2 = _media(db, 'B')
        i1 = _item(db, lst, m1)
        i2 = _item(db, lst, m2)
        assert (i1.position, i2.position) == (1, 2)

    def test_unique_list_media_constraint(self, app, db):
        from models import UserListItem
        from sqlalchemy.exc import IntegrityError
        u = _user(db, 'v2_model_u3')
        lst = _list(db, u.id, 'Unique', slug=f'v2-{uuid.uuid4().hex[:8]}')
        m = _media(db, 'Dup')
        _item(db, lst, m)
        dup = UserListItem(list_id=lst.id, media_id=m.id, media_type='movie',
                           position=99)
        db.session.add(dup)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()

    def test_list_like_unique_per_user_per_list(self, app, db):
        from models import ListLike
        from sqlalchemy.exc import IntegrityError
        u = _user(db, 'v2_model_u4')
        lst = _list(db, u.id, 'Liked', slug=f'v2-{uuid.uuid4().hex[:8]}')
        db.session.add(ListLike(user_id=u.id, list_id=lst.id))
        db.session.commit()
        db.session.add(ListLike(user_id=u.id, list_id=lst.id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ---------------------------------------------------------------------------
# ORDERING + REORDER API
# ---------------------------------------------------------------------------

class TestReorder:
    def _setup(self, db, n=3):
        u = _user(db, 'v2_reorder_u')
        lst = _list(db, u.id, 'Reorder Me', slug=f'v2-{uuid.uuid4().hex[:8]}')
        items = []
        for i in range(n):
            items.append(_item(db, lst, _media(db, f'M{i}')))
        return u, lst, items

    def test_reorder_success_and_positions_normalized(self, auth_client, db):
        u, lst, items = self._setup(db)
        _login(auth_client, u.username)
        ids = [i.id for i in items]
        r = auth_client.put(f'/api/lists/{lst.id}/reorder',
                            json={'item_order': [ids[2], ids[0], ids[1]]})
        assert r.status_code == 200
        from models import UserListItem
        positions = {row.id: row.position for row in
                     UserListItem.query.filter_by(list_id=lst.id).all()}
        assert positions == {ids[2]: 1, ids[0]: 2, ids[1]: 3}

    def test_reorder_accepts_numeric_strings_legacy(self, auth_client, db):
        u, lst, items = self._setup(db)
        _login(auth_client, u.username)
        ids = [str(i.id) for i in items][::-1]
        r = auth_client.put(f'/api/lists/{lst.id}/reorder',
                            json={'item_order': ids})
        assert r.status_code == 200
        from models import UserListItem
        positions = {row.id: row.position for row in
                     UserListItem.query.filter_by(list_id=lst.id).all()}
        assert [positions[i.id] for i in reversed(items)] == [1, 2, 3]

    def test_reorder_rejects_cross_list_items(self, auth_client, db):
        u, lst, items = self._setup(db)
        other_u = _user(db, 'v2_reorder_other')
        other = _list(db, other_u.id, 'Other', slug=f'v2-{uuid.uuid4().hex[:8]}')
        foreign = _item(db, other, _media(db, 'Foreign'))
        _login(auth_client, u.username)
        r = auth_client.put(f'/api/lists/{lst.id}/reorder',
                            json={'item_order': [i.id for i in items] + [foreign.id]})
        assert r.status_code == 400
        assert 'belong' in r.get_json()['error']

    def test_reorder_rejects_partial_order(self, auth_client, db):
        u, lst, items = self._setup(db, n=3)
        _login(auth_client, u.username)
        r = auth_client.put(f'/api/lists/{lst.id}/reorder',
                            json={'item_order': [items[0].id]})
        assert r.status_code == 400

    def test_viewer_cannot_reorder(self, auth_client, db):
        owner, lst, items = self._setup(db)
        viewer = _user(db, 'v2_reorder_viewer')
        from models import ListCollaborator
        db.session.add(ListCollaborator(list_id=lst.id, user_id=viewer.id,
                                        role='viewer'))
        db.session.commit()
        _login(auth_client, viewer.username)
        r = auth_client.put(f'/api/lists/{lst.id}/reorder',
                            json={'item_order': [i.id for i in items]})
        assert r.status_code == 403

    def test_editor_can_reorder(self, auth_client, db):
        owner, lst, items = self._setup(db)
        editor = _user(db, 'v2_reorder_editor')
        from models import ListCollaborator
        db.session.add(ListCollaborator(list_id=lst.id, user_id=editor.id,
                                        role='editor'))
        db.session.commit()
        _login(auth_client, editor.username)
        ids = [i.id for i in items]
        r = auth_client.put(f'/api/lists/{lst.id}/reorder',
                            json={'item_order': ids[::-1]})
        assert r.status_code == 200

    def test_items_shape_partial_positions(self, auth_client, db):
        u, lst, items = self._setup(db, n=3)
        _login(auth_client, u.username)
        r = auth_client.put(f'/api/lists/{lst.id}/reorder',
                            json={'items': [{'item_id': items[2].id, 'position': 1}]})
        assert r.status_code == 200
        from models import UserListItem
        positions = {row.id: row.position for row in
                     UserListItem.query.filter_by(list_id=lst.id).all()}
        # Sparse payload: moved item first, others keep canonical (position,id) order.
        assert positions[items[2].id] == 1
        assert sorted(positions.values()) == [1, 2, 3]

    def test_reorder_requires_auth(self, client, db):
        u, lst, items = self._setup(db)
        r = client.put(f'/api/lists/{lst.id}/reorder',
                       json={'item_order': [i.id for i in items]})
        assert r.status_code in (302, 401)


# ---------------------------------------------------------------------------
# RANKED / UNRANKED MODE
# ---------------------------------------------------------------------------

class TestMode:
    def test_mode_switch_preserves_order(self, auth_client, db):
        u = _user(db, 'v2_mode_u')
        lst = _list(db, u.id, 'Mode', slug=f'v2-{uuid.uuid4().hex[:8]}')
        items = [_item(db, lst, _media(db, f'X{i}')) for i in range(3)]
        _login(auth_client, u.username)

        r = auth_client.put(f'/api/lists/{lst.id}/mode',
                            json={'list_type': 'ranked'})
        assert r.status_code == 200
        assert r.get_json()['list_type'] == 'ranked'

        # Positions untouched by the mode switch.
        from models import UserListItem
        positions = {row.id: row.position for row in
                     UserListItem.query.filter_by(list_id=lst.id).all()}
        assert [positions[i.id] for i in items] == [1, 2, 3]

        r = auth_client.put(f'/api/lists/{lst.id}/mode',
                            json={'list_type': 'unranked'})
        assert r.status_code == 200

    def test_mode_invalid_value(self, auth_client, db):
        u = _user(db, 'v2_mode_u2')
        lst = _list(db, u.id, 'Mode2', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _login(auth_client, u.username)
        r = auth_client.put(f'/api/lists/{lst.id}/mode',
                            json={'list_type': 'best'})
        assert r.status_code == 400

    def test_mode_requires_owner(self, auth_client, db):
        owner = _user(db, 'v2_mode_owner')
        lst = _list(db, owner.id, 'Mode3', slug=f'v2-{uuid.uuid4().hex[:8]}')
        intruder = _user(db, 'v2_mode_intruder')
        _login(auth_client, intruder.username)
        r = auth_client.put(f'/api/lists/{lst.id}/mode',
                            json={'list_type': 'ranked'})
        assert r.status_code == 403

    def test_create_list_with_ranked_mode(self, client, db):
        u = _user(db, 'v2_create_ranked')
        _login(client, u.username)
        r = client.post('/api/lists/create',
                        json={'title': 'Ranked新建', 'list_type': 'ranked'})
        assert r.status_code == 201
        assert r.get_json()['list']['list_type'] == 'ranked'

    def test_create_list_invalid_mode(self, client, db):
        u = _user(db, 'v2_create_bad')
        _login(client, u.username)
        r = client.post('/api/lists/create',
                        json={'title': 'Bad', 'list_type': 'mega'})
        assert r.status_code == 400

    def test_detail_serializes_list_type_and_ranks(self, auth_client, db):
        u = _user(db, 'v2_mode_u3')
        lst = _list(db, u.id, 'Ranks', slug=f'v2-{uuid.uuid4().hex[:8]}', list_type='ranked')
        for i in range(3):
            _item(db, lst, _media(db, f'R{i}'))
        r = auth_client.get(f'/api/lists/{lst.id}')
        assert r.status_code == 200
        data = r.get_json()
        assert data['list_type'] == 'ranked'
        assert [i['position'] for i in data['items']] == [1, 2, 3]

    def test_unranked_serializes_without_rank_presentation(self, auth_client, db):
        u = _user(db, 'v2_mode_u4')
        lst = _list(db, u.id, 'NoRanks', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _item(db, lst, _media(db, 'NR'))
        r = auth_client.get(f'/api/lists/{lst.id}')
        data = r.get_json()
        assert data['list_type'] == 'unranked'
        # unranked presentation is a UI concern; API still carries position.
        assert data['items'][0]['position'] == 1


# ---------------------------------------------------------------------------
# CLONING
# ---------------------------------------------------------------------------

class TestClone:
    def test_clone_creates_private_copy(self, auth_client, db):
        owner = _user(db, 'v2_clone_owner')
        src = _list(db, owner.id, 'Clone Me', slug=f'v2-{uuid.uuid4().hex[:8]}',
                    list_type='ranked')
        items = []
        for i in range(3):
            items.append(_item(db, src, _media(db, f'C{i}'), position=i + 1,
                               note=f'note {i}'))
        cloner = _user(db, 'v2_clone_user')
        _login(auth_client, cloner.username)

        r = auth_client.post(f'/api/lists/{src.id}/clone')
        assert r.status_code == 201
        data = r.get_json()
        assert data['list_id'] != src.id
        assert data['list_type'] == 'ranked'

        from models import UserList, UserListItem
        clone = db.session.get(UserList, data['list_id'])
        assert clone.user_id == cloner.id
        assert clone.is_public is False
        assert clone.slug != src.slug

        clone_items = (UserListItem.query.filter_by(list_id=clone.id)
                       .order_by(UserListItem.position).all())
        assert [i.position for i in clone_items] == [1, 2, 3]
        assert [i.note for i in clone_items] == ['note 0', 'note 1', 'note 2']

        # Original untouched
        src_items = (UserListItem.query.filter_by(list_id=src.id)
                     .order_by(UserListItem.position).all())
        assert [i.id for i in src_items] == [i.id for i in items]

    def test_clone_does_not_copy_social(self, auth_client, db):
        from models import ListLike, ListComment, ListAnalytics, ListView
        owner = _user(db, 'v2_clone_owner2')
        src = _list(db, owner.id, 'Social', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _item(db, src, _media(db, 'S1'))
        liker = _user(db, 'v2_clone_liker')
        db.session.add_all([
            ListLike(user_id=liker.id, list_id=src.id),
            ListComment(user_id=liker.id, list_id=src.id, content='hi'),
            ListAnalytics(list_id=src.id, view_count=42, fork_count=0),
            ListView(list_id=src.id),
        ])
        db.session.commit()

        cloner = _user(db, 'v2_clone_user2')
        _login(auth_client, cloner.username)
        r = auth_client.post(f'/api/lists/{src.id}/clone')
        assert r.status_code == 201
        from models import UserList
        clone = db.session.get(UserList, r.get_json()['list_id'])
        assert clone.likes.count() == 0
        assert clone.comments.count() == 0
        assert clone.analytics is None
        assert clone.views.count() == 0

        # fork_count incremented on the source
        assert ListAnalytics.query.filter_by(list_id=src.id).first().fork_count == 1

    def test_clone_private_list_blocked_for_others(self, auth_client, db):
        owner = _user(db, 'v2_clone_owner3')
        src = _list(db, owner.id, 'Secret', slug=f'v2-{uuid.uuid4().hex[:8]}',
                    is_public=False)
        intruder = _user(db, 'v2_clone_user3')
        _login(auth_client, intruder.username)
        r = auth_client.post(f'/api/lists/{src.id}/clone')
        assert r.status_code == 403

    def test_clone_owner_can_clone_own_private(self, auth_client, db):
        u = _user(db, 'v2_clone_owner4')
        src = _list(db, u.id, 'Mine', slug=f'v2-{uuid.uuid4().hex[:8]}', is_public=False)
        _item(db, src, _media(db, 'M9'))
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{src.id}/clone')
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# LIKES
# ---------------------------------------------------------------------------

class TestLikes:
    def _pub_list(self, db):
        u = _user(db, 'v2_like_owner')
        return _list(db, u.id, 'Likable', slug=f'v2-{uuid.uuid4().hex[:8]}')

    def test_like_unlike_cycle(self, auth_client, db):
        lst = self._pub_list(db)
        liker = _user(db, 'v2_like_user')
        _login(auth_client, liker.username)

        r = auth_client.post(f'/api/lists/{lst.id}/like')
        assert r.status_code == 200
        assert r.get_json() == {'liked': True, 'like_count': 1}

        # duplicate like is a no-op, count stays correct
        r = auth_client.post(f'/api/lists/{lst.id}/like')
        assert r.get_json()['like_count'] == 1

        r = auth_client.delete(f'/api/lists/{lst.id}/like')
        assert r.get_json() == {'liked': False, 'like_count': 0}

        # unlike again stays clean
        r = auth_client.delete(f'/api/lists/{lst.id}/like')
        assert r.get_json()['like_count'] == 0

    def test_like_status_endpoint(self, auth_client, db):
        lst = self._pub_list(db)
        liker = _user(db, 'v2_like_user2')
        _login(auth_client, liker.username)
        r = auth_client.get(f'/api/lists/{lst.id}/like/status')
        assert r.get_json() == {'liked': False, 'like_count': 0}
        auth_client.post(f'/api/lists/{lst.id}/like')
        r = auth_client.get(f'/api/lists/{lst.id}/like/status')
        assert r.get_json() == {'liked': True, 'like_count': 1}

    def test_private_list_cannot_be_liked(self, auth_client, db):
        owner = _user(db, 'v2_like_priv_owner')
        lst = _list(db, owner.id, 'Priv', slug=f'v2-{uuid.uuid4().hex[:8]}',
                    is_public=False)
        intruder = _user(db, 'v2_like_priv_user')
        _login(auth_client, intruder.username)
        r = auth_client.post(f'/api/lists/{lst.id}/like')
        assert r.status_code == 403

    def test_like_requires_auth(self, client, db):
        lst = self._pub_list(db)
        r = client.post(f'/api/lists/{lst.id}/like')
        assert r.status_code in (302, 401)

    def test_owner_sees_own_list_like_state(self, auth_client, db):
        u = _user(db, 'v2_like_owner2')
        lst = _list(db, u.id, 'OwnLike', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{lst.id}/like')
        assert r.status_code == 200
        r = auth_client.get(f'/api/lists/{lst.id}')
        assert r.get_json()['liked_by_me'] is True


# ---------------------------------------------------------------------------
# COMMENTS
# ---------------------------------------------------------------------------

class TestComments:
    def _pub_list(self, db):
        u = _user(db, 'v2_cmt_owner')
        return _list(db, u.id, 'Discussed', slug=f'v2-{uuid.uuid4().hex[:8]}')

    def test_post_list_get_delete(self, auth_client, db):
        lst = self._pub_list(db)
        commenter = _user(db, 'v2_cmt_user')
        _login(auth_client, commenter.username)

        r = auth_client.post(f'/api/lists/{lst.id}/comments',
                             json={'content': '  Great list!  '})
        assert r.status_code == 201
        body = r.get_json()['comment']
        assert body['content'] == 'Great list!'  # stripped
        assert body['user']['username'] == commenter.username

        r = auth_client.get(f'/api/lists/{lst.id}/comments')
        comments = r.get_json()['comments']
        assert len(comments) == 1

        # author can delete own
        cid = comments[0]['id']
        r = auth_client.delete(f'/api/lists/{lst.id}/comments/{cid}')
        assert r.status_code == 200
        r = auth_client.get(f'/api/lists/{lst.id}/comments')
        assert r.get_json()['comments'] == []

    def test_empty_comment_rejected(self, auth_client, db):
        lst = self._pub_list(db)
        _user(db, 'v2_cmt_user2')
        _login(auth_client, 'v2_cmt_user2')
        r = auth_client.post(f'/api/lists/{lst.id}/comments',
                             json={'content': '   '})
        assert r.status_code == 400

    def test_oversized_comment_rejected(self, auth_client, db):
        from routes.lists import LIST_COMMENT_MAX_LEN
        lst = self._pub_list(db)
        _user(db, 'v2_cmt_user3')
        _login(auth_client, 'v2_cmt_user3')
        r = auth_client.post(f'/api/lists/{lst.id}/comments',
                             json={'content': 'x' * (LIST_COMMENT_MAX_LEN + 1)})
        assert r.status_code == 400

    def test_non_author_cannot_delete(self, auth_client, db):
        lst = self._pub_list(db)
        a = _user(db, 'v2_cmt_author')
        _login(auth_client, a.username)
        auth_client.post(f'/api/lists/{lst.id}/comments', json={'content': 'hi'})
        cid = auth_client.get(f'/api/lists/{lst.id}/comments').get_json()['comments'][0]['id']

        b = _user(db, 'v2_cmt_other')
        _login(auth_client, b.username)
        r = auth_client.delete(f'/api/lists/{lst.id}/comments/{cid}')
        assert r.status_code == 403

    def test_owner_can_moderate(self, auth_client, db):
        owner = _user(db, 'v2_cmt_owner2')
        lst = _list(db, owner.id, 'Moderated', slug=f'v2-{uuid.uuid4().hex[:8]}')
        a = _user(db, 'v2_cmt_mod_user')
        _login(auth_client, a.username)
        auth_client.post(f'/api/lists/{lst.id}/comments', json={'content': 'spam'})
        cid = auth_client.get(f'/api/lists/{lst.id}/comments').get_json()['comments'][0]['id']

        _login(auth_client, owner.username)
        r = auth_client.delete(f'/api/lists/{lst.id}/comments/{cid}')
        assert r.status_code == 200

    def test_private_list_comments_blocked(self, auth_client, db):
        owner = _user(db, 'v2_cmt_priv_owner')
        lst = _list(db, owner.id, 'PrivCmts', slug=f'v2-{uuid.uuid4().hex[:8]}',
                    is_public=False)
        intruder = _user(db, 'v2_cmt_priv_user')
        _login(auth_client, intruder.username)
        assert auth_client.post(f'/api/lists/{lst.id}/comments',
                                json={'content': 'x'}).status_code == 403
        assert auth_client.get(f'/api/lists/{lst.id}/comments').status_code == 403

    def test_pagination(self, auth_client, db):
        from routes.lists import COMMENTS_PER_PAGE
        lst = self._pub_list(db)
        for i in range(COMMENTS_PER_PAGE + 3):
            c = _user(db, f'v2_cmt_page_{i}')
            _login(auth_client, c.username)
            auth_client.post(f'/api/lists/{lst.id}/comments',
                             json={'content': f'c{i}'})
        r = auth_client.get(f'/api/lists/{lst.id}/comments?page=1')
        data = r.get_json()
        assert len(data['comments']) == COMMENTS_PER_PAGE
        assert data['pages'] == 2
        r2 = auth_client.get(f'/api/lists/{lst.id}/comments?page=2')
        assert len(r2.get_json()['comments']) == 3


# ---------------------------------------------------------------------------
# BULK OPERATIONS
# ---------------------------------------------------------------------------

class TestBulk:
    def _setup(self, db, n=3):
        u = _user(db, 'v2_bulk_u')
        lst = _list(db, u.id, 'Bulk', slug=f'v2-{uuid.uuid4().hex[:8]}')
        items = [_item(db, lst, _media(db, f'B{i}')) for i in range(n)]
        return u, lst, items

    def test_bulk_remove(self, auth_client, db):
        u, lst, items = self._setup(db)
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{lst.id}/items/bulk',
                             json={'action': 'remove',
                                   'item_ids': [items[0].id, items[2].id]})
        assert r.status_code == 200
        from models import UserListItem
        remaining = UserListItem.query.filter_by(list_id=lst.id).all()
        assert [i.id for i in remaining] == [items[1].id]
        # positions renumbered gapless
        assert remaining[0].position == 1

    def test_bulk_move(self, auth_client, db):
        u, lst, items = self._setup(db)
        target = _list(db, u.id, 'Target', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{lst.id}/items/bulk',
                             json={'action': 'move',
                                   'item_ids': [items[0].id],
                                   'target_list_id': target.id})
        assert r.status_code == 200
        from models import UserListItem
        assert UserListItem.query.get(items[0].id).list_id == target.id

    def test_bulk_move_dedupes_existing(self, auth_client, db):
        u, lst, items = self._setup(db)
        target = _list(db, u.id, 'Target2', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _item(db, target, items[0].media)  # same media already there
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{lst.id}/items/bulk',
                             json={'action': 'move',
                                   'item_ids': [items[0].id],
                                   'target_list_id': target.id})
        assert r.status_code == 200
        from models import UserListItem
        assert UserListItem.query.get(items[0].id) is None  # dropped from source

    def test_bulk_move_requires_owned_target(self, auth_client, db):
        u, lst, items = self._setup(db)
        other = _user(db, 'v2_bulk_other')
        foreign = _list(db, other.id, 'Foreign List', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{lst.id}/items/bulk',
                             json={'action': 'move',
                                   'item_ids': [items[0].id],
                                   'target_list_id': foreign.id})
        assert r.status_code == 403

    def test_bulk_rejects_cross_list_ids(self, auth_client, db):
        u, lst, items = self._setup(db)
        other = _user(db, 'v2_bulk_other2')
        other_lst = _list(db, other.id, 'OtherL', slug=f'v2-{uuid.uuid4().hex[:8]}')
        foreign_item = _item(db, other_lst, _media(db, 'FX'))
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{lst.id}/items/bulk',
                             json={'action': 'remove',
                                   'item_ids': [items[0].id, foreign_item.id]})
        assert r.status_code == 400

    def test_bulk_invalid_action(self, auth_client, db):
        u, lst, items = self._setup(db)
        _login(auth_client, u.username)
        r = auth_client.post(f'/api/lists/{lst.id}/items/bulk',
                             json={'action': 'explode',
                                   'item_ids': [items[0].id]})
        assert r.status_code == 400

    def test_viewer_cannot_bulk(self, auth_client, db):
        u, lst, items = self._setup(db)
        viewer = _user(db, 'v2_bulk_viewer')
        from models import ListCollaborator
        db.session.add(ListCollaborator(list_id=lst.id, user_id=viewer.id,
                                        role='viewer'))
        db.session.commit()
        _login(auth_client, viewer.username)
        r = auth_client.post(f'/api/lists/{lst.id}/items/bulk',
                             json={'action': 'remove',
                                   'item_ids': [items[0].id]})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# WATCHED STATE (batch)
# ---------------------------------------------------------------------------

class TestWatchedState:
    def test_movie_watched_states_batched(self, app, db, auth_client):
        from models import user_viewed, user_watchlist
        u = _user(db, 'v2_watch_u')
        lst = _list(db, u.id, 'Watched', slug=f'v2-{uuid.uuid4().hex[:8]}')
        m_viewed = _media(db, 'Seen')
        m_watching = _media(db, 'Progress')
        m_new = _media(db, 'Fresh')
        i1 = _item(db, lst, m_viewed)
        i2 = _item(db, lst, m_watching)
        i3 = _item(db, lst, m_new)

        # Viewed + watchlist rows are keyed by INTERNAL media id.
        db.session.execute(user_viewed.insert().values(
            user_id=u.id, media_id=m_viewed.id, media_type='movie'))
        db.session.execute(user_watchlist.insert().values(
            user_id=u.id, media_id=m_watching.id, media_type='movie'))
        db.session.commit()

        _login(auth_client, u.username)
        r = auth_client.get(f'/api/lists/{lst.id}/watched-state')
        assert r.status_code == 200
        data = r.get_json()
        states = {i['id']: i['watched'] for i in data['items']}
        assert states == {i1.id: 'watched', i2.id: 'watching',
                          i3.id: 'unwatched'}
        assert data['watched_count'] == 1
        assert data['total'] == 3

    def test_tv_progress_semantics(self, app, db, auth_client):
        from models import TVShowProgress
        u = _user(db, 'v2_watch_tv')
        lst = _list(db, u.id, 'TVWatch', slug=f'v2-{uuid.uuid4().hex[:8]}')
        show_done = _media(db, 'Done Show', media_type='tv', tmdb_id=9_650_500)
        show_wip = _media(db, 'WIP Show', media_type='tv', tmdb_id=9_650_501)
        i_done = _item(db, lst, show_done)
        i_wip = _item(db, lst, show_wip)

        db.session.add_all([
            TVShowProgress(user_id=u.id, show_id=show_done.tmdb_id,
                           status='completed', total_episodes=20,
                           watched_episodes=20),
            TVShowProgress(user_id=u.id, show_id=show_wip.tmdb_id,
                           status='watching', total_episodes=24,
                           watched_episodes=15),
        ])
        db.session.commit()

        _login(auth_client, u.username)
        r = auth_client.get(f'/api/lists/{lst.id}/watched-state')
        data = r.get_json()
        by_id = {i['id']: i for i in data['items']}
        assert by_id[i_done.id]['watched'] == 'watched'
        assert by_id[i_wip.id]['watched'] == 'watching'
        prog = by_id[i_wip.id]['watch_progress']
        assert prog['watched'] == 15 and prog['total'] == 24
        assert prog['percent'] == 63  # round(15*100/24)

    def test_anonymous_has_no_watched_state(self, client, db):
        u = _user(db, 'v2_watch_anon_owner')
        lst = _list(db, u.id, 'Anon', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _item(db, lst, _media(db, 'A1'))
        r = client.get(f'/api/lists/{lst.id}')
        items = r.get_json()['items']
        assert all('watched' not in i or i['watched'] is None
                   for i in items) or all(i.get('watched') is None
                                          for i in items)

    def test_no_n_plus_one_structural(self, app, db, auth_client):
        """One list of N items must not trigger per-item queries.

        Counts SQL statements via the engine cursor during the request.
        """
        from models import user_viewed
        u = _user(db, 'v2_watch_nplus')
        lst = _list(db, u.id, 'Nplus', slug=f'v2-{uuid.uuid4().hex[:8]}')
        for i in range(12):
            m = _media(db, f'N{i}')
            _item(db, lst, m)
            db.session.execute(user_viewed.insert().values(
                user_id=u.id, media_id=m.id, media_type='movie'))
        db.session.commit()

        _login(auth_client, u.username)

        from sqlalchemy import event
        from models.base import db as models_db
        statements = []

        def _count(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(models_db.engine, 'before_cursor_execute', _count)
        try:
            r = auth_client.get(f'/api/lists/{lst.id}/watched-state')
        finally:
            event.remove(models_db.engine, 'before_cursor_execute', _count)

        assert r.status_code == 200
        # Selects only (skip BEGIN/INSERT/COMMIT bookkeeping): watched-state
        # enrichment must stay flat in item count — viewed + watchlist +
        # progress + item/media loads, never 12 per-item queries.
        selects = [s for s in statements
                   if s.lstrip().upper().startswith('SELECT')]
        assert len(selects) < 15, f'{len(selects)} SELECTs for 12 items'


# ---------------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_public_only(self, client, db):
        u = _user(db, 'v2_disc_owner')
        pub = _list(db, u.id, 'V2 Pub Visible', is_public=True)
        _list(db, u.id, 'V2 Priv Hidden', is_public=False)
        r = client.get('/api/lists/discover?sort=recent')
        titles = [row['title'] for row in r.get_json()['lists']]
        assert pub.title in titles
        assert 'V2 Priv Hidden' not in titles

    def test_liked_sort(self, client, db):
        from models import ListLike
        u = _user(db, 'v2_disc_owner2')
        few = _list(db, u.id, 'FewLikes', slug=f'v2-{uuid.uuid4().hex[:8]}')
        many = _list(db, u.id, 'ManyLikes', slug=f'v2-{uuid.uuid4().hex[:8]}')
        for i in range(3):
            liker = _user(db, f'v2_disc_liker{i}')
            db.session.add(ListLike(user_id=liker.id, list_id=many.id))
        db.session.add(ListLike(user_id=u.id, list_id=few.id))
        db.session.commit()

        r = client.get('/api/lists/discover?sort=liked')
        lists = r.get_json()['lists']
        ids = [row['id'] for row in lists]
        if few.id in ids and many.id in ids:
            assert ids.index(many.id) < ids.index(few.id)

    def test_viewed_sort(self, client, db):
        from models import ListView
        u = _user(db, 'v2_disc_owner3')
        low = _list(db, u.id, 'LowViews', slug=f'v2-{uuid.uuid4().hex[:8]}')
        high = _list(db, u.id, 'HighViews', slug=f'v2-{uuid.uuid4().hex[:8]}')
        for _ in range(5):
            db.session.add(ListView(list_id=high.id))
        db.session.add(ListView(list_id=low.id))
        db.session.commit()

        r = client.get('/api/lists/discover?sort=viewed')
        lists = r.get_json()['lists']
        ids = [row['id'] for row in lists]
        if low.id in ids and high.id in ids:
            assert ids.index(high.id) < ids.index(low.id)

    def test_pagination(self, client, db):
        u = _user(db, 'v2_disc_owner4')
        for i in range(3):
            _list(db, u.id, f'Page{i}', slug=f'v2-page-{i}')
        r = client.get('/api/lists/discover?per_page=2&page=1')
        data = r.get_json()
        assert len(data['lists']) <= 2
        assert data['pages'] >= 2


# ---------------------------------------------------------------------------
# SECURITY / CONTRACT
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_private_list_hidden_from_anonymous_detail(self, client, db):
        u = _user(db, 'v2_sec_owner')
        lst = _list(db, u.id, 'Hidden', slug=f'v2-{uuid.uuid4().hex[:8]}', is_public=False)
        r = client.get(f'/api/lists/{lst.id}')
        assert r.status_code == 403

    def test_anonymous_cannot_mutate(self, client, db):
        u = _user(db, 'v2_sec_owner2')
        lst = _list(db, u.id, 'AnonMut', slug=f'v2-{uuid.uuid4().hex[:8]}')
        assert client.post(f'/api/lists/{lst.id}/clone').status_code in (302, 401)
        assert client.post(f'/api/lists/{lst.id}/like').status_code in (302, 401)
        assert client.put(f'/api/lists/{lst.id}/mode',
                          json={'list_type': 'ranked'}).status_code in (302, 401)

    def test_no_ids_leak_in_comment_serialization(self, auth_client, db):
        u = _user(db, 'v2_sec_cmt_owner')
        lst = _list(db, u.id, 'LeakCheck', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _item(db, lst, _media(db, 'L1'))
        commenter = _user(db, 'v2_sec_commenter')
        _login(auth_client, commenter.username)
        auth_client.post(f'/api/lists/{lst.id}/comments', json={'content': 'yo'})
        r = auth_client.get(f'/api/lists/{lst.id}')
        import json as _json
        raw = _json.dumps(r.get_json())
        # comment payload exposes username but never emails
        assert commenter.email not in raw

    def test_detail_additive_fields_present(self, auth_client, db):
        u = _user(db, 'v2_sec_owner3')
        lst = _list(db, u.id, 'Additive', slug=f'v2-{uuid.uuid4().hex[:8]}')
        _item(db, lst, _media(db, 'AD'))
        _login(auth_client, u.username)
        data = auth_client.get(f'/api/lists/{lst.id}').get_json()
        for key in ('list_type', 'like_count', 'comment_count', 'comments',
                    'comments_pages', 'liked_by_me'):
            assert key in data, f'missing additive field {key}'
        # Legacy keys untouched
        for key in ('id', 'title', 'items', 'item_count', 'is_owner'):
            assert key in data
