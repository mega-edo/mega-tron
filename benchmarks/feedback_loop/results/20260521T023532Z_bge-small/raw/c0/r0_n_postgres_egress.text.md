I've completed my initial investigation. While the codebase currently uses **SQLite** for local development (as seen in `agent_workflow/graph.py` and `article_team/main.py`), your report of **80 GB of Neon egress** indicates that the production environment is likely using a remote Postgres backend via LangGraph's `PostgresSaver`.

Based on the `neon-postgres-egress-optimizer` skill, I've identified several potential "egress leaks" in your LangGraph workflows:

1.  **State Bloat:** LangGraph saves the entire state at every step. If your `AgentState` contains large objects (e.g., full research papers, multi-megabyte JSONs) and the workflow has many nodes/revisions, each save operation transfers that bulk data to Neon.
2.  **Unbounded Collections:** If the `steps` list or `history` (in `ArticleState`) grows indefinitely without truncation, the egress per step will increase linearly as the workflow progresses.
3.  **Checkpointer Retrieval:** Retrieving the full state to resume a workflow (common in Human-in-the-Loop nodes like `human_review_node`) can fetch significant data if the state is not optimized.

### Proposed Strategy

I recommend a two-phase diagnostic approach:

**Phase 1: Database-Level Diagnostics**
We should use `pg_stat_statements` on your Neon console to identify the heaviest queries. Specifically:
*   Identify queries returning the most total rows/data.
*   Check if `upsert` operations (used by LangGraph savers) are transferring excessively large state blobs.

**Phase 2: Code-Level Optimization**
I will audit the `AgentState` and `ArticleState` definitions to:
*   Identify and prune unnecessary large fields.
*   Implement "shallow" state where large data is stored in S3/Blob storage and only references are kept in the DB.
*   Ensure lists (like `steps`) use `Annotated` with custom reducers that limit size or keep only recent entries.

**Would you like me to proceed with drafting a detailed implementation plan for these optimizations?**

<skill-used name="neon-postgres-egress-optimizer" verdict="HELPFUL" reason="Provided the diagnostic framework and anti-patterns needed to address the 80GB egress issue in a Postgres/Neon context."/>