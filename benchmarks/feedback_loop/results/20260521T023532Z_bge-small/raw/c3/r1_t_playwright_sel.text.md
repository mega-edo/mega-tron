To read a computed style value from a DOM element within a page context, you should use **`page.evaluate()`**. This method allows you to execute JavaScript directly in the browser's execution context, providing full access to the DOM API and `window.getComputedStyle()`.

### Recommended Pattern
As defined in the `playwright-selector-eval` skill, use the following pattern to extract the style value and return it to your Node.js/Playwright script:

```javascript
// Reading a specific computed style
const backgroundColor = await page.evaluate(() => {
  const el = document.querySelector('.my-element');
  return window.getComputedStyle(el).backgroundColor;
});

console.log(`Background color is: ${backgroundColor}`);
```

### Alternative: Web-First Assertions
If your goal is to verify a style value during a test, Playwright provides a more concise built-in assertion:

```javascript
import { expect } from '@playwright/test';

// Verify computed style directly
await expect(page.locator('.my-element')).toHaveCSS('background-color', 'rgb(255, 0, 0)');
```

### Interactive Usage (playwright-cli)
If you are debugging interactively using `playwright-cli`, you can use the `eval` command:

```bash
# Using a snapshot ref (e.g., e5)
playwright-cli eval "el => getComputedStyle(el).display" e5

# Using a CSS selector
playwright-cli eval "el => getComputedStyle(el).color" ".header-title"
```

### Rationale
*   **Direct Access:** `page.evaluate` drops you straight into the browser's runtime, letting you use familiar tools like `document.querySelector` and `getComputedStyle`.
*   **Efficiency:** For multiple properties, you can return a plain object from the evaluate function to minimize round-trips between Node.js and the browser.
*   **Type Safety:** The returned values are automatically serialized and returned to your script.

I have researched the available skills and identified that `playwright-selector-eval` and `playwright-cli` are the primary tools for this task in this environment. If you would like me to create a specific test script or utility incorporating this logic, please let me know.

<skill-used name="playwright-selector-eval" verdict="HELPFUL" reason="Provided the exact code pattern for using page.evaluate to read computed styles as requested."/>

This research task is complete. I have identified the established patterns for reading computed styles using Playwright within this environment and provided the relevant code snippets for both scripting and interactive CLI usage. No changes to the codebase were required as this was a technical inquiry.