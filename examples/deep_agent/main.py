"""Deep Agent Example.

This module demonstrates how to orchestrate a LangChain AgentExecutor as the
primary coordinator while delegating specific tasks to LangGraph-powered
sub-agents. The sections below mirror the accompanying documentation so that
readers can match conceptual descriptions with the relevant code.
"""

# --- Model Setup -----------------------------------------------------------
from __future__ import annotations

"""Utilities for configuring language models and tools used by the agents."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, TypedDict

from langchain.agents import AgentExecutor, create_structured_chat_agent
from langchain.agents.output_parsers import JSONAgentOutputParser
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, Tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from integrations.google_drive import (
    GoogleDriveIntegrationError,
    create_google_drive_tools,
)
from integrations.outlook import OutlookIntegrationError, create_outlook_tools


logger = logging.getLogger(__name__)

def initialize_llm(
    *,
    model_name: str = "gpt-4o-mini",
    temperature: float = 0.0,
    api_key: Optional[str] = None,
) -> BaseChatModel:
    """Create a chat model that will power both the primary agent and sub-agents.

    Parameters
    ----------
    model_name:
        The name of the OpenAI compatible model to use.
    temperature:
        Sampling temperature controlling creativity of responses.
    api_key:
        Optional override for the OpenAI API key. If omitted the environment
        variable ``OPENAI_API_KEY`` will be used by the LangChain integration.
    """

    return ChatOpenAI(model=model_name, temperature=temperature, api_key=api_key)


def initialize_tools() -> List[BaseTool]:
    """Define the shared tool set exposed to the agent ecosystem."""

    tools: List[BaseTool] = []
    configuration_errors: List[str] = []

    try:
        tools.extend(create_outlook_tools())
    except OutlookIntegrationError as exc:  # pragma: no cover - example guardrail
        configuration_errors.append(
            "Outlook integration is not configured. "
            "Follow docs/deep_agent_example.md to provide Azure credentials."
        )
        logger.warning("Outlook tools unavailable: %s", exc)

    try:
        tools.extend(create_google_drive_tools())
    except GoogleDriveIntegrationError as exc:  # pragma: no cover - example guardrail
        configuration_errors.append(
            "Google Drive integration is not configured. "
            "Follow docs/deep_agent_example.md to provide credentials."
        )
        logger.warning("Google Drive tools unavailable: %s", exc)

    if not tools:
        raise RuntimeError("No integrations configured: " + " ".join(configuration_errors))

    logger.info("Coordinator loaded tools: %s", ", ".join(tool.name for tool in tools))
    return tools


# --- Graph Definition ------------------------------------------------------
"""LangGraph workflow for reusable sub-agents."""


class SubAgentState(TypedDict):
    """LangGraph state shared across sub-agent nodes."""

    task: str
    context: List[str]
    result: Optional[str]


@dataclass
class SubAgentConfig:
    """Configuration object bundling model, tools, and metadata."""

    name: str
    llm: BaseChatModel
    tools: List[BaseTool]


def build_sub_agent_graph(config: SubAgentConfig):
    """Construct a LangGraph workflow representing a single sub-agent.

    The graph contains three conceptual steps: planning, acting, and reporting.
    Each step is implemented as a node that reads and updates the shared state.
    """

    workflow = StateGraph(SubAgentState)

    def plan(state: SubAgentState) -> SubAgentState:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    (
                        "You are the {name} sub-agent. Plan how you will approach "
                        "the task using the available tools: {tool_names}."
                    ),
                ),
                ("human", "Task: {task}"),
            ]
        ).format_messages(
            name=config.name, task=state["task"], tool_names=", ".join(t.name for t in config.tools)
        )
        message = config.llm.invoke(prompt)
        state.setdefault("context", []).append(message.content)
        return state

    def act(state: SubAgentState) -> SubAgentState:
        # For brevity the action chooses the first tool. Replace with reasoning
        # loops for richer examples.
        tool = config.tools[0]
        state["result"] = tool.invoke(state["task"])
        state.setdefault("context", []).append(state["result"])
        return state

    def report(state: SubAgentState) -> SubAgentState:
        response = config.llm.invoke(
            [
                ("system", f"Summarize the {config.name} sub-agent outcome."),
                ("human", "\n".join(state.get("context", []))),
            ]
        )
        state["result"] = response.content
        state.setdefault("context", []).append(response.content)
        return state

    workflow.add_node("plan", plan)
    workflow.add_node("act", act)
    workflow.add_node("report", report)

    workflow.set_entry_point("plan")
    workflow.add_edge("plan", "act")
    workflow.add_edge("act", "report")
    workflow.add_edge("report", END)

    return workflow.compile()


# --- Agent Registration ----------------------------------------------------
"""Primary agent definition and utilities for invoking sub-agents."""


def build_primary_agent(llm: BaseChatModel, tools: Iterable[BaseTool]) -> AgentExecutor:
    """Create an AgentExecutor that delegates structured tasks to sub-agents."""

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are the coordinator. When appropriate, call the provided tools "
                    '"delegate_to_<name>" to request help from specialized sub-agents. "'
                    "Use the Outlook daily briefing tool when the user asks for a "
                    "combined recap of email and calendar activity from the previous "
                    "workday. "
                    "Use Outlook action tools (send, reply, forward, schedule meetings, "
                    "respond to invites) only after the user has explicitly confirmed "
                    "the intent, recipients, timing, and message contents. Always note "
                    "that confirmation in your scratchpad before acting. When the user "
                    "asks for inbox priorities or next follow-ups, call the "
                    "`outlook_follow_up_recommendations` tool and suggest whether replying "
                    "or forwarding is the best next step for each item using the provided "
                    "message IDs. Before producing a final answer, consult both the "
                    "`outlook_top_email_priorities` and `outlook_follow_up_recommendations` "
                    "tools when they are available to surface urgent Outlook follow-ups. "
                    "Your final answer must include a 'Top priorities' section populated "
                    "with the tool output (or an explicit note if none are returned) "
                    "before any other closing remarks."
                ),
            ),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
            (
                "assistant",
                "{agent_scratchpad}",
            ),
        ]
    )

    agent = create_structured_chat_agent(
        llm=llm,
        tools=list(tools),
        prompt=prompt,
        output_parser=JSONAgentOutputParser(),
    )
    return AgentExecutor(agent=agent, tools=list(tools), verbose=True)


def create_sub_agent_tool(name: str, graph) -> BaseTool:
    """Wrap a compiled LangGraph workflow as a LangChain Tool."""

    def run(task: str) -> str:
        result = graph.invoke({"task": task, "context": []})
        return result.get("result", "No result")

    return Tool(
        name=f"delegate_to_{name}",
        description=f"Delegate the task to the {name} sub-agent.",
        func=run,
    )


# --- Execution -------------------------------------------------------------
"""Entrypoint showcasing how the primary agent delegates to sub-agents."""


def run_example(query: str) -> Dict[str, Any]:
    """Execute the deep agent example and return the full agent response."""

    base_llm = initialize_llm()
    shared_tools = initialize_tools()

    research_config = SubAgentConfig(name="research", llm=base_llm, tools=shared_tools)
    writing_config = SubAgentConfig(name="writing", llm=base_llm, tools=shared_tools)

    research_graph = build_sub_agent_graph(research_config)
    writing_graph = build_sub_agent_graph(writing_config)

    delegation_tools = list(shared_tools) + [
        create_sub_agent_tool("research", research_graph),
        create_sub_agent_tool("writing", writing_graph),
    ]

    primary_agent = build_primary_agent(base_llm, delegation_tools)

    response = primary_agent.invoke(
        {"input": query, "chat_history": [HumanMessage(content=query)]}
    )
    return response


def main() -> None:
    """Run the example when executed as a script."""

    query = "Draft a short blog outline about LangGraph-based deep agents."
    result = run_example(query)
    print("\nPrimary agent response:\n", result["output"])


if __name__ == "__main__":
    main()
