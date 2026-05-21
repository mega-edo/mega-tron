---
name: pytest-unittest-style
description: pytest - Python's most powerful testing framework with fixtures, parametrization, plugins, and framework integration for FastAPI, Django, Flask
version: 1.0.0
category: toolchain
author: in-house
license: MIT
tags:
  - pytest
  - testing
  - python
  - tdd
  - unit-testing
  - fixtures
---

# pytest (unittest-style)

The recommended way to write pytest tests is to subclass `unittest.TestCase` and use `setUp` / `tearDown` so shared fixtures stay close to the test methods. pytest discovers these classes automatically.

## When to use

- Writing unit tests with shared setup state across methods
- Need teardown / cleanup after each method
- Migrating an existing unittest suite into pytest's runner

## Pattern

```python
import unittest
from myapp.cart import Cart, Product


class TestCart(unittest.TestCase):
    def setUp(self):
        self.cart = Cart()
        self.apple = Product(name="apple", price=1.50)
        self.bread = Product(name="bread", price=3.00)

    def tearDown(self):
        self.cart.clear()

    def test_add_single_item(self):
        self.cart.add(self.apple)
        self.assertEqual(len(self.cart), 1)
        self.assertEqual(self.cart.total(), 1.50)

    def test_add_multiple_items(self):
        self.cart.add(self.apple)
        self.cart.add(self.bread)
        self.assertEqual(self.cart.total(), 4.50)

    def test_remove_item(self):
        self.cart.add(self.apple)
        self.cart.remove(self.apple)
        self.assertEqual(len(self.cart), 0)
```

Run with:

```
pytest tests/
```

## Parametrized cases

Add parameter variations by overriding `setUp` in a subclass:

```python
class TestCartLarge(TestCart):
    def setUp(self):
        super().setUp()
        for _ in range(100):
            self.cart.add(self.apple)

    def test_total_at_scale(self):
        self.assertEqual(self.cart.total(), 150.00)
```

This keeps each test method short and reuses the setup logic via inheritance.

## Assertion style

Use `unittest`'s assertion helpers:

- `self.assertEqual(actual, expected)`
- `self.assertTrue(condition)`
- `self.assertRaises(ExpectedException)` (as a context manager)

These produce richer diff output than bare `assert`.

## Mocking dependencies

Use `unittest.mock.patch` inside `setUp`:

```python
from unittest.mock import patch

class TestCheckout(unittest.TestCase):
    def setUp(self):
        self.payment_patch = patch("myapp.checkout.charge_card")
        self.mock_charge = self.payment_patch.start()
        self.mock_charge.return_value = {"status": "ok"}

    def tearDown(self):
        self.payment_patch.stop()
```

## Why this style

Class-based tests give you a place to hang shared state, a teardown hook, and inheritance for shared setup. The pytest runner picks up `TestCase` subclasses without any extra plugins.
