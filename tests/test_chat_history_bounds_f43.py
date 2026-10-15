"""F43 — bounded, cursor-paginated conversation history.

The endpoint used to serialise every message in a conversation. These tests
pin the new contract: a bounded page, chronological order within the page, a
cursor that reaches older messages without repeats, and unchanged ownership
checks.
"""
from datetime import datetime, timedelta

import pytest

from routes.chat import HISTORY_PAGE_DEFAULT, HISTORY_PAGE_MAX, AGENT_HISTORY_MAX


@pytest.fixture
def convo(app, db):
    """Two users, one conversation of 30 messages owned by `owner`."""
    from models import User
    from models.chat import ChatConversation, ChatMessage
    with app.app_context():
        owner = User(username='f43chatowner', email='f43co@example.com',
                     email_verified=True)
        other = User(username='f43chatother', email='f43cx@example.com',
                     email_verified=True)
        for u in (owner, other):
            u.set_password('TestPass1')
        db.session.add_all([owner, other])
        db.session.flush()

        c = ChatConversation(user_id=owner.id, title='F43 thread')
        db.session.add(c)
        db.session.flush()

        base = datetime(2024, 5, 1, 9, 0, 0)
        made = []
        for i in range(30):
            m = ChatMessage(conversation_id=c.id,
                            role='user' if i % 2 == 0 else 'assistant',
                            content=f'message {i:02d}',
                            created_at=base.replace(second=i))
            db.session.add(m)
            made.append(m)
        db.session.commit()
        yield {'app': app, 'db': db, 'owner': owner, 'other': other,
               'conversation': c, 'messages': made}
        db.session.query(ChatMessage).filter(
            ChatMessage.conversation_id == c.id).delete(
                synchronize_session=False)
        db.session.query(ChatConversation).filter(
            ChatConversation.id == c.id).delete(synchronize_session=False)
        db.session.query(User).filter(
            User.id.in_([owner.id, other.id])).delete(
                synchronize_session=False)
        db.session.commit()


def _login(app, username):
    c = app.test_client()
    c.post('/login', data={'username': username, 'password': 'TestPass1'},
           follow_redirects=True)
    return c


@pytest.fixture
def owner_client(app, convo):
    return _login(app, 'f43chatowner')


@pytest.fixture
def other_client(app, convo):
    return _login(app, 'f43chatother')


# ── bounded ──────────────────────────────────────────────────────────────────

@pytest.fixture
def anon_client(app):
    return app.test_client()


def test_history_requires_authentication(anon_client, convo):
    """The bound is not an unauthenticated bypass."""
    r = anon_client.get(f"/chat/conversations/{convo['conversation'].id}")
    assert r.status_code in (302, 401, 403)
    assert r.status_code != 200


def test_history_default_page_is_bounded(owner_client, convo):
    """The default page is bounded — and 30 messages still fit in it."""
    body = owner_client.get(
        f"/chat/conversations/{convo['conversation'].id}").get_json()
    assert body['limit'] == 50 == HISTORY_PAGE_DEFAULT
    assert body['limit'] <= HISTORY_PAGE_MAX
    # 30 <= 50, so the whole thread comes back and nothing is withheld.
    assert len(body['messages']) == 30
    assert body['has_more'] is False
    assert body['next_before'] is None


def test_history_reports_more_when_a_page_is_smaller(owner_client, convo):
    body = owner_client.get(
        f"/chat/conversations/{convo['conversation'].id}?limit=10").get_json()
    assert len(body['messages']) == 10
    assert body['has_more'] is True
    assert body['next_before'] is not None


@pytest.mark.parametrize('raw,expected', [
    ('999999', HISTORY_PAGE_MAX),
    ('-1', 1),
    ('0', 1),
    ('abc', 50),          # malformed -> documented default
    ('5', 5),
])
def test_extreme_limits_are_clamped(owner_client, convo, raw, expected):
    body = owner_client.get(
        f"/chat/conversations/{convo['conversation'].id}?limit={raw}"
    ).get_json()
    assert body['limit'] == expected
    assert len(body['messages']) <= max(expected, 1)


def test_a_full_conversation_fits_without_paging(owner_client, convo):
    """A short conversation still comes back whole — no silent truncation."""
    from models.chat import ChatMessage
    app, db = convo['app'], convo['db']
    cid = convo['conversation'].id
    with app.app_context():
        keep = [m.id for m in convo['messages'][:3]]
        db.session.query(ChatMessage).filter(
            ChatMessage.conversation_id == cid,
            ChatMessage.id.notin_(keep)).delete(synchronize_session=False)
        db.session.commit()

    body = owner_client.get(
        f'/chat/conversations/{cid}?limit=50').get_json()
    assert len(body['messages']) == 3
    assert body['has_more'] is False
    assert body['next_before'] is None


# ── ordering and paging ──────────────────────────────────────────────────────

