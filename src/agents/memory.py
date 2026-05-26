"""
Lightweight conversation metadata cache.

LangGraph's MemorySaver owns the actual message history and state.
This module tracks ancillary metadata (message counts, last access)
that is displayed in monitoring/metrics endpoints.
"""

from typing import Dict, Any, Optional
from datetime import datetime, timedelta

_conversation_cache: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = timedelta(hours=24)


class ConversationMemory:
    """Lightweight metadata record for one conversation session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
        self.message_count = 0
        self.metadata: Dict[str, Any] = {}

    def touch(self):
        self.last_accessed = datetime.now()

    def is_expired(self) -> bool:
        return datetime.now() - self.last_accessed > _CACHE_TTL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "message_count": self.message_count,
            "metadata": self.metadata,
        }


def get_conversation_summary(session_id: str) -> Optional[Dict[str, Any]]:
    """Return metadata for a session, or None if expired/missing."""
    conv = _conversation_cache.get(session_id)
    if conv is None:
        return None
    if conv.is_expired():
        del _conversation_cache[session_id]
        return None
    conv.touch()
    return conv.to_dict()


def update_conversation_metadata(
    session_id: str,
    message_count: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Create or update the metadata record for a session."""
    if session_id not in _conversation_cache:
        _conversation_cache[session_id] = ConversationMemory(session_id)
    conv = _conversation_cache[session_id]
    conv.message_count = message_count
    if metadata:
        conv.metadata.update(metadata)
    conv.touch()


def clear_expired_conversations():
    """Evict all expired sessions from the in-process cache."""
    expired = [sid for sid, c in _conversation_cache.items() if c.is_expired()]
    for sid in expired:
        del _conversation_cache[sid]


def get_conversation_context(session_id: str) -> str:
    """Return a short human-readable summary for logging."""
    summary = get_conversation_summary(session_id)
    if not summary:
        return "New conversation"
    parts = [
        f"Session: {session_id}",
        f"Messages: {summary['message_count']}",
    ]
    prefs = summary.get("metadata", {}).get("user_preferences")
    if prefs:
        parts.append(f"Preferences: {prefs}")
    return " | ".join(parts)
