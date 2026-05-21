# Release Workflow

## Release Workflow

### 1. Create Release
```bash
# Using git SHA
VERSION=$(git rev-parse --short HEAD)
sentry-cli releases new $VERSION

# Using semantic version
VERSION="1.2.3"
sentry-cli releases new "myapp@$VERSION"

# Using package.json version
VERSION=$(node -p "require('./package.json').version")
sentry-cli releases new "myapp@$VERSION"
```

### 2. Associate Commits
```bash
# Auto-detect commits from git
sentry-cli releases set-commits $VERSION --auto

# Specify commit range
sentry-cli releases set-commits $VERSION \
  --commit "repo-name@from-sha..to-sha"

# Manual commit association
sentry-cli releases set-commits $VERSION \
  --commit "org/repo@abc123"
```

### 3. Upload Artifacts
```bash
# Upload source maps
sentry-cli releases files $VERSION upload-sourcemaps ./dist

# Upload with URL prefix
sentry-cli releases files $VERSION upload-sourcemaps ./dist \
  --url-prefix "~/static/js"

# Upload specific files
sentry-cli releases files $VERSION upload ./dist/app.js.map
```

### 4. Finalize Release
```bash
# Mark release as complete
sentry-cli releases finalize $VERSION

# All-in-one
sentry-cli releases new $VERSION --finalize
```