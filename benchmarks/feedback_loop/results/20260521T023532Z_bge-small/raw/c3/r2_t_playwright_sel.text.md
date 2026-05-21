To read a computed style value from a DOM element using Playwright, the recommended approach is to use `page.evaluate()`. This executes a function within the browser's page context, providing direct access to the DOM and the `window.getComputedStyle` API.

### Playwright Code Pattern
Use this pattern in your Playwright tests or scripts:

```javascript
const backgroundColor = await page.evaluate(() => {
  const element = document.querySelector('.my-element');
  return window.getComputedStyle(element).backgroundColor;
});
```

### Using playwright-cli
If you are interacting with a page via the CLI, you can use the `eval` command:

```bash
playwright-cli eval "el => getComputedStyle(el).backgroundColor" .my-element
```

### Why use `page.evaluate()`?
- **Efficiency:** The entire extraction happens inside the browser in a single operation, avoiding multiple network round-trips between Node.js and the browser.
- **Full API Access:** You have access to all standard browser APIs (`document.querySelector`, `getComputedStyle`, `dataset`, etc.).
- **Serialization:** Playwright automatically serializes the returned value (strings, numbers, or simple objects) back to your Node.js context.

<skill-used name="playwright-selector-eval" verdict="HELPFUL" reason="The skill provided the exact pattern and code examples for using page.evaluate to read computed styles."/>
<skill-used name="playwright-cli" verdict="HELPFUL" reason="The skill provided the CLI command syntax for evaluating computed styles on elements."/>