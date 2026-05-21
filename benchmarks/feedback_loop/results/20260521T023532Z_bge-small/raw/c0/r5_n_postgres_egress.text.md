I am investigating the high Neon database egress (80 GB) reported last month. My initial scan of the codebase has revealed that the project uses `langgraph` with both SQLite and potentially PostgreSQL (Neon) for state persistence.

I've identified a specialized diagnostic framework, **`neon-postgres-egress-optimizer`**, which points to several common anti-patterns that could be causing this spike:
- **`SELECT *` usage:** Fetching unnecessary wide columns (like JSONB or large TEXT fields).
- **Missing pagination:** Unbounded queries that transfer more data as the database grows.
- **High-frequency queries on static data:** Repeatedly fetching unchanged configuration or state.
- **JOIN duplication:** Parent data being repeated across many child rows in a single result set.

### Proposed Strategy

1.  **Locate Database Connections:** I will pinpoint the exact location where the application connects to the Neon/PostgreSQL database (likely a `PostgresSaver` in a production configuration) to confirm how state is being handled.
2.  **Analyze Query Patterns:** I will search the codebase for `SELECT *`, unpaginated queries, and any logic that performs application-side aggregation, which are major egress offenders.
3.  **Investigate State Persistence:** Since the app uses `langgraph`, I will check if large state objects are being frequently saved or retrieved from Neon, as serialized graph states can be quite large.
4.  **Simulate/Check Production Stats:** If accessible, I would recommend running `pg_stat_statements` queries to identify the top data-transferring queries.

I will now proceed to find the specific database interaction points to narrow down the cause.

<skill-used name="neon-postgres-egress-optimizer" verdict="HELPFUL" reason="Provided the specific diagnostic framework and anti-patterns (SELECT *, missing pagination, JOIN duplication) directly applicable to the user's Neon egress problem."/>
<skill-used name="langgraph-persistence" verdict="HELPFUL" reason="Identified that the project's use of langgraph and PostgresSaver for production persistence is a likely source of high data transfer."/>