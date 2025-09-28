# Deep-Agent

This repository hosts examples that explore deep agent patterns using LangChain
and LangGraph. The highlighted example demonstrates how a primary
`AgentExecutor` can delegate work to specialized sub-agents implemented as
LangGraph workflows.

## Getting started

1. Install dependencies and configure an OpenAI compatible API key as described
   in the [Deep Agent Example Guide](docs/deep_agent_example.md#prerequisites).
2. Review the annotated script at
   [`examples/deep_agent/main.py`](examples/deep_agent/main.py) to understand how
   language models, tools, and graphs are orchestrated.
3. Run the example with:

   ```bash
   python examples/deep_agent/main.py
   ```

   The script prints the coordinator's response after delegating to research and
   writing sub-agents.

## Learn more

- The [Deep Agent Example Guide](docs/deep_agent_example.md) explains the code
  structure and shows how to extend the system with additional sub-agents.
- The LangChain deep agents overview and blog post provide conceptual context:
  - https://docs.langchain.com/labs/deep-agents/overview
  - https://blog.langchain.com/deep-agents/