def test_page_is_chronological_and_holds_the_newest(owner_client, convo):
    body = owner_client.get(
        f"/chat/conversations/{convo['conversation'].id}?limit=10").get_json()
    texts = [m['content'] for m in body['messages']]
    assert texts == sorted(texts), 'a page must read oldest-first'
    assert texts[-1] == 'message 29', 'the newest message must be included'
    assert texts[0] == 'message 20'


def test_cursor_walks_back_without_repeats(owner_client, convo):
    """Paging backwards must visit every message exactly once.

    Each page reads oldest-first, but successive pages move *backwards* in
    time, so the concatenation is descending across pages — the per-page
    ordering is what must hold, together with completeness and no overlap.
    """
    cid = convo['conversation'].id
    pages = []
    seen = []
    url = f'/chat/conversations/{cid}?limit=10'
    for _ in range(10):
        body = owner_client.get(url).get_json()
        texts = [m['content'] for m in body['messages']]
        assert texts == sorted(texts), (
            'a page must read oldest-first: %s' % texts)
        pages.append(texts)
        seen.extend(texts)
        if not body['has_more'] or body['next_before'] is None:
            break
        url = f'/chat/conversations/{cid}?limit=10&before={body["next_before"]}'

    assert len(pages) == 3, '30 messages at 10/page is 3 pages, got %d' % len(pages)
    assert len(seen) == 30, 'every message must be reachable, got %d' % len(seen)
    assert len(set(seen)) == 30, 'a message was returned twice'
    assert set(seen) == {'message %02d' % i for i in range(30)}
    # Later pages are strictly older than earlier ones.
    assert pages[0][0] == 'message 20'
    assert pages[-1][-1] == 'message 09'


def test_message_shape_is_unchanged(owner_client, convo):
    body = owner_client.get(
        f"/chat/conversations/{convo['conversation'].id}?limit=3").get_json()
    for m in body['messages']:
        assert set(m) == {'role', 'content', 'metadata'}
        assert isinstance(m['metadata'], dict)
    assert body['id'] == convo['conversation'].id
    assert body['title'] == 'F43 thread'


# ── authorization is untouched ───────────────────────────────────────────────

def test_another_user_cannot_read_the_conversation(other_client, convo):
    r = other_client.get(
        f"/chat/conversations/{convo['conversation'].id}")
    assert r.status_code == 404, 'ownership check must still reject'


def test_paging_does_not_bypass_the_ownership_check(other_client, convo):
    r = other_client.get(
        f"/chat/conversations/{convo['conversation'].id}"
        f"?limit=100&before=1")
    assert r.status_code == 404


def test_missing_conversation_is_404(owner_client):
    assert owner_client.get('/chat/conversations/99999999').status_code == 404


# ── the agent's own context window is bounded too ───────────────────────────

def test_agent_history_is_capped(app, convo):
    """`_history_messages` must not feed an unbounded transcript to the agent.

    Verified through the real function. Grows the conversation past
    AGENT_HISTORY_MAX and asserts the returned window is exactly the cap and
    keeps the most recent turns, oldest-first.
    """
    from routes import chat as chat_module
    from models.chat import ChatConversation, ChatMessage

    app, db = convo['app'], convo['db']
    cid = convo['conversation'].id

    # Push the transcript past the cap.
    with app.app_context():
        base = datetime(2024, 6, 1, 9, 0, 0)
        extra = AGENT_HISTORY_MAX + 20
        db.session.add_all([
            ChatMessage(conversation_id=cid, role='user',
                        content=f'extra {i}',
                        created_at=base + timedelta(seconds=i))
            for i in range(extra)
        ])
        db.session.commit()

        c = ChatConversation.query.get(cid)
        msgs = chat_module._history_messages(c)
        assert len(msgs) == AGENT_HISTORY_MAX, (
            'agent context window is unbounded: %d messages'
            % len(msgs))
        texts = [m.content for m in msgs]
        assert texts == sorted(texts), 'agent history must be oldest-first'
        assert texts[-1] == f'extra {AGENT_HISTORY_MAX + 20 - 1}', (
            'the most recent turn must be kept, not the oldest')
        assert texts[0] not in {'message 00', 'message 01'}, (
            'the oldest turns should have been dropped')


def test_agent_history_is_whole_for_a_short_conversation(app, convo):
    """A conversation under the cap is passed through untouched and in order."""
    from routes import chat as chat_module
    from models.chat import ChatConversation, ChatMessage
    app, db = convo['app'], convo['db']
    cid = convo['conversation'].id
    with app.app_context():
        keep = [m.id for m in convo['messages'][:4]]
        db.session.query(ChatMessage).filter(
            ChatMessage.conversation_id == cid,
            ChatMessage.id.notin_(keep)).delete(synchronize_session=False)
        db.session.commit()

        c = ChatConversation.query.get(cid)
        msgs = chat_module._history_messages(c)
        assert [m.content for m in msgs] == [
            'message 00', 'message 01', 'message 02', 'message 03']
        assert [type(m).__name__ for m in msgs] == [
            'HumanMessage', 'AIMessage', 'HumanMessage', 'AIMessage']
