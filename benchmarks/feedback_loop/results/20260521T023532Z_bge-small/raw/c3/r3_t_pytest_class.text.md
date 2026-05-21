In **pytest**, the idiomatic way to share setup state across test methods is by using **fixtures** with appropriate **scopes**.

### 1. The Core Pattern: Fixtures
Fixtures are functions decorated with `@pytest.fixture` that provide a fixed baseline for tests. Instead of using `setUp` methods like in `unittest`, you define a fixture and inject it into your test methods as an argument.

### 2. Controlling State Lifecycle with Scopes
The "scope" of a fixture determines how often it is executed and how long the state is preserved:

*   **`function` (default):** Setup runs for every single test method.
*   **`class`:** Setup runs once per test class.
*   **`module`:** Setup runs once per `.py` file.
*   **`session`:** Setup runs once for the entire test run.

```python
import pytest

@pytest.fixture(scope="class")
def db_connection():
    # Setup: Initialize shared state (e.g., a DB connection)
    conn = create_expensive_connection()
    yield conn
    # Teardown: Clean up after all tests in the class finish
    conn.close()

class TestDatabaseLogic:
    def test_query_a(self, db_connection):
        assert db_connection.query("SELECT 1") == 1

    def test_query_b(self, db_connection):
        # Uses the EXACT SAME connection instance as test_query_a
        assert db_connection.is_active
```

### 3. Setup and Teardown with `yield`
The `yield` keyword is the standard way to handle teardown. Everything before `yield` is the **setup**, and everything after is the **teardown** (cleanup).

### 4. Sharing State Across Files (`conftest.py`)
If you need to share fixtures across multiple test files, place them in a file named `conftest.py` in your tests directory. Pytest automatically discovers fixtures in `conftest.py` without requiring explicit imports.

### 5. Composing Fixtures
Fixtures can depend on other fixtures, allowing you to build complex state from simple parts:

```python
@pytest.fixture(scope="session")
def api_base_url():
    return "https://api.example.com"

@pytest.fixture(scope="session")
def auth_token(api_base_url):
    # Depends on api_base_url
    return login_and_get_token(api_base_url)

def test_api_call(auth_token):
    # Injects the shared token
    assert auth_token is not None
```

### Summary of Idiomatic Practices
- **Avoid Global State:** Never use global variables to share state; use fixtures.
- **Prefer Narrowest Scope:** Start with `function` scope and only move to `class` or `module` if the setup is significantly expensive (e.g., spinning up a Docker container or a database).
- **Use `autouse=True` Sparingly:** If every test in a module needs a setup (like clearing a cache), use `@pytest.fixture(autouse=True)`.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scoping (class, module, session) and setup/teardown patterns using yield, which were directly used to answer the inquiry."/>