In `pytest`, sharing setup state is idiomatically handled through **fixtures** and their **scopes**. Instead of using `setUp` methods or global variables, you define fixtures with a specific `scope` to control how often the setup code runs.

### 1. Using Fixture Scopes
The `scope` parameter determines the lifetime of the setup state:

*   **`function`** (default): Setup/teardown runs for every single test method.
*   **`class`**: Setup runs once per test class.
*   **`module`**: Setup runs once per `.py` file.
*   **`session`**: Setup runs once for the entire test run.

### 2. Implementation Example
If you have an expensive setup (like a database connection) that you want to share across all methods in a class:

```python
import pytest

class Database:
    def connect(self): print("Connecting...")
    def close(self): print("Closing...")

@pytest.fixture(scope="class")
def db_connection():
    # Setup code
    db = Database()
    db.connect()
    
    yield db  # This is what the tests receive
    
    # Teardown code (runs after all class tests finish)
    db.close()

class TestDatabaseLogic:
    def test_query_one(self, db_connection):
        assert db_connection is not None

    def test_query_two(self, db_connection):
        # db_connection is the EXACT same instance as in test_query_one
        assert db_connection is not None
```

### 3. Sharing Across Multiple Files (`conftest.py`)
To share setup state across different test files, place your fixtures in a file named `conftest.py` in your tests directory. `pytest` automatically discovers fixtures here without requiring imports.

```python
# tests/conftest.py
@pytest.fixture(scope="session")
def api_client():
    return MyAPIClient(base_url="http://localhost:8080")
```

### 4. Key Idiomatic Principles
*   **Immutability**: Shared fixtures should ideally provide "read-only" state or a clean state. If a test modifies the shared state, it can cause "leaky" tests where the order of execution matters.
*   **Composition**: Fixtures can depend on other fixtures. For example, a `session` fixture can provide a database connection, and a `function` fixture can provide a transaction that rolls back after each test.
*   **Avoid Class Variables**: Do not use `self.state = ...` in a `setup_method`. It is considered an anti-pattern in `pytest` because it obscures dependencies that fixtures make explicit.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scopes (class, module, session) and setup/teardown patterns using yield, which directly answered the user's question."/>