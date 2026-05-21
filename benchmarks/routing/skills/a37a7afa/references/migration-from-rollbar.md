# Migration From Rollbar

## Migration from Rollbar

### SDK Replacement
```typescript
// Before: Rollbar
import Rollbar from 'rollbar';
const rollbar = new Rollbar({
  accessToken: 'ROLLBAR_TOKEN',
  environment: process.env.NODE_ENV,
});
rollbar.error('Something went wrong', error);

// After: Sentry
import * as Sentry from '@sentry/node';
Sentry.init({
  dsn: process.env.SENTRY_DSN,
  environment: process.env.NODE_ENV,
});
Sentry.captureException(error);
```

### Feature Mapping: Rollbar → Sentry
| Rollbar | Sentry |
|---------|--------|
| `rollbar.error()` | `Sentry.captureException()` |
| `rollbar.warning()` | `Sentry.captureMessage(..., 'warning')` |
| `rollbar.info()` | `Sentry.captureMessage(..., 'info')` |
| `rollbar.configure({ payload: {} })` | `Sentry.setContext()` |
| `rollbar.configure({ person: {} })` | `Sentry.setUser()` |
| Custom fingerprinting | `Sentry.withScope()` + fingerprint |

### Express Middleware Migration
```typescript
// Before: Rollbar
app.use(rollbar.errorHandler());

// After: Sentry
import * as Sentry from '@sentry/node';
Sentry.init({ dsn: process.env.SENTRY_DSN });
app.use(Sentry.Handlers.requestHandler());
app.use(Sentry.Handlers.errorHandler());
```