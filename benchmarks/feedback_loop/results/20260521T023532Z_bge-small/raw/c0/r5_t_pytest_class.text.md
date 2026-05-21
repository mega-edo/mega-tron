To share setup state across `pytest` test methods idiomatically, you use **Fixtures** with the appropriate **Scope**. 

In `pytest`, a fixture is a function that provides data, setup, or state to your tests. By adjusting its `scope`, you control how often the setup runs and how long the state persists.

### 1. Sharing State within a Test Class (`scope="class"`)
If you want a setup to run only once for all methods in a class, use the `class` scope.

```python
import pytest

class TestDatabaseFlow:
    @pytest.fixture(scope="class", autouse=True)
    def db_connection(self):
        # Setup: Runs once before the first test in this class
        conn = "Connected to DB"
        print("\n[Setup] Database connection established")
        
        yield conn  # Provide the state to tests
        
        # Teardown: Runs once after the last test in this class
        print("\n[Teardown] Database connection closed")

    def test_insert(self):
        assert True  # Uses the shared connection

    def test_query(self):
        assert True  # Uses the same connection
```

### 2. Sharing State across the Entire Session (`scope="session"`)
For expensive setups (like starting a web server or connecting to a remote API) that should persist for all tests, use the `session` scope. These are typically placed in a `conftest.py` file.

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
def api_client():
    client = "Heavy API Client"
    yield client
    # Teardown happens at the very end of the test run
```

### 3. Summary of Available Scopes
| Scope | Lifetime |
| :--- | :--- |
| `function` (default) | Runs for every single test method. |
| `class` | Runs once per test class. |
| `module` | Runs once per `.py` file. |
| `package` | Runs once per directory/package. |
| `session` | Runs once for the entire test execution. |

### 4. Important Considerations
*   **Immutability:** If a test modifies a shared state (e.g., a shared list or database record), it can cause "test leakage" where one test's failure depends on another. Only share **read-only** state or ensure each test performs its own cleanup/isolation.
*   **`autouse=True`:** Use this if you want the fixture to run automatically without explicitly passing it as an argument to every test method.
*   **`yield` vs `return`:** Use `yield` when you need to perform **teardown** (cleanup) logic after the tests are finished.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided the foundational knowledge of pytest fixtures and scoping, which is the direct answer to sharing state idiomatically."/>