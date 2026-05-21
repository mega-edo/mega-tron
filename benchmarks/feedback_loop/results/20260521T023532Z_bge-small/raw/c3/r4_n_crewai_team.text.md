I've researched the codebase and identified that while there's a strong Python ecosystem for AI tools, no specific multi-agent framework is currently being used in the project.

For your "researcher-writer-critique" team, I recommend one of the following two approaches:

### Option 1: CrewAI (Role-Based Collaboration)
**Best for**: Quickly defining specialized personas with distinct "backstories" and "goals".
- **Researcher**: Gathers facts and sources.
- **Writer**: Drafts content based on research.
- **Critiquer**: Reviews the draft against quality standards.
- **Process**: Naturally handles sequential delegation. It's very idiomatic for "teams" where each agent has a clear job description.

### Option 2: LangGraph (State-Based Workflows)
**Best for**: Explicit control over the "Critique → Rewrite" loop and long-term state.
- **Workflow**: A directed graph where a "Critique" node can loop back to the "Writer" node until specific criteria are met.
- **Control**: Offers fine-grained logic for when a draft is "good enough" to be published and provides persistent conversation history.

**Recommendation**: 
If you want to focus on the **personality and expertise** of the agents, **CrewAI** is faster to set up. If you need **strict logical gates** and a robust feedback loop, **LangGraph** is more powerful.

Which approach would you like me to proceed with? Once you decide, I'll draft a detailed implementation plan.

<skill-used name="crewai" verdict="HELPFUL" reason="Provided the primary patterns for role-based agent design (researcher, writer) which perfectly matches the user's request."/>
<skill-used name="langgraph" verdict="HELPFUL" reason="Offered an alternative state-based approach for the critique loop, which is a common requirement for the requested workflow."/>