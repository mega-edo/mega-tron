I have researched the available frameworks and the project structure. To build this specialized agent team, I recommend using **CrewAI**, as it is specifically designed for role-based collaboration and matches your "team" terminology perfectly.

### Proposed Strategy

I will implement a multi-agent system with three distinct roles:
1.  **The Researcher**: Gathers and synthesizes information on a given topic using search tools.
2.  **The Writer**: Transforms the research findings into a well-structured and engaging draft.
3.  **The Critique**: Reviews the draft for accuracy, tone, and completeness, providing feedback or final approval before "publishing".

**Framework Options:**
- **Option A: CrewAI (Recommended)**: Best for defining "personas" (backstory, goals) and handles delegation naturally. It uses a clean, declarative approach to define tasks and crews.
- **Option B: LangGraph**: Offers more granular control over state and explicit loops (e.g., if the Critique agent rejects the draft, it loops back to the Writer). This is better for highly complex, non-linear workflows.

**Proposed Implementation:**
- `agents.py`: Define the agents, their backstories, and goals.
- `tasks.py`: Define the sequential tasks (Research -> Write -> Critique).
- `main.py`: Orchestrate the crew and run the process.
- `requirements.txt`: Include `crewai` and any necessary search tools.

Which approach would you prefer, or do you have specific tools (e.g., search APIs) you'd like the Researcher to use?

<skill-used name="crewai" verdict="HELPFUL" reason="The skill provided the core patterns for role-based agent design and sequential task orchestration which directly aligns with the user's request."/>
<skill-used name="langgraph" verdict="HELPFUL" reason="The skill provided an alternative state-based approach for the critique loop, which is a common requirement for the requested workflow."/>