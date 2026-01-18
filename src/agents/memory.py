"""
Conversation memory and persistence utilities.

Provides conversation history management and state persistence.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

# In-memory cache for conversation histories
_conversation_cache: Dict[str, Dict[str, Any]] = {}
_cache_ttl = timedelta(hours=24)  # Cache expires after 24 hours


class ConversationMemory:
    """Manages conversation history and metadata."""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
        self.message_count = 0
        self.metadata = {}
    
    def update_access(self):
        """Update last accessed timestamp."""
        self.last_accessed = datetime.now()
    
    def is_expired(self) -> bool:
        """Check if conversation has expired."""
        return datetime.now() - self.last_accessed > _cache_ttl
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "message_count": self.message_count,
            "metadata": self.metadata
        }


def get_conversation_summary(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Get conversation summary from cache.
    
    Args:
        session_id: Session identifier
    
    Returns:
        Conversation metadata or None
    """
    if session_id in _conversation_cache:
        conv = _conversation_cache[session_id]
        
        # Check if expired
        if conv.is_expired():
            del _conversation_cache[session_id]
            return None
        
        conv.update_access()
        return conv.to_dict()
    
    return None


def update_conversation_metadata(
    session_id: str,
    message_count: int = 0,
    metadata: Optional[Dict[str, Any]] = None
):
    """
    Update conversation metadata in cache.
    
    Args:
        session_id: Session identifier
        message_count: Number of messages in conversation
        metadata: Additional metadata to store
    """
    if session_id not in _conversation_cache:
        _conversation_cache[session_id] = ConversationMemory(session_id)
    
    conv = _conversation_cache[session_id]
    conv.message_count = message_count
    
    if metadata:
        conv.metadata.update(metadata)
    
    conv.update_access()


def clear_expired_conversations():
    """Remove expired conversations from cache."""
    expired = [
        sid for sid, conv in _conversation_cache.items()
        if conv.is_expired()
    ]
    
    for sid in expired:
        del _conversation_cache[sid]


def get_conversation_context(session_id: str, max_messages: int = 10) -> str:
    """
    Get formatted conversation context for LLM.
    
    Args:
        session_id: Session identifier
        max_messages: Maximum number of recent messages to include
    
    Returns:
        Formatted conversation context string
    """
    summary = get_conversation_summary(session_id)
    
    if not summary:
        return "New conversation"
    
    context_parts = [
        f"Session: {session_id}",
        f"Messages: {summary['message_count']}",
        f"Duration: {_format_duration(summary['created_at'])}"
    ]
    
    if summary.get('metadata'):
        meta = summary['metadata']
        if meta.get('user_preferences'):
            context_parts.append(f"Preferences: {meta['user_preferences']}")
    
    return " | ".join(context_parts)


def _format_duration(created_at_iso: str) -> str:
    """Format conversation duration."""
    created = datetime.fromisoformat(created_at_iso)
    duration = datetime.now() - created
    
    if duration.days > 0:
        return f"{duration.days}d ago"
    elif duration.seconds > 3600:
        return f"{duration.seconds // 3600}h ago"
    elif duration.seconds > 60:
        return f"{duration.seconds // 60}m ago"
    else:
        return "just now"


# Persistence to disk (optional)
def save_conversation_to_disk(session_id: str, state: Dict[str, Any], directory: str = ".conversations"):
    """
    Save conversation state to disk for persistence.
    
    Args:
        session_id: Session identifier
        state: Graph state to save
        directory: Directory to save conversations
    """
    try:
        Path(directory).mkdir(exist_ok=True)
        filepath = Path(directory) / f"{session_id}.json"
        
        # Convert messages to serializable format
        serializable_state = {
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "message_count": len(state.get("messages", [])),
            "user_intent": state.get("user_intent"),
            "metadata": state.get("final_response_metadata", {})
        }
        
        with open(filepath, 'w') as f:
            json.dump(serializable_state, f, indent=2)
    
    except Exception as e:
        print(f"Failed to save conversation: {e}")


def load_conversation_from_disk(session_id: str, directory: str = ".conversations") -> Optional[Dict[str, Any]]:
    """
    Load conversation state from disk.
    
    Args:
        session_id: Session identifier
        directory: Directory where conversations are saved
    
    Returns:
        Conversation state or None
    """
    try:
        filepath = Path(directory) / f"{session_id}.json"
        
        if not filepath.exists():
            return None
        
        with open(filepath, 'r') as f:
            return json.load(f)
    
    except Exception as e:
        print(f"Failed to load conversation: {e}")
        return None
