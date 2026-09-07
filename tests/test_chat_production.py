"""
P2 production-hardening tests for /chat_api.

Covers the two audited concerns:

1. AsyncSqliteSaver loop-affinity — every async graph call must execute
   on the single process chat loop; the request path must not create its
   own event loop.
2. Pre-stream HTTP semantics — graph/checkpointer failure BEFORE the
   first SSE byte must be HTTP 503 JSON, never HTTP 200.

Plus: lifecycle cleanup, persistence across messages, same-conversation
concurrency, and warning assertions (unawaited coroutines fail loudly).
"""
import asyncio
import json
import threading
import warnings

import pytest
from unittest.mock import patch
from langchain_core.messages import AIMessage


class _FakeChunk:
    def __init__(self, content):
        self.content = content


def _parse_sse(body):
    events = []
    for line in body.split('\n\n'):
        line = line.strip()
        if line.startswith('data: '):
            events.append(json.loads(line[6:]))
    return events


def _make_events(tokens=("hi",), final_meta=None):
    return [
        {"event": "on_chain_start", "name": "supervisor", "data": {}, "tags": []},
        *(
            {"event": "on_chat_model_stream", "name": "ChatOpenAI",
             "tags": ["final_response"], "data": {"chunk": _FakeChunk(t)}}
            for t in tokens
        ),
        {"event": "on_chain_end", "name": "LangGraph",
         "data": {"output": {
             "messages": [AIMessage(content="".join(tokens))],
             "final_response_metadata": final_meta or {"movies": [], "tv_shows": []},
         }}, "tags": []},
    ]


class _FakeGraph:
    """Thread-safe fake whose astream_events runs on ANY loop."""

    def __init__(self, tokens=("hi",), delay=0.0):
        self._tokens = tokens
        self._delay = delay
        self.seen_states = []
        self.closed = []

    def astream_events(self, state, config, version=None):
        outer = self

        class _Stream:
            def __init__(self):
                self._it = iter(_make_events(outer._tokens))
                outer.seen_states.append(state)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if outer._delay:
                    await asyncio.sleep(outer._delay)
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

            async def aclose(self):
                outer.closed.append(True)

        return _Stream()


@pytest.fixture
def stream_auth(app, db):
    from models import User
    with app.app_context():
        u = User(username='produser', email='prod@example.com', email_verified=True)
        u.set_password('ProdPass1')
        db.session.add(u)
        db.session.commit()
        uid = u.id

    test_client = app.test_client()
    test_client.post('/login', data={'username': 'produser', 'password': 'ProdPass1'})
    yield test_client, uid

    with app.app_context():
        from models import User as U
        u = U.query.get(uid)
        if u:
            db.session.delete(u)
            db.session.commit()


class TestPreStreamHttpSemantics:
    def test_graph_unavailable_returns_503_json(self, stream_auth, app):
        client, uid = stream_auth
        with patch('routes.chat.get_agent_graph', return_value=None):
            r = client.post('/chat_api', json={'message': 'hello'})
        assert r.status_code == 503
        body = r.get_json()
        assert body["error"] == "Chat service temporarily unavailable"
        # No internal details leak.
        assert "Traceback" not in r.get_data(as_text=True)
        assert "sqlite" not in r.get_data(as_text=True).lower()

    def test_graph_unavailable_consumes_no_quota(self, stream_auth, app):
        client, uid = stream_auth
        with patch('routes.chat.get_agent_graph', return_value=None):
            client.post('/chat_api', json={'message': 'hello'})
        with app.app_context():
            from models import UserChatDailyUsage
            usage = UserChatDailyUsage.query.filter_by(user_id=uid).first()
            assert usage is None or usage.question_count == 0

    def test_checkpointer_unavailable_returns_503(self, stream_auth):
        client, _ = stream_auth

        class _BrokenCkpt:
            async def setup(self):
                raise OSError("checkpoint file locked")

        class _GraphWithBrokenCkpt(_FakeGraph):
            checkpointer = _BrokenCkpt()

        with patch('routes.chat.get_agent_graph',
                   return_value=_GraphWithBrokenCkpt()):
            r = client.post('/chat_api', json={'message': 'hello'})
        assert r.status_code == 503
        assert r.get_json()["error"] == "Chat service temporarily unavailable"

    def test_mid_stream_failure_is_sse_error_with_200(self, stream_auth):
        client, _ = stream_auth

        class _FailingGraph:
            def astream_events(self, state, config, version=None):
                async def _gen():
                    yield {"event": "on_chat_model_stream", "name": "ChatOpenAI",
                           "tags": ["final_response"],
                           "data": {"chunk": _FakeChunk("partial")}}
                    raise RuntimeError("llm exploded")
                    yield  # pragma: no cover - makes this an async generator
                return _gen()

        with patch('routes.chat.get_agent_graph', return_value=_FailingGraph()):
            r = client.post('/chat_api', json={'message': 'q'})
        assert r.status_code == 200
        assert r.mimetype == 'text/event-stream'
        events = _parse_sse(r.get_data(as_text=True))
        r.close()
        assert any(e["type"] == "token" for e in events)
        assert events[-1]["type"] == "error"
        assert events[-1]["error"] == "Generation failed"


