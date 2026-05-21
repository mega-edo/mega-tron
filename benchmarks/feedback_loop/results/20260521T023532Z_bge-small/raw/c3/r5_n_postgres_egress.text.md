The investigation into the 80 GB Neon database egress spike will follow the structured approach recommended by the `neon-postgres-egress-optimizer` skill. Since the current workspace lacks application-side query logic, the primary focus will be on diagnostic SQL queries that the user can run directly on their Neon database to identify the specific queries responsible for the high data transfer.

### Proposed Strategy

1.  **Diagnose via `pg_stat_statements`**:
    *   Verify if the `pg_stat_statements` extension is active.
    *   Run diagnostic queries to rank SQL statements by total rows returned and average rows per call.
    *   Identify queries returning large amounts of data (e.g., those with `JSONB`, `TEXT`, or `BYTEA` columns).

2.  **Analyze Application-Side Egress Patterns**:
    *   Look for common anti-patterns such as `SELECT *` (fetching unnecessary wide columns).
    *   Identify list endpoints missing `LIMIT` or pagination.
    *   Check for "N+1" query patterns or duplicated data in large `JOIN` results.
    *   Identify application-side aggregation where entire tables are fetched to compute a simple sum or count.

3.  **Cross-Reference with Traffic Logs**:
    *   Use the existing `extract_logs.py` utility to analyze Nginx access logs (if available).
    *   Correlate high-egress database queries with specific application request IDs to identify the triggering features or endpoints.

4.  **Implementation of Fixes**:
    *   Optimize identified queries (e.g., select only required columns, add pagination).
    *   Push aggregations to the database layer.
    *   Implement caching for frequently fetched, slow-changing data.

I will now draft the implementation plan.

<skill-used name="neon-postgres-egress-optimizer" verdict="HELPFUL" reason="The skill provided the exact diagnostic SQL queries and identified common egress anti-patterns needed to solve the user's task."/>
<skill-used name="neon-vercel-postgres" verdict="NEUTRAL" reason="While relevant to Neon, this skill focused more on setup and connection pooling rather than egress optimization."/>
<skill-used name="distributed-tracing" verdict="NEUTRAL" reason="Relevant for debugging performance, but not as direct as the specialized egress optimizer for this specific task."/>
<skill-used name="native-data-fetching" verdict="NEUTRAL" reason="Focused on frontend fetching patterns, not database egress."/>

---
**Plan: Investigation of High Neon Database Egress**

**Objective**
Identify and mitigate the source of the 80 GB egress spike in the Neon database.

**Key Files & Context**
*   `extract_logs.py`: Utility for parsing Nginx logs to correlate web traffic with database load.
*   `neon-postgres-egress-optimizer`: Specialized guidance for identifying expensive Postgres queries.

**Investigation Steps**
1.  **Run Egress Diagnostic Queries** (User-executed on Neon):
    *   Check extension: `SELECT 1 FROM pg_stat_statements LIMIT 1;`
    *   Identify top rows-per-call queries:
        ```sql
        SELECT query, calls, rows / calls AS avg_rows_per_call 
        FROM pg_stat_statements 
        ORDER BY avg_rows_per_call DESC LIMIT 10;
        ```
    *   Identify total data transfer contributors:
        ```sql
        SELECT query, calls, total_exec_time, rows 
        FROM pg_stat_statements 
        ORDER BY rows DESC LIMIT 10;
        ```
2.  **Correlate with Logs**:
    *   Run `python3 extract_logs.py --format json <access.log>` on application logs.
    *   Identify endpoints with high `bytes` sent to clients, which often correlates with high DB egress.
3.  **Audit Code for Anti-Patterns**:
    *   Search for `SELECT *` in any newly added or modified application code.
    *   Check list endpoints for missing `OFFSET`/`LIMIT`.
    *   Look for `.map()`, `.filter()`, or `.reduce()` on large database result sets in application code.

**Verification & Testing**
*   Reset stats: `SELECT pg_stat_statements_reset();`
*   Monitor egress for 24 hours after applying fixes to confirm the reduction in data transfer.

I will proceed to write this plan to the plans directory.I've analyzed the workspace and identified that the 80 GB egress spike is likely due to application-side query patterns that fetch more data than necessary. Since I don't have direct access to your live Neon database or the core application code in this environment, I've developed an investigation strategy based on the `neon-postgres-egress-optimizer` skill.

### Proposed Strategy

1.  **Database Diagnostics**: I'll provide you with SQL queries to run on your Neon console using `pg_stat_statements`. These will identify exactly which queries are transferring the most data.
2.  **Code Audit**: Once we identify the heavy-hitter queries, I'll audit the codebase (or any code you share) for egress-heavy patterns like `SELECT *`, missing pagination, or application-side aggregation.
3.  **Log Correlation**: I can use the existing `extract_logs.py` to analyze your Nginx logs and correlate high-traffic endpoints with the database egress spikes.

I have drafted a detailed plan in `plans/neon-egress-investigation.md` that covers these phases. 

**Does this strategy sound correct to you?** If so, please run the diagnostic queries listed in the plan and provide the top results so we can pinpoint the culprit.

<skill-used name="neon-postgres-egress-optimizer" verdict="HELPFUL" reason="The skill provided the exact diagnostic SQL queries and identified common egress anti-patterns needed to solve the user's task."/>