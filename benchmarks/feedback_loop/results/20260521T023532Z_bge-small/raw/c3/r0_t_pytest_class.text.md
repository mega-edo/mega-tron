The idiomatic way to share setup state in `pytest` is through **Fixtures** and their **Scoping** mechanism. Unlike the `unittest` framework's `setUp` and `tearDown` methods, `pytest` fixtures use dependency injection to provide resources to tests.

### 1. Fixture Scoping
The `scope` parameter determines how often a fixture is executed. By default, fixtures are `function` scoped (run for every test), but you can elevate them to share state:

*   **`class`**: Run once per test class.
*   **`module`**: Run once per Python file.
*   **`session`**: Run once for the entire test suite.

#### Example: Class-Scoped Fixture
Use `scope="class"` when you have an expensive resource (like a database connection) that multiple methods in a class should reuse.

```python
import pytest

class Database:
    def connect(self): print("Connecting...")
    def close(self): print("Closing...")
    def query(self, q): return f"Result for {q}"

@pytest.fixture(scope="class")
def db_connection():
    # Setup: runs once before the first test in the class
    db = Database()
    db.connect()
    
    yield db  # This provides the 'state' to the tests
    
    # Teardown: runs once after the last test in the class
    db.close()

class TestDatabase:
    def test_query_one(self, db_connection):
        assert db_connection.query("A") == "Result for A"

    def test_query_two(self, db_connection):
        assert db_connection.query("B") == "Result for B"
```

### 2. Global Sharing with `conftest.py`
To share state across multiple files, place your fixtures in a file named `conftest.py` at the root of your test directory. `pytest` automatically discovers fixtures in this file, making them available to all tests in that directory and subdirectories without needing imports.

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
def api_client():
    client = ExpensiveAPIClient()
    yield client
    client.shutdown()
```

### 3. Composing Fixtures
Fixtures can depend on other fixtures. This allows you to build complex state from smaller, reusable parts.

```python
@pytest.fixture
def logged_in_client(api_client):
    api_client.login("admin", "password")
    return api_client
```

### 4. Automatic Setup with `autouse`
If you want a fixture to run for every test (or every test in a class/module) without explicitly passing it as an argument, use `autouse=True`.

```python
@pytest.fixture(scope="class", autouse=True)
def setup_environment():
    # Prepare environment variables or config
    pass
```

### Summary of Best Practices
*   **Use `yield`**: Always use `yield` instead of `return` if you need to perform teardown (cleanup) after tests finish.
*   **Keep State Immutable**: While you *can* share mutable state, it's safer to provide a "factory" or a "connection" rather than a global object that tests modify. If tests must modify state, ensure the fixture handles resetting that state.
*   **Prefer Narrowest Scope**: Start with `function` scope and only move to `class` or `module` if performance (e.g., slow setup) requires it.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scoping (class, module, session) and setup/teardown patterns using yield, which were directly used to answer the inquiry."/>

The idiomatic way to share setup state in `pytest` is through **Fixtures** and their **Scoping** mechanism. Unlike the `unittest` framework's `setUp` and `tearDown` methods, `pytest` fixtures use dependency injection to provide resources to tests.

### 1. Fixture Scoping
The `scope` parameter determines how often a fixture is executed. By default, fixtures are `function` scoped (run for every test), but you can elevate them to share state:

*   **`class`**: Run once per test class.
*   **`module`**: Run once per Python file.
*   **`session`**: Run once for the entire test suite.

#### Example: Class-Scoped Fixture
Use `scope="class"` when you have an expensive resource (like a database connection) that multiple methods in a class should reuse.

```python
import pytest

class Database:
    def connect(self): print("Connecting...")
    def close(self): print("Closing...")
    def query(self, q): return f"Result for {q}"

@pytest.fixture(scope="class")
def db_connection():
    # Setup: runs once before the first test in the class
    db = Database()
    db.connect()
    
    yield db  # This provides the 'state' to the tests
    
    # Teardown: runs once after the last test in the class
    db.close()

class TestDatabase:
    def test_query_one(self, db_connection):
        assert db_connection.query("A") == "Result for A"

    def test_query_two(self, db_connection):
        assert db_connection.query("B") == "Result for B"
```

### 2. Global Sharing with `conftest.py`
To share state across multiple files, place your fixtures in a file named `conftest.py` at the root of your test directory. `pytest` automatically discovers fixtures in this file, making them available to all tests in that directory and subdirectories without needing imports.

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
def api_client():
    client = ExpensiveAPIClient()
    yield client
    client.shutdown()
```

### 3. Composing Fixtures
Fixtures can depend on other fixtures. This allows you to build complex state from smaller, reusable parts.

```python
@pytest.fixture
def logged_in_client(api_client):
    api_client.login("admin", "password")
    return api_client
```

### 4. Automatic Setup with `autouse`
If you want a fixture to run for every test (or every test in a class/module) without explicitly passing it as an argument, use `autouse=True`.

```python
@pytest.fixture(scope="class", autouse=True)
def setup_environment():
    # Prepare environment variables or config
    pass
```

### Summary of Best Practices
*   **Use `yield`**: Always use `yield` instead of `return` if you need to perform teardown (cleanup) after tests finish.
*   **Keep State Immutable**: While you *can* share mutable state, it's safer to provide a "factory" or a "connection" rather than a global object that tests modify. If tests must modify state, ensure the fixture handles resetting that state.
*   **Prefer Narrowest Scope**: Start with `function` scope and only move to `class` or `module` if performance (e.g., slow setup) requires it.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scoping (class, module, session) and setup/teardown patterns using yield, which were directly used to answer the inquiry."/>