I've analyzed the codebase and found that `extract_logs.py` already implements Nginx log extraction, but it currently includes an extra `bytes` field that wasn't requested. This discrepancy also appears to cause the existing `shellspec` tests to fail, as they expect only the three requested fields.

I propose to update `extract_logs.py` to:
1.  **Modify the regex** to stop capturing the `bytes` field as a named group.
2.  **Update the output logic** to only include `timestamp`, `status`, and `request_id`.
3.  **Sync the internal tests** within the script to match these changes.

This will fulfill your request precisely and restore the test suite's health. Does this strategy sound good to you?

<skill-used name="regex-log" verdict="HELPFUL" reason="The skill provided a systematic approach to log parsing which I used to evaluate the existing regex in extract_logs.py."/>