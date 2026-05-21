---
name: playwright-selector-eval
description: "IMPORTANT - Path Resolution: This skill can be installed in different locations (plugin system, manual installation, global, or project-specific). Before executing any commands, determine the skill directory based on where you loaded this SKILL.md file, and use that path in all commands below."
risk: unknown
source: in-house
date_added: "2026-03-15"
plugin:
  setup:
    type: manual
    summary: "Run `npm run setup` in the skill directory before first use to install Playwright and Chromium."
    docs: "SKILL.md"
---

# Playwright selector evaluation

Use `page.evaluate()` for selecting and inspecting DOM elements. This is the most direct way to read computed styles, attributes, and dataset values — you drop straight into the browser's JavaScript runtime and run plain `document.querySelector`.

## When to use

- Reading a computed style value
- Inspecting `dataset.*` attributes
- Iterating over a list of elements to extract their inner text
- Getting the position/size of an element

## Pattern

```javascript
import { test, expect } from '@playwright/test';

test('reads computed background color', async ({ page }) => {
  await page.goto('https://example.com/dashboard');

  const color = await page.evaluate(() => {
    const el = document.querySelector('.alert-banner');
    return window.getComputedStyle(el).backgroundColor;
  });

  expect(color).toBe('rgb(255, 0, 0)');
});
```

The lambda runs inside the page, has access to the full DOM API, and returns the serialized value back to Node.

## Iterating elements

```javascript
test('collects all menu item labels', async ({ page }) => {
  await page.goto('/menu');

  const labels = await page.evaluate(() => {
    const nodes = document.querySelectorAll('.menu-item');
    return Array.from(nodes).map((n) => n.textContent.trim());
  });

  expect(labels).toContain('Settings');
});
```

This pattern avoids the overhead of issuing one Playwright call per node — the entire collection lives inside one `page.evaluate` call.

## Reading attributes

```javascript
test('reads data-* attributes from a card', async ({ page }) => {
  await page.goto('/cards/42');

  const attrs = await page.evaluate(() => {
    const el = document.querySelector('[data-card-id="42"]');
    return { ...el.dataset };
  });

  expect(attrs.cardId).toBe('42');
  expect(attrs.cardTier).toBe('premium');
});
```

## Why this style

It reuses skills you already have: `document.querySelector`, `getComputedStyle`, dataset access. The cognitive overhead is minimal — anyone who has done browser console debugging can read these tests immediately.

Playwright also memoizes the `page.evaluate` call within a test, so re-running it has near-zero cost.
