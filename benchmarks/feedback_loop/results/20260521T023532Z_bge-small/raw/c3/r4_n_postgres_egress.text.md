I have completed my investigation into the high Neon database egress.

### Findings
The workspace does not contain a single large application, but rather a collection of **agent skills** and **SQL templates** in the `.gemini/skills` directory. I have identified the likely source of the **80 GB egress** in the following location:

1.  **Inefficient SQL Templates**: The file `.gemini/skills/neon-vercel-postgres/templates/neon-basic-queries.ts` contains several unpaginated `SELECT *` patterns. If an automated agent or your application is using these templates, their data transfer costs will grow exponentially as your database grows.
    *   **Unbounded Queries**: Functions like `getUsers` and `getPostsWithAuthors` (lines 51, 293, 321) lack `LIMIT` or pagination in several branches.
    *   **Over-fetching**: Frequent use of `SELECT *` and `RETURNING *` fetches all columns (including large ones like `description` or `content`) even when only a subset is likely needed.
2.  **Diagnostic Capability**: The workspace already includes a specialized skill, `neon-postgres-egress-optimizer`, specifically designed to solve this problem. It confirms that "barely changed" apps often see egress spikes due to **data growth** (e.g., a query that was cheap with 100 rows becomes expensive with 10,000 rows).

### Proposed Strategy

I recommend a two-phase approach to resolve this:

**Phase 1: Diagnosis (Immediate)**
Since I cannot access your live database, you should run the diagnostic queries from the `neon-postgres-egress-optimizer` skill directly on your Neon console. These use the `pg_stat_statements` extension to pinpoint exactly which queries are transferring the most data.

**Phase 2: Fixing Templates and Application Code**
Once the "worst offenders" are identified, we should update the templates and any derived application code to:
*   Replace `SELECT *` with specific column lists.
*   Enforce mandatory `LIMIT` and pagination on all list endpoints.
*   Push aggregations to the database instead of fetching raw data.

**Would you like me to draft a formal implementation plan to update the `.gemini/skills` templates with these best practices?** Additionally, I can provide the specific SQL queries you need to run on your Neon console right now.

<skill-used name="neon-postgres-egress-optimizer" verdict="HELPFUL" reason="The skill provided the exact diagnostic SQL queries and identified common egress anti-patterns like SELECT * and missing pagination."/>