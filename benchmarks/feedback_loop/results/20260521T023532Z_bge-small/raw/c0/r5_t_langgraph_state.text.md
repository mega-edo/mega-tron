I have investigated the existing codebase and found two examples of agent workflows: `agent_workflow` and `article_team`. The `agent_workflow` already implements a multi-step process with state persistence using `SqliteSaver` and a human-in-the-loop pattern.

To fulfill your request, I propose setting up a new, clean multi-step agent workflow called **`task_orchestrator`**. This workflow will demonstrate:
1.  **State Management**: Using `TypedDict` with `Annotated` reducers to handle complex state transitions.
2.  **Multi-step Routing**: A flow involving a `Planner`, an `Executor`, and a `QA` agent.
3.  **Persistence**: Using `SqliteSaver` to ensure state is saved between nodes and can be resumed across sessions.
4.  **Conditional Logic**: The `QA` agent will decide whether to finish or send the task back for revision.
5.  **Verification**: A dedicated test script to prove that state persists even if the process is interrupted.

### Proposed Workflow Structure:
-   **Planner Node**: Breaks down a complex goal into specific tasks.
-   **Executor Node**: Simulates task execution (or calls an LLM to do so).
-   **QA Node**: Validates the output and updates the state with success or failure.
-   **Persistence**: A SQLite database (`orchestrator.db`) will store the checkpoints.

Does this strategy meet your requirements, or would you like me to modify an existing workflow instead?

I have successfully set up the `task_orchestrator` workflow, a multi-step agent system with state persistence between nodes.

### Implementation Highlights:
1.  **State Schema (`task_orchestrator/state.py`)**: Defined a `TaskState` using `TypedDict` that tracks the goal, plan, iteration count, and accumulated results. Used `Annotated` with `operator.add` to ensure results from multiple execution steps are preserved and concatenated.
2.  **Modular Nodes (`task_orchestrator/nodes.py`)**:
    -   `planner_node`: Generates a multi-step execution plan.
    -   `executor_node`: Processes steps sequentially, maintaining the current iteration state.
    -   `qa_node`: Evaluates progress and controls the loop back to execution or completion.
3.  **Persistent Graph (`task_orchestrator/graph.py`)**: Constructed a `StateGraph` integrated with `SqliteSaver`. I implemented a context manager pattern (`get_orchestrator_app`) to robustly manage the SQLite connection lifecycle, ensuring that checkpointer state is correctly flushed and retrieved.
4.  **Demonstration & Verification**:
    -   **`main.py`**: A CLI entry point that demonstrates starting a new task and resuming an existing one from the same thread.
    -   **`test_persistence.py`**: An automated test that simulates a workflow interruption after the "planned" stage, creates a new graph instance, and verifies that the state (goal, plan, status) is perfectly restored from the database before completing the remaining steps.

### Verification Results:
-   The automated test `test_persistence.py` passed, confirming that state survives graph re-instantiation.
-   The `main.py` demo successfully executed a 3-step plan, with the final state correctly accumulating results and marking the status as "finished".
-   A second run of `main.py` correctly identified the existing "finished" state for the same thread, demonstrating successful long-term persistence.

The workflow is now ready for production-grade agent tasks where reliability and state recovery are critical.

<skill-used name="langgraph" verdict="HELPFUL" reason="I used the LangGraph StateGraph, SqliteSaver, and checkpointing patterns recommended by this skill to implement the persistent workflow."/>