#!/usr/bin/env python3
"""
NOUS LangGraph multi-agent graf.
Entry-point: supervisor → household | legal | supervisor(self).
"""
from typing import TypedDict

from langgraph.graph import END, StateGraph

from household import HouseholdAgent
from legal import LegalAgent
from legacy import LegacyAgent
from supervisor import SupervisorAgent

_supervisor = SupervisorAgent()
_household  = HouseholdAgent()
_legal      = LegalAgent()
_legacy     = LegacyAgent()


class AgentState(TypedDict):
    query:      str
    user:       str
    routed_to:  str
    context:    list[dict]
    response:   str
    agent_name: str


# ── Graf-noder ─────────────────────────────────────────────────────────────────

def supervisor_node(state: AgentState) -> AgentState:
    decision = _supervisor.route(state["query"])
    if decision == "supervisor":
        answer = _supervisor.answer(state["query"])
        return {**state, "routed_to": "supervisor", "response": answer, "agent_name": "supervisor"}
    return {**state, "routed_to": decision}


def household_node(state: AgentState) -> AgentState:
    hits: list[dict] = []
    for wing in _household.allowed_wings:
        try:
            hits.extend(_household.read(state["query"], wing))
        except Exception:
            pass
    context = "\n\n---\n\n".join(
        h["payload"].get("text", "")[:600] for h in hits
    )
    response = _household.think(context, state["query"])
    return {**state, "context": hits, "response": response, "agent_name": "household"}


def legal_node(state: AgentState) -> AgentState:
    # qwen3:14b reserveres til night_pipeline — ingen LLM-kald i interaktiv mode
    return {
        **state,
        "context":    [],
        "response":   "Juridisk analyse kører i nat-pipeline kl. 00:00. Resultater findes i Analyse-mode i Cockpit.",
        "agent_name": "legal",
    }


def legacy_node(state: AgentState) -> AgentState:
    response, hits = _legacy.think_with_sources(state["query"])
    return {**state, "context": hits, "response": response, "agent_name": "legacy"}


def _route_decision(state: AgentState) -> str:
    return state["routed_to"]


# ── Graf-kompilering ───────────────────────────────────────────────────────────

_builder = StateGraph(AgentState)
_builder.add_node("supervisor", supervisor_node)
_builder.add_node("household",  household_node)
_builder.add_node("legal",      legal_node)
_builder.add_node("legacy",     legacy_node)

_builder.set_entry_point("supervisor")
_builder.add_conditional_edges(
    "supervisor",
    _route_decision,
    {
        "household":  "household",
        "legal":      "legal",
        "legacy":     "legacy",
        "supervisor": END,
    },
)
_builder.add_edge("household", END)
_builder.add_edge("legal",     END)
_builder.add_edge("legacy",    END)

agent_app = _builder.compile()


def run_agent_graph(query: str, user: str = "dan") -> dict:
    """Kør agent-grafen og returnér state-dict med response, agent_name og routed_to."""
    initial: AgentState = {
        "query":      query,
        "user":       user,
        "routed_to":  "",
        "context":    [],
        "response":   "",
        "agent_name": "",
    }
    return agent_app.invoke(initial)
