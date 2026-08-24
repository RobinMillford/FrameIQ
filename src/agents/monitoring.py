"""
Logging and monitoring utilities for the agent system.

Provides structured logging, performance tracking, and debugging tools.
"""

import logging
import time
from typing import Dict, Any, Optional
from functools import wraps
from datetime import datetime
import json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


class PerformanceTracker:
    """Track performance metrics for agent operations."""
    
    def __init__(self):
        self.metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "average_response_time": 0.0,
            "tool_usage": {},
            "route_distribution": {}
        }
    
    def record_request(
        self,
        success: bool,
        response_time: float,
        route: Optional[str] = None,
        tools_used: Optional[list] = None
    ):
        """Record metrics for a request."""
        self.metrics["total_requests"] += 1
        
        if success:
            self.metrics["successful_requests"] += 1
        else:
            self.metrics["failed_requests"] += 1
        
        # Update average response time
        total = self.metrics["total_requests"]
        current_avg = self.metrics["average_response_time"]
        self.metrics["average_response_time"] = (
            (current_avg * (total - 1) + response_time) / total
        )
        
        # Track route distribution
        if route:
            self.metrics["route_distribution"][route] = (
                self.metrics["route_distribution"].get(route, 0) + 1
            )
        
        # Track tool usage
        if tools_used:
            for tool in tools_used:
                self.metrics["tool_usage"][tool] = (
                    self.metrics["tool_usage"].get(tool, 0) + 1
                )
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        return self.metrics.copy()
    
    def reset(self):
        """Reset all metrics."""
        self.__init__()


# Global performance tracker
_performance_tracker = PerformanceTracker()


def track_performance(func):
    """Decorator to track function performance."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        success = False
        
        try:
            result = func(*args, **kwargs)
            success = True
            return result
        finally:
            elapsed = time.time() - start_time
            _performance_tracker.record_request(
                success=success,
                response_time=elapsed
            )
            
            logger.info(f"{func.__name__} completed in {elapsed:.2f}s (success={success})")
    
    return wrapper


def log_agent_decision(
    node_name: str,
    decision: str,
    reasoning: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
):
    """
    Log agent routing decisions for debugging.
    
    Args:
        node_name: Name of the node making the decision
        decision: The decision made
        reasoning: Optional reasoning for the decision
        metadata: Additional metadata
    """
    log_data = {
        "timestamp": datetime.now().isoformat(),
        "node": node_name,
        "decision": decision,
        "reasoning": reasoning,
        "metadata": metadata or {}
    }
    
    logger.info(f"Agent Decision: {json.dumps(log_data, indent=2)}")


def get_performance_metrics() -> Dict[str, Any]:
    """Get current performance metrics."""
    return _performance_tracker.get_metrics()


def reset_performance_metrics():
    """Reset performance tracking."""
    _performance_tracker.reset()
