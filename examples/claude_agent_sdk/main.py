"""Interactive Claude Agent SDK example with in-process MCP tools.

This script demonstrates how to:

* define custom tools with the :mod:`claude_agent_sdk` ``@tool`` decorator
* bundle those tools into an in-process MCP server via ``create_sdk_mcp_server``
* start a streaming session with :class:`~claude_agent_sdk.client.ClaudeSDKClient`
  and inspect the structured messages that Claude emits

Prerequisites
-------------

1. Install the Claude Agent SDK dependencies::

       pip install claude-agent-sdk

2. Export an API key that has access to the Claude Code APIs::

       export ANTHROPIC_API_KEY="sk-ant-..."

Run the example with::

    python examples/claude_agent_sdk/main.py
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from textwrap import dedent
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)


@dataclass
class Forecast:
    """Small value object used by the demo tools."""

    outlook: str
    temperature_f: int

    def render(self, *, location: str) -> str:
        return (
            f"The latest forecast for {location} calls for {self.outlook} skies "
            f"around {self.temperature_f}\N{DEGREE SIGN}F."
        )


FAKE_WEATHER: dict[str, Forecast] = {
    "seattle": Forecast(outlook="light showers", temperature_f=62),
    "san francisco": Forecast(outlook="low clouds", temperature_f=58),
    "new york": Forecast(outlook="clear", temperature_f=77),
}

CITY_ACTIVITY: dict[str, list[str]] = {
    "seattle": [
        "visit the Chihuly Garden and Glass museum",
        "take the monorail to Seattle Center",
        "enjoy fresh seafood at Pike Place Market",
    ],
    "san francisco": [
        "walk along Crissy Field for Golden Gate views",
        "tour the Ferry Building marketplace",
        "ride a historic cable car through Nob Hill",
    ],
    "new york": [
        "stroll the High Line park",
        "catch a matinee on Broadway",
        "explore exhibits at the Museum of Modern Art",
    ],
}


@tool(
    name="lookup_weather",
    description="Return a concise weather report for a city.",
    input_schema={"city": str},
)
async def lookup_weather(arguments: dict[str, Any]) -> dict[str, Any]:
    """Simulate a weather API lookup for the requested city."""

    city = arguments["city"].lower()
    forecast = FAKE_WEATHER.get(city)
    if not forecast:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "No weather information is available. Try Seattle, San Francisco, "
                        "or New York."
                    ),
                }
            ],
            "is_error": True,
        }

    return {
        "content": [
            {
                "type": "text",
                "text": forecast.render(location=arguments["city"]),
            }
        ]
    }


@tool(
    name="suggest_activities",
    description="Suggest three local activities for an afternoon visit to a city.",
    input_schema={"city": str},
)
async def suggest_activities(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a curated list of activities for a given city."""

    city = arguments["city"].lower()
    ideas = CITY_ACTIVITY.get(city)
    if not ideas:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "No curated activities are available yet. Try Seattle, San Francisco, "
                        "or New York."
                    ),
                }
            ],
            "is_error": True,
        }

    formatted = "\n".join(f"- {item}" for item in ideas)
    return {
        "content": [
            {
                "type": "text",
                "text": f"Here are a few ideas:\n{formatted}",
            }
        ]
    }


def _summarize_tool_content(block: ToolResultBlock) -> str:
    """Normalize tool result payloads into a printable string."""

    if isinstance(block.content, list):
        texts = [
            item.get("text")
            for item in block.content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        joined = " ".join(text for text in texts if text)
        if joined:
            return joined
        return repr(block.content)
    if block.content:
        return str(block.content)
    return "<tool returned no content>"


async def stream_itinerary(city: str) -> None:
    """Drive a short conversation and log each structured response."""

    options = ClaudeAgentOptions(
        system_prompt=dedent(
            """
            You are an upbeat travel concierge. Use the provided tools before answering
            so that your suggestions include the latest local details.
            """
        ).strip(),
        allowed_tools=["lookup_weather", "suggest_activities"],
        mcp_servers={
            "local-guides": create_sdk_mcp_server(
                name="local-guides", tools=[lookup_weather, suggest_activities]
            )
        },
        permission_mode="default",
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            (
                "I am planning a Saturday afternoon in {city}. Please check the weather "
                "and suggest a mini itinerary."
            ).format(city=city),
        )

        print("--- Conversation transcript ---")
        async for message in client.receive_messages():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Claude: {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"→ Claude invoked tool '{block.name}' with {block.input}")
                    elif isinstance(block, ToolResultBlock):
                        print(f"← Tool result: {_summarize_tool_content(block)}")
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"(session {message.session_id} finished in {message.duration_ms} ms, cost ≈ ${cost:.4f})")
                break


def main() -> None:
    """Entry point used by ``python -m`` or direct execution."""

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "Set the ANTHROPIC_API_KEY environment variable before running this example."
        )

    city = "Seattle"
    asyncio.run(stream_itinerary(city))


if __name__ == "__main__":
    main()
