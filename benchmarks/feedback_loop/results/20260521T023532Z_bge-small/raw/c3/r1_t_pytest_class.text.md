To share setup state across `pytest` test methods idiomatically, you should use **fixtures** with an appropriate **scope**. 

### 1. The Core Pattern: Fixture Scopes
By default, fixtures have `scope="function"`, meaning they are re-executed for every test method. To share state, increase the scope:

*   **`scope="class"`**: The fixture runs once per test class.
*   **`scope="module"`**: The fixture runs once per test file (`.py` module).
*   **`scope="session"`**: The fixture runs once for the entire test run.

```python
import pytest

@pytest.fixture(scope="class")
def shared_resource():
    # Setup: runs once for the whole class
    resource = {"data": "initialized"}
    print("\n[Setup] Resource initialized")
    
    yield resource # Provide the resource to tests
    
    # Teardown: runs after all tests in the class finish
    print("\n[Teardown] Cleaning up resource")
    resource.clear()

class TestExample:
    def test_one(self, shared_resource):
        shared_resource["count"] = 1
        assert shared_resource["data"] == "initialized"

    def test_two(self, shared_resource):
        # State from test_one is preserved because of 'class' scope
        assert shared_resource["count"] == 1
```

### 2. Implementation Best Practices

#### Using `yield` for Teardown
Instead of returning a value, use `yield`. Code before `yield` is the **setup**, and code after `yield` is the **teardown**.

#### Composable Fixtures
Fixtures can depend on other fixtures. You can have a `session` scope fixture for a database connection and a `function` scope fixture for a transaction that rolls back after each test.

```python
@pytest.fixture(scope="session")
def db_engine():
    engine = create_engine(...)
    yield engine
    engine.dispose()

@pytest.fixture(scope="function")
def db_session(db_engine):
    session = Session(db_engine)
    yield session
    session.rollback()
    session.close()
```

#### Shared Fixtures via `conftest.py`
If you need to share fixtures across multiple files, place them in a `conftest.py` file in your tests directory. `pytest` discovers them automatically without needing imports.

### 3. Comparison with `unittest`
Unlike `unittest`'s `setUpClass`, `pytest` fixtures are **dependency-injected**, making it explicit which tests require which resources, even when those resources have different lifecycles.

<skill-used name="pytest" verdict="HELPFUL" reason="The skill provided comprehensive details on fixture scoping (class, module, session) and setup/teardown patterns using yield, which were directly used to answer the inquiry."/>