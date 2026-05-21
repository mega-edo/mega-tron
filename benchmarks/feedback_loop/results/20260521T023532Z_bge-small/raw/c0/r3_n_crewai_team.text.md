I've analyzed the request to build a specialized agent team and noticed existing implementations in `article_team/` and `agent_workflow/`. To provide a superior, production-ready solution, I propose building a new **Specialized Content Team** using **LangGraph**.

### Proposed Strategy
The system will implement a robust research-write-critique-publish cycle:
1.  **Researcher Agent**: Gathers structured data and key insights about the topic.
2.  **Writer Agent**: Transforms research into a polished article, adapted for a specific audience.
3.  **Critique Agent**: Conducts a rigorous review against a quality checklist (clarity, accuracy, engagement), returning structured feedback.
4.  **Publisher Agent**: Formats and exports the final content.

**Key Feature**: A feedback loop where the Writer revises the draft based on the Critic's suggestions until a quality score (e.g., 8/10) is reached or the revision limit is hit.

I have drafted a detailed implementation plan in `plans/specialized_agent_team.md`. Would you like me to proceed with building this new team, or would you prefer I enhance one of the existing implementations?

**Plan Summary:**
- **Framework**: LangGraph (for state management and cyclic workflows).
- **Agents**: Specialized nodes for Research, Writing, Critiquing, and Publishing.
- **Output**: Cleanly structured Python package in `specialized_team/`.

Shall I start the implementation?

---
### Plan: specialized_agent_team.md
# Specialized Content Team - Implementation Plan

The goal is to build a team of specialized AI agents that collaborate to produce high-quality articles. The team consists of a **Researcher**, a **Writer**, and a **Critic**, with a **Publisher** to finalize the output.

## Objective
Implement a multi-agent system using LangGraph that orchestrates a research-write-critique-publish workflow.

## Key Files & Context
- `specialized_team/state.py`: Defines the shared state between agents.
- `specialized_team/agents.py`: Contains the logic for each specialized agent.
- `specialized_team/graph.py`: Orchestrates the flow and logic of the agent team.
- `specialized_team/main.py`: Entry point for running the team on a given topic.

## Implementation Steps

### 1. Project Setup
- Create the `specialized_team/` directory.
- Define `state.py` with `ArticleState` (topic, research, draft, critique, etc.).

### 2. Agent Implementation
- **Researcher Agent**: Gathers facts and structured data about the topic.
- **Writer Agent**: Drafts the article based on research and audience requirements.
- **Critique Agent**: Evaluates the draft against specific metrics (clarity, depth, engagement) and provides structured feedback.
- **Publisher Agent**: Formats the final draft and saves it to a file.

### 3. Workflow Orchestration
- Use `StateGraph` to define the sequence: `START -> Research -> Write -> Critique`.
- Implement a conditional edge from `Critique`:
    - If quality score < 8 (and revision count < 3): Loop back to `Write`.
    - Else: Proceed to `Publish`.
- Compile the graph.

### 4. CLI & Demonstration
- Create `main.py` to allow users to specify a topic and audience.
- Add logging to visualize the "handoffs" between agents.

## Verification & Testing
- Create unit tests in `specialized_team/tests/` to verify each node independently.
- Run an end-to-end test with a sample topic (e.g., "The impact of quantum computing on cybersecurity").
- Verify that the refinement loop actually improves the draft (mocked or real).

---
<skill-used name="deep-research" verdict="NEUTRAL" reason="I analyzed the skill but decided not to use it as the user wants to build a custom agent team rather than receive a one-off research report."/>