To share setup state across test methods in `pytest`, the idiomatic pattern is to use **Fixtures** with an appropriate **Scope**.

### 1. The Fixture System
Instead of using traditional `setUp` and `tearDown` methods (like in `unittest`), `pytest` uses fixtures. You define a fixture using the `@pytest.fixture` decorator.

### 2. Sharing State via Scopes
The `scope` parameter determines how often a fixture is executed and how long its state is maintained.

| Scope | Frequency | Best For |
| :--- | :--- | :--- |
| `function` (default) | Once per test method | Independent state (e.g., a fresh list) |
| **`class`** | **Once per test class** | Shared state across methods in a class (e.g., a shared model) |
| `module` | Once per `.py` file | Heavy setup like a database connection for one file |
| `session` | Once per test run | Global setup like a web server or expensive config |

### 3. Implementation Example

Here is how you share a database connection across all methods in a class:

```python
import pytest

class Database:
    def connect(self): print("Connecting...")
    def close(self): print("Closing...")

@pytest.fixture(scope="class")
def db_conn():
    # Setup
    db = Database()
    db.connect()
    
    yield db  # This is the object passed to tests
    
    # Teardown
    db.close()

class TestDatabaseOperations:
    def test_insert(self, db_conn):
        # db_conn is the same instance across all tests in this class
        assert True 

    def test_query(self, db_conn):
        assert True
```

### 4. Key Idiomatic Patterns

*   **`yield` for Teardown:** Use `yield` instead of `return`. Everything before `yield` is "Setup"; everything after is "Teardown".
*   **Dependency Injection:** Simply include the fixture name as an argument in your test methods. `pytest` automatically handles the injection.
*   **`conftest.py`:** If you want to share fixtures across multiple test files, place them in a file named `conftest.py` in your tests directory. `pytest` will discover them automatically without imports.
*   **Fixture Composition:** Fixtures can depend on other fixtures. For example, a `db_session` fixture can depend on a `db_connection` fixture.

### 5. Sharing Data (Mutable State)
If you need to share a piece of data that one test modifies and another reads (though generally discouraged as it makes tests order-dependent), you can use a `session` or `module` scoped fixture that returns a mutable object (like a dictionary or a namespace).

```python
@pytest.fixture(scope="module")
def shared_data():
    return {"id": None}

def test_step_1(shared_data):
    shared_data["id"] = 123

def test_step_2(shared_data):
    assert shared_data["id"] == 123
```

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scoping (class, module, session) and setup/teardown patterns using yield, which were directly used to answer the inquiry."/>