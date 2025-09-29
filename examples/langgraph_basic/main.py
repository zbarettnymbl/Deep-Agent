"""Minimal LangGraph workflow with inline tools.

This script accompanies the documentation that contrasts a lightweight
LangGraph-only workflow with the deeper AgentExecutor-driven orchestration in
``examples/deep_agent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, TypedDict

from langgraph.graph import END, StateGraph


# --- State definition -------------------------------------------------------
class BasicState(TypedDict, total=False):
    """Structure of the state object flowing through the LangGraph."""

    query: str
    scratchpad: List[str]
    result: str


# --- Tool registration ------------------------------------------------------
@dataclass(frozen=True)
class InlineTool:
    """Simple callable wrapper with metadata for instructional purposes."""

    name: str
    description: str
    func: Callable[[str], str]

    def __call__(self, text: str) -> str:
        return self.func(text)


def build_toolkit() -> Dict[str, InlineTool]:
    """Create the inline tools used by the agent node."""

    def to_upper(text: str) -> str:
        return text.upper()

    def word_count(text: str) -> str:
        count = len(text.split())
        return f"The prompt contains {count} words."

    def reverse(text: str) -> str:
        return text[::-1]

    tools = [
        InlineTool(
            name="uppercase",
            description="Convert the entire string to uppercase characters.",
            func=to_upper,
        ),
        InlineTool(
            name="word_count",
            description="Count how many words are present in the string.",
            func=word_count,
        ),
        InlineTool(
            name="reverse",
            description="Reverse the characters in the supplied string.",
            func=reverse,
        ),
    ]
    return {tool.name: tool for tool in tools}


# --- Node implementations ---------------------------------------------------
def choose_tool(query: str, tools: Dict[str, InlineTool]) -> InlineTool:
    """Naively select an inline tool based on keyword heuristics."""

    lowered = query.lower()
    if "upper" in lowered or "shout" in lowered:
        return tools["uppercase"]
    if "count" in lowered:
        return tools["word_count"]
    return tools["reverse"]


def build_agent_node(tools: Dict[str, InlineTool]):
    """Create the lone agent node responsible for dispatching to tools."""

    def agent(state: BasicState) -> BasicState:
        scratchpad = list(state.get("scratchpad", []))
        scratchpad.append(f"Received query: {state['query']}")

        tool = choose_tool(state["query"], tools)
        scratchpad.append(f"Selected tool: {tool.name}")
        scratchpad.append(f"Tool description: {tool.description}")

        result = tool(state["query"])
        scratchpad.append(f"Tool output: {result}")

        return {
            "query": state["query"],
            "scratchpad": scratchpad,
            "result": result,
        }

    return agent


# --- Graph construction -----------------------------------------------------
def build_basic_graph() -> StateGraph[BasicState]:
    """Assemble the minimal graph with a single agent node."""

    tools = build_toolkit()
    workflow: StateGraph[BasicState] = StateGraph(BasicState)
    workflow.add_node("agent", build_agent_node(tools))
    workflow.set_entry_point("agent")
    workflow.add_edge("agent", END)
    return workflow


# --- CLI helper -------------------------------------------------------------
def main() -> None:
    """Run the basic LangGraph workflow for a sample query."""

    graph = build_basic_graph().compile()
    query = "Please count the words in LangGraph makes agents modular"
    final_state = graph.invoke({"query": query, "scratchpad": []})
    print("Query:", query)
    print("Result:", final_state.get("result"))
    print("Scratchpad:")
    for line in final_state.get("scratchpad", []):
        print("  -", line)


if __name__ == "__main__":
    main()
