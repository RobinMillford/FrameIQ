"""
Test script for the LangGraph agent system.

Run this to test the agent workflow without starting the full Flask app.
"""

import os
import sys
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

load_dotenv()

from src.api.agent_service import run_agent_chat


def test_agent():
    """Test the agent with various queries."""
    
    test_queries = [
        "Hello! What can you help me with?",
        "Suggest me movies like Inception",
        "What are some recent sci-fi movies from 2024?",
        "What is film noir?",
        "Show me trending movies this week",
    ]
    
    session_id = "test_session_001"
    
    print("=" * 80)
    print("TESTING LANGGRAPH AGENT SYSTEM")
    print("=" * 80)
    
    for i, query in enumerate(test_queries, 1):
        print(f"\n{'='*80}")
        print(f"TEST {i}: {query}")
        print(f"{'='*80}\n")
        
        try:
            response = run_agent_chat(query, session_id)
            
            print(f"REPLY: {response.get('reply', 'No reply')}\n")
            
            if response.get('movies'):
                print(f"MOVIES ({len(response['movies'])}):")
                for movie in response['movies']:
                    print(f"  - {movie.get('title')} ({movie.get('year')})")
            
            if response.get('tv_shows'):
                print(f"TV SHOWS ({len(response['tv_shows'])}):")
                for show in response['tv_shows']:
                    print(f"  - {show.get('title')} ({show.get('year')})")
            
            if response.get('error'):
                print(f"ERROR: {response['error']}")
        
        except Exception as e:
            print(f"EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
        
        print()
    
    print("=" * 80)
    print("TESTING COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    test_agent()