class TestAsyncLifecycle:
    def test_request_path_creates_no_event_loop(self, stream_auth):
        """The request thread must never call asyncio.new_event_loop.

        All async graph execution happens on the process chat loop.
        """
        import asyncio as asyncio_mod
        from src.agents import graph as graph_mod
        graph_mod.get_chat_loop()  # warm: loop already running before patch

        client, _ = stream_auth
        fake = _FakeGraph(tokens=("loop-free",))
        with patch('routes.chat.get_agent_graph', return_value=fake), \
             patch.object(asyncio_mod, 'new_event_loop',
                          side_effect=AssertionError("per-request loop created")):
            r = client.post('/chat_api', json={'message': 'q'})
            assert r.status_code == 200
            events = _parse_sse(r.get_data(as_text=True))
            r.close()
        assert events[-1]["type"] == "final"
        assert events[-1]["reply"] == "loop-free"

    def test_async_generator_is_closed_on_same_loop(self, stream_auth):
        client, _ = stream_auth
        fake = _FakeGraph(tokens=("close-me",))
        with patch('routes.chat.get_agent_graph', return_value=fake):
            r = client.post('/chat_api', json={'message': 'q'})
            r.get_data(as_text=True)
            r.close()
        assert fake.closed, "producer did not aclose() the astream_events generator"

    def test_no_unawaited_coroutine_warnings(self, stream_auth):
        client, _ = stream_auth
        fake = _FakeGraph(tokens=("clean",))
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            with patch('routes.chat.get_agent_graph', return_value=fake):
                r = client.post('/chat_api', json={'message': 'q'})
                r.get_data(as_text=True)
                r.close()
        bad = [
            w for w in records
            if issubclass(w.category, RuntimeWarning)
            and ("never awaited" in str(w.message)
                 or "shutdown_asyncgens" in str(w.message))
        ]
        assert not bad, "unawaited-coroutine warnings: %r" % (
            [str(w.message) for w in bad],)


class TestPersistenceAcrossMessages:
    def test_second_message_sees_prior_history(self, stream_auth):
        client, _ = stream_auth
        fake = _FakeGraph(tokens=("first reply",))
        with patch('routes.chat.get_agent_graph', return_value=fake):
            r1 = client.post('/chat_api', json={'message': 'i love sci-fi'})
            assert r1.status_code == 200
            r1.get_data()
            cid = int(r1.headers['X-Chat-Conversation-ID'])
            r1.close()

            r2 = client.post('/chat_api',
                             json={'message': 'more like that',
                                   'conversation_id': cid})
            assert r2.status_code == 200
            r2.get_data()
            r2.close()

        assert len(fake.seen_states) == 2
        second_messages = fake.seen_states[1]["messages"]
        texts = [getattr(m, "content", "") for m in second_messages]
        assert "i love sci-fi" in texts
        assert "first reply" in texts
        assert "more like that" in texts

    def test_graph_rebuild_keeps_checkpoint_file(self, app, tmp_path):
        """Closing and rebuilding the graph must not lose the checkpoint DB."""
        import os
        import sqlite3
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from src.agents import graph as graph_mod
        first = graph_mod.get_agent_graph()
        assert first is not None
        checkpointer = getattr(first, "checkpointer", None)
        # Must be the REAL sqlite saver, not a silent in-memory fallback.
        assert isinstance(checkpointer, AsyncSqliteSaver), (
            "graph is not backed by AsyncSqliteSaver; persistence is dead"
        )
        assert os.path.isfile(graph_mod._CHECKPOINT_DB)
        with sqlite3.connect(graph_mod._CHECKPOINT_DB) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

        graph_mod._close_graph()
        second = graph_mod.get_agent_graph()
        assert second is not None
        assert isinstance(getattr(second, "checkpointer", None),
                          AsyncSqliteSaver)
        assert os.path.isfile(graph_mod._CHECKPOINT_DB)


