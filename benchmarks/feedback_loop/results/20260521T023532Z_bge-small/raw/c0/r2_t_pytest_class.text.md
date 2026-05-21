To share setup state across `pytest` test methods, the idiomatic approach is to use **Fixtures** with a defined **scope**.

By default, fixtures have `scope="function"`, meaning they are re-run for every test. To share state, you increase the scope to `class`, `module`, or `session`.

### 1. Using Scoped Fixtures
Set the `scope` parameter in the `@pytest.fixture` decorator:

- **`class`**: Run once per test class.
- **`module`**: Run once per Python file (module).
- **`session`**: Run once for the entire test run.

```python
import pytest

@pytest.fixture(scope="class")
def shared_db_connection():
    # Setup: Initialize something expensive
    print("\nConnecting to database...")
    db = {"connection": "active", "data": []}
    
    yield db  # Provide the object to tests
    
    # Teardown: Clean up after all tests in the class finish
    print("\nClosing database connection...")

class TestDatabaseLogic:
    def test_insert(self, shared_db_connection):
        shared_db_connection["data"].append("record1")
        assert len(shared_db_connection["data"]) == 1

    def test_verify_persistence(self, shared_db_connection):
        # This test uses the same 'shared_db_connection' instance
        assert "record1" in shared_db_connection["data"]
```

### 2. Sharing across files with `conftest.py`
If you need to share state across multiple test files, define your fixture in a file named `conftest.py` in your test directory. Pytest automatically discovers fixtures in `conftest.py` without requiring imports.

```python
# conftest.py
@pytest.fixture(scope="session")
def api_client():
    return MyApiClient(token="secret")
```

### 3. Setup/Teardown with `yield`
The `yield` statement is the standard way to handle teardown. Everything before `yield` is setup; everything after is teardown, which runs after the scope (class, module, or session) ends.

### 4. Direct Class Setup (xUnit style)
If you prefer the traditional xUnit style, you can use `setup_class` and `teardown_class`, though fixtures are generally preferred for their composability:

```python
class TestLegacy:
    @classmethod
    def setup_class(cls):
        cls.shared_resource = "Initialized"

    def test_one(self):
        assert self.shared_resource == "Initialized"
```

**Recommendation:** Use **fixtures with `scope="class"` or `scope="module"`** for better flexibility and easier dependency injection.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provides the foundational knowledge of pytest fixtures and scoping, which is the direct answer to sharing state idiomatically."/>