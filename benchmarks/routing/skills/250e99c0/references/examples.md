# Examples

### Quick Performance Wrapper
```typescript
const withPerformance = <T>(name: string, fn: () => Promise<T>) =>
  measuredSupabaseCall(name, () =>
    cachedSupabaseRequest(`cache:${name}`, fn)
  );
```