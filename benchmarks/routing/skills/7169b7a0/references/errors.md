# Error Handling Reference

Common errors and solutions:
1. **Invalid Signature**: Verify secret matches, check raw body encoding
2. **Timeout**: Return 200 quickly, process async
3. **Missing Events**: Check event subscription list