# Examples

**Example: Cloud Run deployment for a Genkit API**
- Inputs: container image, region, min/max instances, and required model API keys.
- Outputs: Terraform for Cloud Run + Secret Manager bindings and a smoke test curl command that hits a health endpoint / flow route.