# Deep Agent Example Guide

This guide explains how to run the example located at
`examples/deep_agent/main.py` and customize it to explore LangChain deep agents
powered by LangGraph.

## Prerequisites

1. **Python environment** – Python 3.9 or newer is recommended.
2. **Dependencies** – Install the required packages:

   ```bash
   pip install langchain langchain-openai langgraph msal requests \
       google-api-python-client google-auth google-auth-oauthlib
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
   activity. Optionally, set `OUTLOOK_PRIORITY_SENDERS` to a comma-separated
   list of prioritized email addresses (or `@domain.com` domain rules). You can
   assign weights with `address:weight` pairs (for example,
   `ceo@example.com:5,@executive.example.com:4`). These rules feed the
   `outlook_top_email_priorities` tool that highlights urgent follow-ups in the
   agent's final response.

5. **Google Drive access** – Choose one of the following authentication flows
   so the example can call the Drive metadata APIs:

   - **Service account (preferred for corporate environments)** – Create a
     service account in the [Google Cloud console](https://console.cloud.google.com/)
     and grant it domain-wide delegation for the Drive scopes listed below.
     Download the JSON key file and export the path:

       ```bash
       export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/key.json"
       ```

     If you are using domain-wide delegation, also export the primary email
     address to impersonate:

       ```bash
       export GOOGLE_DRIVE_DELEGATED_USER="user@example.com"
       ```

   - **OAuth 2.0 user credentials** – Create an OAuth client ID (type "Desktop"
     or "Web") in the Google Cloud console. Use the client ID/secret to obtain
     an authorized user token JSON containing a refresh token that grants the
     `https://www.googleapis.com/auth/drive.metadata.readonly` scope. Save the
     token JSON to disk and export its location:

       ```bash
       export GOOGLE_DRIVE_TOKEN_PATH="/absolute/path/to/token.json"
       ```

     Alternatively, you can place the JSON payload directly in the
     `GOOGLE_DRIVE_TOKEN_JSON` environment variable.

   Regardless of the chosen flow, ensure the credential has at least the
   `https://www.googleapis.com/auth/drive.metadata.readonly` scope enabled so it
   can list files and fetch metadata. These values are consumed by
   [`create_google_drive_tools`](../integrations/google_drive.py), which
   registers Drive helpers in the example agent.

## How the example works

The script is divided into sectioned helpers that map directly to the structure
of the code:

- **Model setup** – [`initialize_llm`](../examples/deep_agent/main.py#L27) and
  [`initialize_tools`](../examples/deep_agent/main.py#L49) configure the shared
  chat model and integration-driven tools (Outlook, Google Drive) that every
  agent can access.
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
   work to the research and writing sub-agents. When the Outlook and Google
   Drive integrations are configured, the agent can call tools that summarize
   the previous workday's emails, calendar events, and recent Drive activity.

If you run into authentication errors, double-check that the `OPENAI_API_KEY`,
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`, and the appropriate
Google credential environment variables are exported in the shell that launches
the script. The script will raise a descriptive error if any integration is
missing credentials.

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
