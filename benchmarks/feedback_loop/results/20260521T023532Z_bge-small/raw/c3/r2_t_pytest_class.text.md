In `pytest`, the idiomatic way to share setup state across test methods is by using **Fixtures** with appropriate **Scopes**.

### 1. The Fixture Pattern
Instead of using `setUp` and `tearDown` methods from `unittest`, `pytest` uses the `@pytest.fixture` decorator. Test methods "request" these fixtures by including them as arguments.

```python
import pytest

@pytest.fixture
def database_connection():
    # Setup: Initialize resource
    conn = create_db_connection()
    yield conn  # Provide resource to tests
    # Teardown: Clean up after tests
    conn.close()

def test_user_creation(database_connection):
    assert database_connection.is_active()
```

### 2. Controlling Sharing with `scope`
The `scope` parameter determines how often a fixture is executed and shared:

*   **`function` (default)**: The fixture is executed for *every* test method. State is not shared between tests (ensuring isolation).
*   **`class`**: The fixture is executed once per `TestClass`. All methods in that class share the same instance.
*   **`module`**: Executed once per `.py` file.
*   **`session`**: Executed once for the entire test run. This is ideal for expensive operations like starting a web server or connecting to a global database.

```python
@pytest.fixture(scope="session")
def expensive_resource():
    return setup_global_api_client()
```

### 3. Global Sharing with `conftest.py`
To share fixtures across multiple test files without importing them, place them in a file named `conftest.py` in your tests directory. `pytest` automatically discovers these fixtures for all tests in that directory and its subdirectories.

### 4. Sharing State via "Class Scope"
If you prefer organizing tests in classes, you can use a class-scoped fixture:

```python
@pytest.fixture(scope="class")
def shared_data():
    return {"id": 123, "status": "active"}

class TestWorkflow:
    def test_step_one(self, shared_data):
        assert shared_data["id"] == 123

    def test_step_two(self, shared_data):
        assert shared_data["status"] == "active"
```

### 5. Idiomatic "Factory" Pattern
If you need shared setup but want each test to be able to customize the data, return a function (a factory) from your fixture:

```python
@pytest.fixture
def make_user():
    def _make_user(name):
        return {"name": name, "id": generate_id()}
    return _make_user

def test_admin(make_user):
    admin = make_user("Admin")
    assert admin["name"] == "Admin"
```

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scoping (class, module, session) and setup/teardown patterns using yield, which were directly used to answer the inquiry."/>