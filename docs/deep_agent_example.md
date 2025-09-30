# Deep Agent Example Guide

This guide explains how to run the example located at
`examples/deep_agent/main.py` and customize it to explore LangChain deep agents
powered by LangGraph.

## Prerequisites

1. **Python environment** – Python 3.9 or newer is recommended.
2. **Dependencies** – Install the required packages:

   ```bash
   pip install langchain langchain-openai langgraph msal requests
   ```

3. **API keys** – Export an OpenAI compatible API key so the example can invoke
   the chat model used by the agents:

   ```bash
   export OPENAI_API_KEY="sk-your-key"
   ```

  You can substitute the model provider by editing
  [`initialize_llm`](../examples/deep_agent/main.py#L27) to use a different
   LangChain chat model wrapper.

4. **Microsoft Graph access** – Register a "web" app in the
   [Azure portal](https://portal.azure.com/) and grant it the following
   application permissions under Microsoft Graph:

   - `Mail.Read`
   - `Calendars.Read`

   After granting admin consent, copy the app's **Client ID**, **Tenant ID**,
   and **Client secret**. Export them as environment variables so the example
   can authenticate through MSAL:

   ```bash
   export AZURE_CLIENT_ID="<app-client-id>"
   export AZURE_TENANT_ID="<directory-tenant-id>"
   export AZURE_CLIENT_SECRET="<client-secret>"
   ```

   These values are consumed by
   [`create_outlook_tools`](../integrations/outlook.py), which builds the
   LangChain tools that summarize the previous workday's email and calendar
   activity. The Outlook integration also exposes a
   `outlook_top_priority_emails` tool that scores messages using importance
   flags, sender rules, and due dates so the agent can spotlight urgent emails.

## How the example works

The script is divided into sectioned helpers that map directly to the structure
of the code:

- **Model setup** – [`initialize_llm`](../examples/deep_agent/main.py#L27) and
  [`initialize_tools`](../examples/deep_agent/main.py#L49) configure the shared
  chat model and Outlook-derived tools that every agent can access.
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
   work to the research and writing sub-agents. When the Outlook integration is
   configured, the agent can call tools that summarize the previous workday's
   emails and calendar events. The final response now includes a **Top priorities**
   section populated from the prioritization tool so you can immediately review
   urgent messages.

If you run into authentication errors, double-check that the `OPENAI_API_KEY`,
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_CLIENT_SECRET` environment
variables are exported in the shell that launches the script. The script will
raise a descriptive error if the Outlook integration is missing credentials.

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
