To share setup state across test methods in `pytest`, the idiomatic pattern is to use **Fixtures** combined with **Scoping**.

### 1. The Core Pattern: Fixtures and Scopes
Fixtures are functions decorated with `@pytest.fixture` that provide data or state to your tests. The `scope` parameter determines how often the fixture is executed and how long its state is shared.

| Scope | Frequency | Sharing Level |
| :--- | :--- | :--- |
| **`function`** | Once per test method | **No sharing** (default) |
| **`class`** | Once per test class | Shared across all methods in the class |
| **`module`** | Once per `.py` file | Shared across all tests in the file |
| **`session`** | Once per test run | Shared across the entire project |

### 2. Sharing State Across Methods in a Class
If you have multiple test methods that need to share a heavy setup (like a database connection or an initialized object), use `scope="class"`.

```python
import pytest

class TestDatabaseFlow:
    @pytest.fixture(scope="class")
    def db_connection(self):
        # Setup: Runs once for the entire class
        conn = "Database Connection Object"
        print("\nConnecting to DB...")
        
        yield conn  # Provide the state to tests
        
        # Teardown: Runs after all methods in the class finish
        print("\nClosing DB Connection...")

    def test_step_one(self, db_connection):
        assert db_connection == "Database Connection Object"

    def test_step_two(self, db_connection):
        assert "Connection" in db_connection
```

### 3. Cleanup with `yield`
Instead of using `return`, use `yield`. Code before the `yield` is your **setup**, and code after it is your **teardown** (cleanup). This ensures resources are released correctly regardless of test outcomes.

### 4. Global Sharing with `conftest.py`
To share state across **multiple files**, define your fixtures in a file named `conftest.py` in your tests directory. `pytest` automatically discovers fixtures in this file.

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
def api_token():
    # This runs once for the whole test session
    return "secret-token-123"
```

### 5. Applying Fixtures Automatically
If a test doesn't need to access the fixture's return value but needs the setup to happen (e.g., setting an environment variable), use `@pytest.mark.usefixtures`.

```python
@pytest.mark.usefixtures("db_connection")
class TestIntegration:
    def test_logic(self):
        # db_connection setup ran, but we don't need the object here
        assert True
```

### Summary of Best Practices
*   **Prefer `function` scope** unless the setup is expensive (like a browser or database).
*   **Keep fixtures focused.** Use fixture composition (one fixture calling another) to build complex state.
*   **Use `session` scope** for global configuration or authentication tokens.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided the exact definitions and examples for fixture scoping and setup/teardown logic required to answer the user's question."/>