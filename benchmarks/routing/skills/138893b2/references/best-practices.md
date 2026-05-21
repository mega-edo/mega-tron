# Best Practices

## Best Practices

### Consistent Naming
```bash
# Good: Predictable, traceable
release: "myapp@1.2.3"
release: "myapp@abc123"

# Bad: Unpredictable
release: "latest"
release: "production"
```

### Source Map Management
```bash
# Delete old source maps
sentry-cli releases files $OLD_VERSION delete --all

# Cleanup old releases
sentry-cli releases delete $OLD_VERSION
```

### Release Script
```bash
#!/bin/bash
# release.sh

set -e

VERSION=$1
if [ -z "$VERSION" ]; then
  VERSION=$(git rev-parse --short HEAD)
fi

echo "Creating release: $VERSION"

sentry-cli releases new $VERSION
sentry-cli releases set-commits $VERSION --auto
sentry-cli releases files $VERSION upload-sourcemaps ./dist
sentry-cli releases finalize $VERSION

echo "Release $VERSION created successfully"
```