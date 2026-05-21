To read a computed style value from a DOM element using Playwright, you can use `page.$eval` or `page.evaluate`. The core JavaScript API for this is `window.getComputedStyle(element)`.

### Recommended Strategy

1.  **Identify the Element:** Use a CSS selector or Playwright locator to target the specific element.
2.  **Execute in Page Context:** Use `page.$eval` to select the element and run the style extraction logic inside the browser context.
3.  **Return the Value:** Extract the specific property (e.g., `color`, `display`, `width`) from the `CSSStyleDeclaration` object.

### Implementation Example

```javascript
// Using page.$eval (best for single elements)
const backgroundColor = await page.$eval('.my-element', (el) => {
  return window.getComputedStyle(el).getPropertyValue('background-color');
});

// Using a locator (Playwright 1.14+)
const element = page.locator('.my-element');
const display = await element.evaluate((el) => {
  return window.getComputedStyle(el).display;
});
```

### Verification Steps
*   Ensure the selector matches exactly one element to avoid errors.
*   Note that `getComputedStyle` returns the **resolved** values (e.g., "rgb(255, 0, 0)" instead of "red").
*   If the element is inside an iframe, make sure to use `frame.evaluate` or `frame.locator`.

<skill-used name="playwright-selector-eval" verdict="NEUTRAL" reason="The skill provided general context but the core solution relies on standard Playwright and Web APIs already known to the agent."/>