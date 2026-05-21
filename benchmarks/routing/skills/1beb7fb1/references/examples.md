# Examples

### Quick Circuit Check
```typescript
const state = supabaseBreaker.stats().state;
console.log('Supabase circuit:', state);
```