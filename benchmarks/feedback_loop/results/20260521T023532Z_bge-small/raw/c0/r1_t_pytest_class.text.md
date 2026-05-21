To share setup state across `pytest` test methods, you should use **Fixtures with explicit scopes**. This is the idiomatic way to handle shared state while maintaining test isolation and clarity.

### 1. Shared State with Scoped Fixtures
By default, fixtures have a `function` scope (they run for every test). To share state across multiple tests in a class, module, or session, you adjust the `scope` parameter.

| Scope | Lifetime | Typical Use Case |
| :--- | :--- | :--- |
| `function` | Once per test method | Fresh data, mocks |
| `class` | Once per test class | Heavy object initialization |
| `module` | Once per `.py` file | Database connections |
| `session` | Once per test suite run | Global configuration, Docker containers |

### 2. Implementation Pattern

```python
import pytest

# 1. Define the shared resource in a fixture
@pytest.fixture(scope="class")
def shared_resource():
    print("\n--- Setup: Initializing expensive resource ---")
    resource = {"data": [], "connection": "active"}
    
    yield resource  # 2. Provide the resource to tests
    
    # 3. Teardown logic runs after all tests in scope finish
    print("\n--- Teardown: Cleaning up resource ---")
    resource["data"] = []

class TestFeature:
    def test_step_one(self, shared_resource):
        shared_resource["data"].append("A")
        assert "A" in shared_resource["data"]

    def test_step_two(self, shared_resource):
        # State persists between these two tests because of 'class' scope
        shared_resource["data"].append("B")
        assert len(shared_resource["data"]) == 2
```

### 3. Best Practices for Shared State
*   **Use `yield` for Teardown:** Always use `yield` instead of `return` if you need to clean up resources (closing DB connections, deleting temp files) after the tests finish.
*   **Prefer `conftest.py` for Global State:** If you want to share state across multiple test files, define your fixtures in a file named `conftest.py` in your root or test directory. `pytest` discovers these automatically.
*   **Avoid Side Effects:** While scopes allow state sharing, try to keep tests as independent as possible. If `test_A` modifies shared state in a way that breaks `test_B`, it can make debugging difficult.
*   **Requesting Multiple Fixtures:** You can request multiple fixtures by adding them as arguments to your test function: `def test_example(db, cache, config):`.

### 4. Running and Verification
To see your setup/teardown logs while running:
```bash
pytest -s  # -s allows print statements to show in the console
```

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scopes (function, class, module, session) and setup/teardown patterns which directly answered the user's inquiry."/>