def _file_backed_app(tmp_path, monkeypatch):
    """A second app instance on a FILE sqlite DB for threaded tests.

    The session app uses :memory: (one connection per thread = empty DB
    per thread), so real cross-thread requests need a shared file.
    """
    import os
    db_file = str(tmp_path / "concurrency.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    # create_app reads env at call time; SECRET_KEY/TMDB key already set.
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    try:
        from app import create_app
        file_app = create_app()
    finally:
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    file_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                           RATELIMIT_ENABLED=False)
    return file_app, db_file


def _login_client(file_app, username, password):
    from models import User
    with file_app.app_context():
        from models import db as _db
        u = User(username=username, email=f"{username}@example.com",
                 email_verified=True)
        u.set_password(password)
        _db.session.add(u)
        _db.session.commit()
        uid = u.id
    client = file_app.test_client()
    client.post('/login', data={'username': username, 'password': password})
    return client, uid


class _EchoGraph:
    """One shared fake for threaded tests: echoes the newest user message.

    A single instance is patched in for the whole run — per-thread
    patch() enter/exit would race and cross responses between threads.
    """

    def __init__(self, delay=0.05):
        self._delay = delay

    def astream_events(self, state, config, version=None):
        outer = self

        class _Stream:
            def __init__(self):
                newest = state["messages"][-1].content
                self._it = iter(_make_events((f"echo:{newest}",)))

            def __aiter__(self):
                return self

            async def __anext__(self):
                if outer._delay:
                    await asyncio.sleep(outer._delay)
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

            async def aclose(self):
                pass

        return _Stream()


class TestConcurrency:
    def test_two_conversations_stream_concurrently(self, tmp_path, monkeypatch):
        from sqlalchemy import text
        file_app, db_file = _file_backed_app(tmp_path, monkeypatch)
        with file_app.app_context():
            from models import db as _db
            with _db.engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.execute(text("PRAGMA busy_timeout=5000"))
                conn.commit()

        results, errors = {}, []

        def _run(name):
            try:
                client, _ = _login_client(file_app, name, 'Passw0rd!')
                r = client.post('/chat_api', json={'message': f'msg-{name}'})
                assert r.status_code == 200
                events = _parse_sse(r.get_data(as_text=True))
                r.close()
                results[name] = events
            except Exception as exc:  # noqa: BLE001 — collected, then asserted
                errors.append(exc)

        with patch('routes.chat.get_agent_graph', return_value=_EchoGraph()):
            threads = [threading.Thread(target=_run, args=(f"user{i}",))
                       for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        assert not errors, "concurrent streams failed: %r" % (errors,)
        assert results["user0"][-1]["reply"] == "echo:msg-user0"
        assert results["user1"][-1]["reply"] == "echo:msg-user1"

    def test_same_conversation_concurrent_requests_do_not_corrupt(
            self, tmp_path, monkeypatch):
        file_app, _ = _file_backed_app(tmp_path, monkeypatch)
        client, uid = _login_client(file_app, 'sameconv', 'Passw0rd!')

        with patch('routes.chat.get_agent_graph', return_value=_EchoGraph()):
            # One conversation created up front; both threads post into it.
            r = client.post('/chat_api', json={'message': 'opener'})
            assert r.status_code == 200
            cid = int(r.headers['X-Chat-Conversation-ID'])
            r.get_data()
            r.close()

            errors = []

            def _run(tag):
                try:
                    thread_client = file_app.test_client()
                    thread_client.post('/login',
                                       data={'username': 'sameconv',
                                             'password': 'Passw0rd!'})
                    resp = thread_client.post(
                        '/chat_api',
                        json={'message': f'q-{tag}', 'conversation_id': cid})
                    assert resp.status_code == 200
                    resp.get_data()
                    resp.close()
                except Exception as exc:  # noqa: BLE001 — collected, then asserted
                    errors.append(exc)

            threads = [threading.Thread(target=_run, args=(tag,))
                       for tag in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        assert not errors, "same-conversation concurrency failed: %r" % (errors,)
        with file_app.app_context():
            from models import ChatMessage
            messages = ChatMessage.query.filter_by(
                conversation_id=cid).order_by(ChatMessage.id).all()
            by_role = {}
            for m in messages:
                by_role.setdefault(m.role, []).append(m.content)
            # opener user+assistant, plus 2 concurrent user+assistant pairs.
            assert sorted(by_role.get('user', [])) == ['opener', 'q-a', 'q-b'], (
                "lost or duplicated user messages: %r"
                % [(m.role, m.content) for m in messages]
            )
            assert sorted(by_role.get('assistant', [])) == [
                'echo:opener', 'echo:q-a', 'echo:q-b'], (
                "lost or duplicated assistant messages: %r"
                % [(m.role, m.content) for m in messages]
            )
