"""
P1 integration tests — /chat_api token streaming + SSE event sequence.

Mocks the agent graph so no LLM/network is needed. Verifies:
- token events stream from tagged final-response model only
- tool_call events emitted for tool starts and node labels
- final event carries reply + poster metadata
- unauthenticated request rejected
- missing message -> 400
"""
import json

import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage


@pytest.fixture(autouse=True)
def _push_request_context(request):
    """Neutralize pytest-flask's autouse request-context push for this module.

    The plugin holds one request/app context open per test, which makes all
    requests within a test share `g` — Flask-Login caches `g._login_user`
    there, so an authenticated login would leak into later "unauthenticated"
    requests. Production pushes a fresh context per request; these tests
    don't need the plugin's context (they use test clients directly).
    """
    yield


class _FakeChunk:
    def __init__(self, content):
        self.content = content


class _FakeConfig:
    """Minimal config stub for graph.astream_events."""

    def __init__(self, configurable):
        self.configurable = configurable
        self.tags = []
        self.metadata = {}
        self.callbacks = None
        self.recursion_limit = 20


def _make_fake_graph(tokens="Hello **world**", tool_name="discover_movies",
                     final_meta=None):
    """Build a fake graph whose astream_events yields a realistic event flow."""

    async def astream_events(_state, _config, version=None):
        # supervisor chain start (node label)
        yield {"event": "on_chain_start", "name": "supervisor",
               "data": {}, "tags": []}
        # retriever's internal model call — NOT tagged -> must not stream
        yield {"event": "on_chat_model_stream", "name": "ChatOpenAI",
               "tags": ["secret_internal"], "data": {"chunk": _FakeChunk("internal")}}
        # a tool call
        yield {"event": "on_tool_start", "name": tool_name,
               "data": {"input": {}}, "tags": []}
        # final-response model tokens (tagged)
        for tok in tokens:
            yield {"event": "on_chat_model_stream", "name": "ChatOpenAI",
                   "tags": ["final_response"], "data": {"chunk": _FakeChunk(tok)}}
        # enricher node start
        yield {"event": "on_chain_start", "name": "enricher",
               "data": {}, "tags": []}
        # root graph end with final state
        yield {"event": "on_chain_end", "name": "LangGraph",
               "data": {"output": {
                   "messages": [AIMessage(content="".join(tokens))],
                   "final_response_metadata": final_meta or {"movies": [], "tv_shows": []},
               }}, "tags": []}

    class FakeGraph:
        def astream_events(self, state, config, version=None):
            return astream_events(state, config, version)

    return FakeGraph()


@pytest.fixture
def auth_client(client, db, app):
    from models import User
    with app.app_context():
        u = User(username='chatuser', email='chat@example.com', email_verified=True)
        u.set_password('ChatPass1')
        db.session.add(u)
        db.session.commit()
        uid = u.id
    client.post('/login', data={'username': 'chatuser', 'password': 'ChatPass1'})
    yield client, uid
    with app.app_context():
        from models import User as U
        u = U.query.get(uid)
        if u:
            db.session.delete(u)
            db.session.commit()


@pytest.fixture
def stream_auth(app, db):
    """Self-contained authed test client for streaming tests.

    Bypasses pytest-flask's context-tracked client fixture: streaming
    responses push/pop extra request contexts (stream_with_context),
    which unbalances the plugin's teardown bookkeeping.
    """
    from models import User
    with app.app_context():
        u = User(username='streamuser', email='stream@example.com', email_verified=True)
        u.set_password('StreamPass1')
        db.session.add(u)
        db.session.commit()
        uid = u.id

    test_client = app.test_client()
    test_client.post('/login', data={'username': 'streamuser', 'password': 'StreamPass1'})
    yield test_client, uid

    with app.app_context():
        from models import User as U
        u = U.query.get(uid)
        if u:
            db.session.delete(u)
            db.session.commit()


def _parse_sse(body):
    events = []
    for line in body.split('\n\n'):
        line = line.strip()
        if line.startswith('data: '):
            events.append(json.loads(line[6:]))
    return events


