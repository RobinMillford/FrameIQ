"""
Rate limiting and request throttling for the agent system.

Protects against API rate limits and excessive usage.
"""

from typing import Dict, Optional
from datetime import datetime, timedelta
from collections import defaultdict
import time


class RateLimiter:
    """Simple rate limiter using token bucket algorithm."""
    
    def __init__(self, max_requests: int = 10, time_window: int = 60):
        """
        Initialize rate limiter.
        
        Args:
            max_requests: Maximum requests allowed in time window
            time_window: Time window in seconds
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests: Dict[str, list] = defaultdict(list)
    
    def is_allowed(self, identifier: str) -> bool:
        """
        Check if request is allowed for identifier.
        
        Args:
            identifier: Unique identifier (e.g., session_id, IP address)
        
        Returns:
            True if request is allowed, False otherwise
        """
        now = datetime.now()
        cutoff = now - timedelta(seconds=self.time_window)
        
        # Remove old requests
        self.requests[identifier] = [
            req_time for req_time in self.requests[identifier]
            if req_time > cutoff
        ]
        
        # Check if under limit
        if len(self.requests[identifier]) < self.max_requests:
            self.requests[identifier].append(now)
            return True
        
        return False
    
    def get_wait_time(self, identifier: str) -> float:
        """
        Get time to wait before next request is allowed.
        
        Args:
            identifier: Unique identifier
        
        Returns:
            Seconds to wait, or 0 if request is allowed now
        """
        if self.is_allowed(identifier):
            return 0.0
        
        # Find oldest request
        if not self.requests[identifier]:
            return 0.0
        
        oldest = min(self.requests[identifier])
        wait_until = oldest + timedelta(seconds=self.time_window)
        wait_seconds = (wait_until - datetime.now()).total_seconds()
        
        return max(0.0, wait_seconds)


# Global rate limiters
_user_rate_limiter = RateLimiter(max_requests=20, time_window=60)  # 20 req/min per user
_global_rate_limiter = RateLimiter(max_requests=100, time_window=60)  # 100 req/min global


def check_rate_limit(session_id: str) -> tuple[bool, Optional[float]]:
    """
    Check if request is within rate limits.
    
    Args:
        session_id: Session identifier
    
    Returns:
        Tuple of (is_allowed, wait_time_seconds)
    """
    # Check global limit first
    if not _global_rate_limiter.is_allowed("global"):
        wait_time = _global_rate_limiter.get_wait_time("global")
        return False, wait_time
    
    # Check per-user limit
    if not _user_rate_limiter.is_allowed(session_id):
        wait_time = _user_rate_limiter.get_wait_time(session_id)
        return False, wait_time
    
    return True, None


def get_rate_limit_info(session_id: str) -> Dict[str, any]:
    """
    Get rate limit information for a session.
    
    Args:
        session_id: Session identifier
    
    Returns:
        Dictionary with rate limit details
    """
    now = datetime.now()
    cutoff = now - timedelta(seconds=_user_rate_limiter.time_window)
    
    recent_requests = [
        req for req in _user_rate_limiter.requests.get(session_id, [])
        if req > cutoff
    ]
    
    return {
        "requests_made": len(recent_requests),
        "requests_allowed": _user_rate_limiter.max_requests,
        "time_window_seconds": _user_rate_limiter.time_window,
        "requests_remaining": max(0, _user_rate_limiter.max_requests - len(recent_requests))
    }
