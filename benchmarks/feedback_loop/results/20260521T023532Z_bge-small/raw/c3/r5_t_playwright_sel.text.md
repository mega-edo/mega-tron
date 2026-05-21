To grab a DOM element and read its computed style, use the `page.evaluate()` method. This allows you to execute JavaScript directly within the browser context and leverage the standard Web API.

```javascript
const computedStyleValue = await page.evaluate(() => {
  const element = document.querySelector('YOUR_SELECTOR');
  if (!element) return null;
  return window.getComputedStyle(element).getPropertyValue('YOUR_STYLE_PROPERTY');
});
```

### Key Advantages:
- **Direct Access:** You use standard browser APIs like `document.querySelector` and `window.getComputedStyle`.
- **Efficiency:** The entire operation happens in a single round-trip to the browser.
- **Reliability:** It handles elements that might not be easily accessible via Playwright's high-level Locators when specific style values are needed.

<skill-used name="playwright-selector-eval" verdict="HELPFUL" reason="The skill provided the exact pattern using page.evaluate() and window.getComputedStyle() as recommended in SKILL.md."/>