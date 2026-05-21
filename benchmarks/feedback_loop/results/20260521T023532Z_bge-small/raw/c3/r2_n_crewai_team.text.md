I've analyzed your request to build a multi-agent team for research, writing, and critiquing. Based on current industry standards and the available skills in your workspace, I recommend one of the following two architectural approaches:

### Option 1: CrewAI (Role-Based Orchestration)
**Best for:** Rapid development and clear persona-driven collaboration.
- **How it works:** You define "Agents" with specific roles, goals, and backstories (Researcher, Writer, Critic). You then define "Tasks" and assign them to agents. CrewAI handles the delegation and sequential flow automatically.
- **Pros:** Very intuitive; focuses on "who" does "what"; built-in support for hierarchical or sequential processes.
- **Cons:** Less granular control over the internal state machine compared to LangGraph.

### Option 2: LangGraph (Graph-Based State Management)
**Best for:** Complex feedback loops and production-grade reliability.
- **How it works:** You define a state machine (graph) where each agent is a node. The flow is explicitly defined by edges, including conditional edges (e.g., "If critic rejects, go back to writer").
- **Pros:** Full control over the workflow; explicit state management; excellent for complex "loop-back" logic.
- **Cons:** Steeper learning curve; requires more boilerplate code to set up.

### Proposed Team Structure (Applies to both)
1.  **Researcher:** Specialized in gathering facts and citing sources.
2.  **Writer:** Responsible for drafting the content based on research findings.
3.  **Critic:** Performs a quality check and provides actionable feedback.
4.  **Publisher:** A final step (or tool) to output the approved content.

Which framework would you like to use for this implementation? Once you decide, I will draft a detailed plan and create the necessary files.