I have analyzed the existing codebase and found two multi-agent implementations (`article_team` and `agent_workflow`) that follow a similar "Research-Write-Critique" pattern.

To fulfill your request, I propose building a **new, refined agent team** in a dedicated directory `expert_writer_team/`. This implementation will be optimized for high-quality content generation with the following features:

1.  **Specialized Agent Roles**:
    *   **Researcher**: Deep-dives into the topic to generate a structured knowledge base.
    *   **Writer**: Crafting professional-grade content based on the research.
    *   **Critique (Editor)**: Providing structured feedback with a focus on technical accuracy and clarity.
2.  **Iterative Refinement Loop**: A robust feedback loop that allows the Writer to improve the draft based on the Critic's suggestions until a quality threshold is met.
3.  **Final Publishing**: Automatically formatting and saving the approved content to the `output/` directory.
4.  **Modern Stack**: Using `LangGraph` for workflow management and `Pydantic` for structured output from the critic.

### Proposed Strategy

*   **State Management**: Define a comprehensive state that tracks the topic, research findings, draft revisions, and structured feedback.
*   **Workflow Design**:
    *   `START` -> `Researcher` -> `Writer` -> `Critique`.
    *   Conditional edge from `Critique`: If approved, move to `Publish`; if rejected, move back to `Writer` with feedback.
*   **Safety**: Implement a revision limit to prevent infinite loops.

Does this strategy meet your requirements, or would you like me to add more specialized roles (e.g., a Fact Checker or an SEO Optimizer)?