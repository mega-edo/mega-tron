To read a computed style value using Playwright, you should use `page.evaluate()`. This allows you to execute JavaScript directly within the browser context and access the DOM API, including `window.getComputedStyle()`.

### Recommended Pattern

The following snippet demonstrates how to grab an element and extract its `background-color`:

```javascript
const color = await page.evaluate(() => {
  const el = document.querySelector('.your-element-selector');
  return window.getComputedStyle(el).backgroundColor;
});

console.log(color); // e.g., "rgb(255, 0, 0)"
```

### Why use `page.evaluate()`?
- **Direct Access:** It provides the most direct route to the browser's runtime for inspection tasks.
- **Efficiency:** You can perform multiple DOM queries or calculations in a single execution loop, reducing the overhead of multiple Playwright commands.
- **Accuracy:** `getComputedStyle` reflects the final state of all applied CSS rules, which is often more reliable than checking individual styles in a test runner.

<skill-used name="playwright-selector-eval" verdict="HELPFUL" reason="The skill explicitly recommends page.evaluate() with getComputedStyle for reading computed styles, which perfectly matches the user request."/>