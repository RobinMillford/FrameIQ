"""
Enhanced error handling and fallback mechanisms for the agent system.

This module provides robust error handling, retry logic, and graceful degradation.
"""

from typing import Dict, Any, Optional
from functools import wraps
import time
import logging

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """Base exception for agent-related errors."""
    pass


class ToolExecutionError(AgentError):
    """Raised when a tool fails to execute."""
    pass


class LLMError(AgentError):
    """Raised when LLM call fails."""
    pass


def retry_on_error(max_retries: int = 3, delay: float = 1.0):
    """
    Decorator to retry function calls on failure.
    
    Args:
        max_retries: Maximum number of retry attempts
        delay: Delay between retries in seconds
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
                    
                    if attempt < max_retries - 1:
                        time.sleep(delay * (attempt + 1))  # Exponential backoff
                    else:
                        logger.error(f"All {max_retries} attempts failed")
            
            raise last_exception
        
        return wrapper
    return decorator


def safe_tool_execution(tool_func, *args, **kwargs) -> Dict[str, Any]:
    """
    Safely execute a tool with error handling.
    
    Returns:
        Dictionary with 'success', 'data', and optional 'error' keys
    """
    try:
        result = tool_func(*args, **kwargs)
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        logger.error(f"Tool execution failed: {tool_func.__name__}: {e}")
        return {
            "success": False,
            "error": str(e),
            "data": None
        }


def get_fallback_response(error_type: str, user_message: str) -> str:
    """
    Generate appropriate fallback response based on error type.
    
    Args:
        error_type: Type of error encountered
        user_message: Original user message
    
    Returns:
        Friendly fallback message
    """
    fallbacks = {
        "tool_error": "I encountered an issue searching for that information. Could you try rephrasing your question?",
        "llm_error": "I'm having trouble processing your request right now. Please try again in a moment.",
        "rate_limit": "I'm receiving too many requests right now. Please wait a moment and try again.",
        "timeout": "That search is taking longer than expected. Could you try a more specific query?",
        "not_found": "I couldn't find any results for that. Could you provide more details or try a different search?"
    }
    
    return fallbacks.get(error_type, "I encountered an unexpected error. Please try again.")


def validate_state(state: Dict[str, Any]) -> bool:
    """
    Validate that state has required fields.
    
    Args:
        state: Graph state dictionary
    
    Returns:
        True if valid, False otherwise
    """
    required_fields = ["messages", "user_intent", "next_step"]
    
    for field in required_fields:
        if field not in state:
            logger.error(f"Missing required state field: {field}")
            return False
    
    return True
