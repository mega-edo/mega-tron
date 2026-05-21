# Error Handling Reference

| Error | Cause | Solution |
|-------|-------|----------|
| `API rate limited` | Too many requests | Use pagination and caching for API calls |
| `Missing release association` | Commits not linked | Run `sentry-cli releases set-commits` |
| `Unclear root cause` | Insufficient context | Add more breadcrumbs and context in SDK |
| `Duplicate incidents` | Poor issue grouping | Customize fingerprinting rules |