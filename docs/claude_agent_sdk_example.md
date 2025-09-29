# Claude Agent SDK Travel Concierge Example

This guide explains how to run the `examples/claude_agent_sdk/main.py` script, which
creates a small travel concierge powered by the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview).
The script showcases in-process MCP tools, the streaming client, and how to inspect
Claude's structured responses.

## Prerequisites

1. **Install dependencies**

   ```bash
   pip install claude-agent-sdk
   ```

   The SDK pulls in `anyio`, the Model Context Protocol (`mcp`), and additional runtime
   utilities automatically.

2. **Configure authentication**

   Export an API key that can access the Claude Code endpoints:

   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```

   The SDK will read this variable when it spawns the Claude Code CLI under the hood.

## What the example does

* Registers two lightweight tools—`lookup_weather` and `suggest_activities`—using the
  SDK's `@tool` decorator.
* Groups those tools into an in-process MCP server by calling `create_sdk_mcp_server`.
  Because the server runs in-process, the tools can access native Python state without
  additional IPC setup.
* Starts an interactive session with `ClaudeSDKClient` in streaming mode, sends an
  itinerary request, and prints each structured message as it arrives. The script logs
  tool invocations, tool results, regular assistant text, and a final `ResultMessage`
  containing cost metadata.

## Running the script

With prerequisites met, launch the demo from the repository root:

```bash
python examples/claude_agent_sdk/main.py
```

You should see a transcript similar to:

```
--- Conversation transcript ---
→ Claude invoked tool 'lookup_weather' with {'city': 'Seattle'}
← Tool result: The latest forecast for Seattle calls for light showers skies around 62°F.
→ Claude invoked tool 'suggest_activities' with {'city': 'Seattle'}
← Tool result: Here are a few ideas:
- visit the Chihuly Garden and Glass museum
- take the monorail to Seattle Center
- enjoy fresh seafood at Pike Place Market
Claude: Here's a rainy-day friendly plan for Saturday afternoon in Seattle...
(session default finished in 4312 ms, cost ≈ $0.0075)
```

Exact responses vary depending on the model revision, but the structure of the
output—tool calls followed by a final reply—remains the same.
