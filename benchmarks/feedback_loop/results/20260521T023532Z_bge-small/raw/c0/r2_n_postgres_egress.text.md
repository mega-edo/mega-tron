I have investigated the codebase and identified the likely cause of the 80 GB egress on your Neon database.

### Initial Findings
The application uses **LangGraph** for multi-agent workflows. In the current codebase, persistence is handled by a local SQLite checkpointer (`state.db`). However, the reported 80 GB egress strongly suggests that in your production environment, this has been switched to a **Postgres checkpointer** (like `PostgresSaver`) pointing to Neon.

### The Root Cause: State Serialization Overhead
LangGraph checkpointers save the **entire state** of the graph to the database at every "super-step" (every time a node finishes execution). 

Looking at your state definitions in `agent_workflow/state.py` and `article_team/state.py`, you are storing large strings:
*   `research_notes` / `research_data`: Detailed factual notes and summaries.
*   `draft`: The full text of the article being written/revised.
*   `history` / `steps`: A growing list of every action taken.

**Why this costs 80 GB:**
1.  **Redundant Writes:** If a workflow has 10 steps (Research -> Write -> Critique -> Revise -> ...), the full `research_notes` and the evolving `draft` are written to the database **10 times**. 
2.  **Redundant Reads:** Every time a node starts, it reads the full state from the database.
3.  **Growth over time:** As the `history` list grows, the "payload" size increases with every step.
4.  **No Column Selection:** Postgres checkpointers typically perform `SELECT *` and `INSERT` on a single `checkpoints` table containing the serialized state (often in a `BYTEA` or `JSONB` column). 

If a single research note + draft is 100 KB, and a workflow takes 10 turns, you've transferred 2 MB for one article. If you run 1,000 such workflows a month, that's 2 GB. If your research data is larger (e.g., 5 MB of raw text), it scales rapidly to the 80 GB you are seeing.

### Proposed Strategy

1.  **Prune the State:** Only store what is absolutely necessary for the *next* node to function.
2.  **Use External Storage for Large Blobs:** Store the large `research_notes` and `draft` in a separate table (or S3) and only store a **reference ID** in the LangGraph state.
3.  **Trim History:** Instead of a growing list of strings, store a summary or move the history to a dedicated logs table.
4.  **Checkpointer Optimization:** If possible, use a more efficient serialization format or a checkpointer that supports partial updates (though most LangGraph checkpointers are currently monolithic).

### Next Steps
I will now create a formal plan to refactor the state management to reduce database egress. I'll focus on `agent_workflow/state.py` and `article_team/state.py` to identify fields that can be moved out of the persistent state.

<skill-used name="neon-postgres-egress-optimizer" verdict="HELPFUL" reason="The skill's diagnostic framework for identifying large data transfers (SELECT *) and JOIN duplication perfectly matched the LangGraph checkpointer pattern found in the code."/>