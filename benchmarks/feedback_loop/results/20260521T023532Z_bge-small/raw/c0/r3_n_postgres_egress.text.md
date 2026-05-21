I am currently in **Plan Mode** and cannot execute scripts or modify source code directly. My goal is to investigate the high egress issue and propose a plan for you to approve.

### Research Findings

1.  **LangGraph Persistence:** The project uses `langgraph` with a local SQLite checkpointer (`state.db`). The `state.db-wal` file is **1.5 MB**, which is significant compared to the 4 KB base file, indicating active state updates.
2.  **Data Bloat Potential:** In `agent_workflow/state.py`, the `AgentState` includes large fields like `research_notes`, `draft`, and `final_content`. Additionally, the `steps` field is `Annotated[list[str], operator.add]`, meaning it grows indefinitely with every turn in a thread.
3.  **Neon Connection:** While no hardcoded Neon credentials exist in the codebase, the high egress (80 GB) suggests that in the production environment (e.g., Vercel), the `SqliteSaver` is likely replaced by a `PostgresSaver` pointing to Neon via environment variables.
4.  **The "Egress Trap":** LangGraph saves the *entire* state object at every checkpoint. If your workflow has many steps and each step appends to a list or keeps large strings (like full research papers) in the state, each database write becomes exponentially larger, quickly consuming network egress.

### Proposed Strategy

I will create an implementation plan to optimize the data storage and retrieval patterns.

1.  **State Optimization:** Refactor `AgentState` to avoid redundant large data storage. Instead of storing full research notes in every checkpoint, we can store only deltas or move large blobs to an external storage (like S3/GCS) and store only references in the DB.
2.  **Trim History:** Implement a mechanism to prune the `steps` and `history` lists in `AgentState` to prevent unbounded growth.
3.  **Database Diagnostics:** I will provide a script to run in your production environment (where Neon is connected) to identify the specific tables and rows causing the highest egress using `pg_stat_statements` or Neon-specific metrics.

I will now draft the formal plan in the `plans/` directory.

---

**Informal Agreement Request:**
Does this strategy of optimizing the LangGraph state structure and pruning history to reduce checkpoint size sound correct to you? If so, I will finalize the plan.

<skill-used name="neon-postgres-egress-optimizer" verdict="NEUTRAL" reason="The skill provided general guidance on egress but the investigation is currently focused on LangGraph state persistence which isn't specifically covered in the skill's examples."/>