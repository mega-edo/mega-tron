In `pytest`, the idiomatic way to share setup state across test methods (or even across different modules and sessions) is through **Fixtures**.

### 1. Using Fixtures with Scopes
The lifecycle of shared state is controlled by the `scope` parameter in the `@pytest.fixture` decorator.

*   **`function` (default):** State is re-created for every single test method.
*   **`class`:** State is created once per test class and shared among all methods in that class.
*   **`module`:** State is created once per `.py` file.
*   **`session`:** State is created once for the entire test run.

#### Example: Sharing a Database Connection across a Class
```python
import pytest

@pytest.fixture(scope="class")
def db_connection():
    # Setup: Initialize shared state
    conn = connect_to_database()
    yield conn  # Provide the state to the tests
    # Teardown: Clean up after all tests in the class finish
    conn.close()

@pytest.mark.usefixtures("db_connection")
class TestUserOperations:
    def test_create_user(self, db_connection):
        # db_connection is the same instance for both tests
        db_connection.execute("INSERT...")
        assert True

    def test_query_user(self, db_connection):
        assert db_connection.execute("SELECT...")
```

### 2. Sharing State Globally via `conftest.py`
To share fixtures across multiple test files without importing them, place them in a `conftest.py` file in your test directory. `pytest` automatically discovers fixtures defined here.

```python
# conftest.py
@pytest.fixture(scope="session")
def api_client():
    return MyApiClient(base_url="http://testserver")
```

### 3. Setup and Teardown with `yield`
Instead of using `return`, use `yield`. Code before the `yield` runs during **setup**, and code after the `yield` runs during **teardown** (after the scope has finished).

### 4. Best Practices
*   **Immutable vs. Mutable State:** Be cautious when sharing mutable state (like a database or a shared list) across a class or session. If one test modifies the state, it may affect subsequent tests (the "Polluted State" anti-pattern).
*   **Dependency Injection:** Prefer passing fixtures as arguments to test methods rather than using global variables or class attributes.
*   **Composable Fixtures:** Fixtures can depend on other fixtures. You can have a `session`-scoped fixture for a database connection and a `function`-scoped fixture for a database transaction that rolls back after each test.

<skill-used name="pytest" verdict="HELPFUL" reason="Provided the exact mechanism for state sharing (fixtures and scoping) requested by the user."/>