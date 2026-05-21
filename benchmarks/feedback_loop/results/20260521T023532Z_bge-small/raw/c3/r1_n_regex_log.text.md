I have analyzed the workspace and found an existing Python script, `extract_logs.py`, which is specifically designed to extract data from Nginx access logs.

### Current Capabilities
The `extract_logs.py` script uses a regular expression to capture the following fields:
- **Timestamp** (`timestamp`)
- **HTTP Status Code** (`status`)
- **Request ID** (`request_id`)
- **Bytes Sent** (`bytes`)

It supports both **TSV** (Tab-Separated Values) and **JSON** output formats and includes a built-in test suite.

### Proposed Strategy
Since the existing script already performs the extraction you requested, I propose the following:

1.  **Demonstration**: I can create a sample Nginx log file and run the existing script to show you the output.
2.  **Refinement (Optional)**: If you need the output to strictly contain *only* the timestamp, request ID, and status code (excluding the bytes field), or if you prefer a different format (like CSV), I can modify the script accordingly.
3.  **Standalone Regex**: If you prefer to use a tool like `grep`, `sed`, or `awk`, I can provide the standalone regular expression for use in those tools.

**How would you like to proceed?** Would you like to see a demonstration of the current script, or should I modify it to match a specific requirement?

<skill-used name="regex-log" verdict="HELPFUL" reason="The skill provided a systematic approach to log parsing which I used to evaluate the existing regex in extract_logs.py."/>