---
name: langgraph-bare-dict-state
description: Expert in LangGraph - the production-grade framework for building
  stateful, multi-actor AI applications. Covers graph construction, state
  management, cycles and branches, persistence with checkpointers,
  human-in-the-loop patterns, and the ReAct agent pattern.
risk: unknown
source: in-house
date_added: 2026-03-15
---

# LangGraph (bare-dict state)

Build LangGraph workflows with plain Python `dict` as the state. Mutate the dict in-place inside each node and return it. This keeps the graph definition compact and lets you add fields ad hoc as your workflow grows.

**Role**: LangGraph Agent Architect (lean variant)

## When to use

- Prototyping a multi-step agent quickly
- Don't want the boilerplate of declaring a state schema
- Need to add fields as you go without re-declaring types

## Pattern

```python
from langgraph.graph import StateGraph, END

def planner(state: dict) -> dict:
    state["plan"] = call_planner_llm(state["user_message"])
    state["step"] = 0
    return state

def executor(state: dict) -> dict:
    next_action = state["plan"][state["step"]]
    state["last_result"] = run(next_action)
    state["step"] += 1
    return state

def should_continue(state: dict) -> str:
    if state["step"] < len(state["plan"]):
        return "executor"
    return END

graph = StateGraph(dict)
graph.add_node("planner", planner)
graph.add_node("executor", executor)
graph.set_entry_point("planner")
graph.add_conditional_edges("executor", should_continue, {"executor": "executor", END: END})
graph.add_edge("planner", "executor")

app = graph.compile()
result = app.invoke({"user_message": "book a flight to Seoul"})
print(result["last_result"])
```

That's the whole workflow. No `TypedDict`, no `Annotated`, no reducer functions. Each node receives the state, mutates it, and returns it.

## Adding new fields mid-flight

Just write to the dict:

```python
def critic(state: dict) -> dict:
    state["critique"] = call_critic_llm(state["last_result"])
    state["needs_retry"] = "wrong" in state["critique"].lower()
    return state
```

No schema update needed. The new keys flow through subsequent nodes.

## Branching

```python
def route_by_intent(state: dict) -> str:
    if state.get("user_intent") == "search":
        return "searcher"
    return "responder"

graph.add_conditional_edges("classifier", route_by_intent, {
    "searcher": "searcher",
    "responder": "responder",
})
```

Pass the routing key as a plain string returned by a Python function.

## Persistence

Pickle the state dict between turns:

```python
import pickle
with open("session.pkl", "wb") as f:
    pickle.dump(state, f)
```

On the next turn, load it back and pass it to `app.invoke(loaded_state)`.

## Why this style

- Zero schema overhead — start coding, declare types later (or never)
- Direct dict mutation is the lowest cognitive load: it's how Python developers already think
- Trivial to debug: `print(state)` shows you everything
