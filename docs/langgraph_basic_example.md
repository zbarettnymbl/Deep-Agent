# LangGraph Basic Example

The script at [`examples/langgraph_basic/main.py`](../examples/langgraph_basic/main.py)
illustrates the smallest useful LangGraph workflow:

1. A typed state dictionary keeps track of the incoming query, scratchpad
   updates, and final result so each node shares a predictable structure.
2. Inline Python callables are registered as tools. They do not require network
   access and highlight how pure functions can be wrapped with lightweight
   metadata.
3. A single agent node decides which tool to use, invokes it, and appends
   human-readable traces to the scratchpad.
4. The workflow is wired together with `StateGraph`, compiled, and executed via
   a simple CLI helper.

Run the example to observe the trace:

```bash
python examples/langgraph_basic/main.py
```

Compared with the [Deep Agent example](deep_agent_example.md), this workflow
remains entirely within LangGraph—no `AgentExecutor` orchestrator. The deeper
example layers an `AgentExecutor` on top so a coordinator LLM can delegate work
across multiple LangGraph sub-agents.
