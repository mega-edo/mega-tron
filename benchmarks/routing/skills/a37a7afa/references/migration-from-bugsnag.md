# Migration From Bugsnag

## Migration from Bugsnag

### SDK Replacement
```typescript
// Before: Bugsnag
import Bugsnag from '@bugsnag/js';
Bugsnag.start({ apiKey: 'BUGSNAG_KEY' });
Bugsnag.notify(error);

// After: Sentry
import * as Sentry from '@sentry/browser';
Sentry.init({ dsn: process.env.SENTRY_DSN });
Sentry.captureException(error);
```

### Feature Mapping: Bugsnag → Sentry
| Bugsnag | Sentry |
|---------|--------|
| `Bugsnag.notify()` | `Sentry.captureException()` |
| `Bugsnag.leaveBreadcrumb()` | `Sentry.addBreadcrumb()` |
| `Bugsnag.setUser()` | `Sentry.setUser()` |
| `Bugsnag.addMetadata()` | `Sentry.setContext()` |
| `onError` callback | `beforeSend` hook |

### React Integration Migration
```typescript
// Before: Bugsnag React
import Bugsnag from '@bugsnag/js';
import BugsnagPluginReact from '@bugsnag/plugin-react';
Bugsnag.start({ plugins: [new BugsnagPluginReact()] });
const ErrorBoundary = Bugsnag.getPlugin('react').createErrorBoundary(React);

// After: Sentry React
import * as Sentry from '@sentry/react';
Sentry.init({ dsn: process.env.SENTRY_DSN });
// Use Sentry.ErrorBoundary component
<Sentry.ErrorBoundary fallback={<ErrorFallback />}>
  <App />
</Sentry.ErrorBoundary>
```