class TestChatStreaming:
    def test_sse_event_sequence(self, stream_auth, app):
        client, _ = stream_auth
        fake = _make_fake_graph(
            tokens=['Hello ', '**world**'],
            final_meta={'movies': [{'title': 'Foo', 'poster_url': '/x'}],
                        'tv_shows': []},
        )
        with patch('routes.chat.get_agent_graph', return_value=fake):
            r = client.post('/chat_api', json={'message': 'hi'})
        assert r.status_code == 200
        assert r.mimetype == 'text/event-stream'

        events = _parse_sse(r.get_data(as_text=True))
        r.close()
        types = [e['type'] for e in events]

        # ordering: tool_call(s) -> tokens -> final
        assert types[0] == 'tool_call'
        assert 'token' in types
        assert types[-1] == 'final'

        tokens = [e['content'] for e in events if e['type'] == 'token']
        assert ''.join(tokens) == 'Hello **world**'

        final = events[-1]
        assert final['reply'] == 'Hello **world**'
        assert final['movies'][0]['title'] == 'Foo'

    def test_internal_model_tokens_not_streamed(self, stream_auth, app):
        client, _ = stream_auth
        fake = _make_fake_graph(tokens=['visible'])
        with patch('routes.chat.get_agent_graph', return_value=fake):
            r = client.post('/chat_api', json={'message': 'q'})
        events = _parse_sse(r.get_data(as_text=True))
        r.close()
        token_text = ''.join(
            e['content'] for e in events if e['type'] == 'token')
        assert 'internal' not in token_text
        assert token_text == 'visible'

    def test_tool_call_event_emitted(self, stream_auth, app):
        client, _ = stream_auth
        fake = _make_fake_graph(tool_name='discover_movies')
        with patch('routes.chat.get_agent_graph', return_value=fake):
            r = client.post('/chat_api', json={'message': 'q'})
        events = _parse_sse(r.get_data(as_text=True))
        r.close()
        tools = [e['tool'] for e in events if e['type'] == 'tool_call']
        assert 'discover_movies' in tools
        # node labels present too
        assert 'supervisor' in tools and 'enricher' in tools

    def test_no_response_error_when_no_final_state(self, stream_auth, app):
        client, _ = stream_auth
        fake = _make_fake_graph()

        async def astream_events_no_end(_s, _c, version=None):
            yield {"event": "on_chain_start", "name": "supervisor",
                   "data": {}, "tags": []}
            # no LangGraph on_chain_end -> final_state stays None

        fake.astream_events = astream_events_no_end
        with patch('routes.chat.get_agent_graph', return_value=fake):
            r = client.post('/chat_api', json={'message': 'q'})
        events = _parse_sse(r.get_data(as_text=True))
        r.close()
        assert events[-1]['type'] == 'error'
        assert 'No response generated' in events[-1]['error']

    def test_missing_message_400(self, stream_auth, app):
        client, _ = stream_auth
        r = client.post('/chat_api', json={})
        assert r.status_code == 400

    def test_unauthenticated_rejected(self, app, db):
        """Fresh unauthenticated client must be redirected."""
        from models import User
        with app.app_context():
            u = User(username='anoncheck', email='anon@example.com', email_verified=True)
            u.set_password('AnonPass1')
            db.session.add(u)
            db.session.commit()
            uid = u.id
        # log in on client A, then hit the endpoint from a clean client B
        ca = app.test_client()
        ca.post('/login', data={'username': 'anoncheck', 'password': 'AnonPass1'})
        cb = app.test_client()
        r = cb.post('/chat_api', json={'message': 'hi'})
        if r.status_code == 200:  # debug
            print('BODY:', r.get_data(as_text=True)[:200])
        assert r.status_code in (302, 401)
        with app.app_context():
            u = User.query.get(uid)
            if u:
                db.session.delete(u)
                db.session.commit()


class TestAgentEndpointsPruned:
    def test_agent_chat_api_removed(self, stream_auth, app):
        client, _ = stream_auth
        r = client.post('/agent_chat_api', json={'message': 'hi'})
        assert r.status_code == 404

    def test_agent_chat_stream_removed(self, stream_auth, app):
        client, _ = stream_auth
        r = client.post('/agent_chat_stream', json={'message': 'hi'})
        assert r.status_code == 404

    def test_metrics_health_alive(self, stream_auth, app):
        client, _ = stream_auth
        assert client.get('/agent_metrics').status_code == 200
        assert client.get('/agent_health').status_code == 200


class TestPersistentMemory:
    def test_sqlite_checkpoint_configured(self, app):
        """Graph must use SqliteSaver, not in-process MemorySaver."""
        from src.agents.graph import get_agent_graph
        graph = get_agent_graph()
        checkpointer = getattr(graph, "checkpointer", None)
        assert checkpointer is not None, "graph has no checkpointer"
        from langgraph.checkpoint.sqlite import SqliteSaver
        assert isinstance(checkpointer, SqliteSaver), (
            f"expected SqliteSaver, got {type(checkpointer).__name__}"
        )

    def test_checkpoint_db_path_exists_after_compile(self, app):
        import os
        from src.agents.graph import _CHECKPOINT_DB
        assert _CHECKPOINT_DB.endswith("chat_memory.db")
        assert os.path.isdir(os.path.dirname(_CHECKPOINT_DB))
