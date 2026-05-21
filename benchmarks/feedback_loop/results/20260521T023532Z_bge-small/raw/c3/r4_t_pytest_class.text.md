To share setup state across test methods in `pytest` idiomatically, you should use **Fixtures** with appropriate **Scopes**.

### 1. The Core Mechanism: `@pytest.fixture`
Fixtures are functions that run before (and optionally after) your tests to provide a reliable baseline or data state.

```python
import pytest

@pytest.fixture
def api_client():
    # Setup: Create the resource
    client = APIClient(base_url="https://api.example.com")
    
    yield client  # Provide the client to the test
    
    # Teardown: Clean up after the test (optional)
    client.close()
```

### 2. Controlling Lifetime with `scope`
The `scope` parameter determines how often the setup code runs. By default, it's `"function"`, but you can increase it to share state:

| Scope | Frequency | Best For |
| :--- | :--- | :--- |
| `function` (default) | Once per test method | Isolated unit tests, temporary files. |
| `class` | Once per test class | Grouping related tests that share a database connection. |
| `module` | Once per `.py` file | Expensive setup like spinning up a local server. |
| `session` | Once per test run | Heavy lifting like global configuration or Docker containers. |

**Example of Class-Scoped Sharing:**
```python
@pytest.fixture(scope="class")
def db_connection():
    conn = Database.connect()
    yield conn
    conn.close()

class TestDatabase:
    def test_insert(self, db_connection):
        # Uses the shared connection
        assert db_connection.insert({"id": 1})

    def test_query(self, db_connection):
        # Uses the SAME connection instance as test_insert
        assert db_connection.find(1)
```

### 3. Global Sharing with `conftest.py`
If you want to share fixtures across multiple files without importing them, place them in a file named `conftest.py` in your tests directory. `pytest` automatically discovers these.

```python
# tests/conftest.py
import pytest

@pytest.fixture(scope="session")
def app_config():
    return {"timeout": 30, "debug": True}
```

### 4. Composition (Fixtures using Fixtures)
You can build complex states by having one fixture depend on another. This is cleaner than one massive setup block.

```python
@pytest.fixture
def db():
    return Database()

@pytest.fixture
def admin_user(db):
    user = db.create_user(role="admin")
    return user

def test_admin_access(admin_user):
    assert admin_user.is_admin is True
```

### 5. Sharing State via "Yield" (Setup & Teardown)
The `yield` keyword is the idiomatic way to handle teardown. Everything before `yield` is setup; everything after is teardown.

```python
@pytest.fixture
def temp_file(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("content")
    yield f
    # Teardown happens here after the test finishes
    f.unlink()
```

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scoping (class, module, session) and setup/teardown patterns using yield, which were directly used to answer the inquiry."/>