# Error Handling Reference

| Error | Cause | Solution |
|-------|-------|----------|
| `Missing errors during migration` | Sampling mismatch | Run both tools at 100% during parallel |
| `Alert parity issues` | Different alert models | Map alert rules carefully before cutover |
| `Data export failed` | API rate limits | Use pagination and backoff |
| `Team unfamiliar with UI` | No training | Schedule Sentry training sessions |