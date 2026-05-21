# Error Handling Reference

| Error | Cause | Solution |
|-------|-------|----------|
| `Release already exists` | Duplicate version | Use unique version (git SHA or timestamp) |
| `Failed to set commits` | Repository not connected | Install Sentry GitHub/GitLab integration |
| `Source map upload failed` | Auth token expired | Generate new token in Sentry settings |
| `Release not visible in SDK` | Version mismatch | Ensure `release` in SDK matches CLI version |