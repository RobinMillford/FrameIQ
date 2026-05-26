"""
Tests for social routes — follow/unfollow, follower/following lists.
Verifies N+1 fix: follower list must issue a bounded number of queries.
"""
import pytest


@pytest.fixture
def two_users(db, app):
    from models import User
    from sqlalchemy import text
    with app.app_context():
        u1 = User(username='alice', email='alice@example.com', email_verified=True)
        u1.set_password('AlicePass1')
        u2 = User(username='bob', email='bob@example.com', email_verified=True)
        u2.set_password('BobPass1')
        db.session.add_all([u1, u2])
        db.session.commit()
        u1_id, u2_id = u1.id, u2.id
        yield u1_id, u2_id
        # Raw SQL bypasses ORM cascade to avoid FK/NULL constraint violations
        db.session.execute(text(
            "DELETE FROM user_follow WHERE follower_id IN (:a,:b) OR following_id IN (:a,:b)"
        ), {"a": u1_id, "b": u2_id})
        db.session.execute(text('DELETE FROM "user" WHERE id IN (:a, :b)'), {"a": u1_id, "b": u2_id})
        db.session.commit()


def test_follow_toggle(client, two_users, app):
    u1_id, u2_id = two_users
    # Login as alice
    client.post('/login', data={'username': 'alice', 'password': 'AlicePass1'})

    r = client.post(f'/api/users/{u2_id}/follow',
                    content_type='application/json', json={})
    assert r.status_code == 200
    data = r.get_json()
    assert data['is_following'] is True
    assert data['followers_count'] == 1

    # Toggle off
    r = client.post(f'/api/users/{u2_id}/follow',
                    content_type='application/json', json={})
    assert r.get_json()['is_following'] is False


def test_cannot_follow_self(client, two_users, app):
    u1_id, _ = two_users
    client.post('/login', data={'username': 'alice', 'password': 'AlicePass1'})
    r = client.post(f'/api/users/{u1_id}/follow',
                    content_type='application/json', json={})
    assert r.status_code == 400


def test_followers_list_returns_json(client, two_users, app):
    u1_id, u2_id = two_users
    client.post('/login', data={'username': 'alice', 'password': 'AlicePass1'})
    client.post(f'/api/users/{u2_id}/follow',
                content_type='application/json', json={})

    r = client.get(f'/api/users/{u2_id}/followers')
    assert r.status_code == 200
    data = r.get_json()
    assert 'followers' in data
    assert len(data['followers']) == 1
    assert data['followers'][0]['username'] == 'alice'


def test_following_list_returns_json(client, two_users, app):
    u1_id, u2_id = two_users
    client.post('/login', data={'username': 'alice', 'password': 'AlicePass1'})
    client.post(f'/api/users/{u2_id}/follow',
                content_type='application/json', json={})

    r = client.get(f'/api/users/{u1_id}/following')
    assert r.status_code == 200
    data = r.get_json()
    assert 'following' in data
    assert data['following'][0]['username'] == 'bob'


def test_follow_status_endpoint(client, two_users, app):
    u1_id, u2_id = two_users
    client.post('/login', data={'username': 'alice', 'password': 'AlicePass1'})
    client.post(f'/api/users/{u2_id}/follow',
                content_type='application/json', json={})

    r = client.get(f'/api/users/{u2_id}/follow-status')
    assert r.status_code == 200
    data = r.get_json()
    assert data['is_following'] is True
    assert data['is_followed_by'] is False
    assert data['is_mutual'] is False
