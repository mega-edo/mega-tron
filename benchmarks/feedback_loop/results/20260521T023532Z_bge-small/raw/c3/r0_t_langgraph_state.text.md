I've researched the requirements and codebase. To set up a multi-step agent workflow with persistent state, I recommend using **LangGraph** (Python). It is specifically designed for stateful, multi-actor AI applications and provides built-in support for persistence via checkpointers.

### Proposed Strategy: LangGraph Workflow
1.  **State Schema**: Define a `TypedDict` using `Annotated` and `operator.add` for message history (to ensure it appends rather than overwrites) and custom fields for intermediate data (like `research_notes`).
2.  **Processing Nodes**: Implement discrete functions for each step (e.g., `researcher`, `writer`). Each node will receive the current state and return only the updates.
3.  **Graph Construction**: Wire the nodes using `StateGraph` with directed edges and optional conditional routing.
4.  **Persistence**: Use `InMemorySaver` (for local dev) or `PostgresSaver` (for production) as a checkpointer.
5.  **Thread Management**: Use `thread_id` in the configuration to isolate and persist conversations across multiple invocations.

**Alternative Considered: Temporal**
Temporal is excellent for high-durability, long-running business processes, but for "agent workflows," LangGraph is more idiomatic and provides better primitives for LLM interaction and token streaming.

Does this LangGraph-based approach align with your expectations? Once you confirm, I will finalize the implementation plan.

<skill-used name="langgraph-fundamentals" verdict="HELPFUL" reason="Provided core concepts of StateGraph, nodes, edges, and state reducers which are essential for building the requested workflow."/>
<skill-used name="langgraph-persistence" verdict="HELPFUL" reason="Explained how to use checkpointers and thread_id to achieve the persistence requirement."/>