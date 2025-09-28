# Deep Agent Example Guide

This guide explains how to run the example located at
`examples/deep_agent/main.py` and customize it to explore LangChain deep agents
powered by LangGraph.

## Prerequisites

1. **Python environment** – Python 3.9 or newer is recommended.
2. **Dependencies** – Install the required packages:

   ```bash
   pip install langchain langchain-openai langgraph
   ```

3. **API keys** – Export an OpenAI compatible API key so the example can invoke
   the chat model used by the agents:

   ```bash
   export OPENAI_API_KEY="sk-your-key"
   ```

  You can substitute the model provider by editing
  [`initialize_llm`](../examples/deep_agent/main.py#L27) to use a different
   LangChain chat model wrapper.

## How the example works

The script is divided into sectioned helpers that map directly to the structure
of the code:

- **Model setup** – [`initialize_llm`](../examples/deep_agent/main.py#L27) and
  [`initialize_tools`](../examples/deep_agent/main.py#L49) configure the shared
  chat model and utility tools that every agent can access.
- **Graph definition** – [`build_sub_agent_graph`](../examples/deep_agent/main.py#L101)
  uses LangGraph to define reusable sub-agents with plan/act/report nodes.
- **Agent registration** – [`build_primary_agent`](../examples/deep_agent/main.py#L164)
  and [`create_sub_agent_tool`](../examples/deep_agent/main.py#L192) register the
  LangGraph workflows as tools that the primary `AgentExecutor` can call.
- **Execution flow** – [`run_example`](../examples/deep_agent/main.py#L210)
  shows how the coordinator agent assembles the system and invokes a task.

Each of these helpers is documented inline in the script so you can match the
narrative in this guide with the implementation details.

## Running the script

1. Ensure the prerequisites above are satisfied.
2. From the repository root, execute:

   ```bash
   python examples/deep_agent/main.py
   ```

3. The script prints the final response from the primary agent after delegating
   work to the research and writing sub-agents.

If you run into authentication errors, double-check that your `OPENAI_API_KEY`
environment variable is exported in the shell that launches the script.

## Extending the example

To add additional sub-agents:

1. Create a new `SubAgentConfig` with the desired name, tools, and model
   configuration.
2. Call [`build_sub_agent_graph`](../examples/deep_agent/main.py#L101) to compile
   a new LangGraph workflow.
3. Wrap the compiled graph with [`create_sub_agent_tool`](../examples/deep_agent/main.py#L192)
   and append the resulting tool to the list passed into
   [`build_primary_agent`](../examples/deep_agent/main.py#L164).
4. Update [`run_example`](../examples/deep_agent/main.py#L210) to include the new
   tool in `delegation_tools`.

Following this pattern keeps the control flow explicit and makes it easy to
iterate on increasingly capable deep agent systems.
