"""State schema for the FrameIQ LangGraph agent graph."""

from typing import TypedDict, Annotated, Sequence, Optional, List, Dict, Any
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class GraphState(TypedDict):
    """
    Shared state flowing through every node.

    Fields:
        messages:               Full conversation history (auto-merged).
        user_intent:            Routing label: "search" | "chat" | "enrich" | "end".
        next_step:              Supervisor's routing target for conditional edges.
        retrieved_context:      Actual tool results from the retriever node.
        final_response_metadata: UI payload with poster URLs and TMDb links.
        user_context:           Personalisation string built from the user's
                                watch history and ratings (injected before invoke).
    """

    messages: Annotated[Sequence[BaseMessage], add_messages]
    user_intent: Optional[str]
    next_step: Optional[str]
    retrieved_context: List[Dict[str, Any]]
    final_response_metadata: Dict[str, Any]
    user_context: Optional[str]


class SupervisorDecision(TypedDict):
    """Structured output from the LLM-based supervisor."""

    next_step: str   # "retriever" | "chat" | "enricher" | "end"
    reasoning: str   # brief justification (used in monitoring logs)
