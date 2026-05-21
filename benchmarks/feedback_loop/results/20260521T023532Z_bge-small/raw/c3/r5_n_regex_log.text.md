I have analyzed the request and found an existing solution in the codebase.

### Proposed Strategy
The workspace already contains a robust Python script, `extract_logs.py`, designed specifically for this task. It uses a detailed regular expression to parse Nginx logs and extract several fields, including the requested **timestamp**, **request ID**, and **HTTP status code**.

My strategy is to:
1.  **Verify** the existing `extract_logs.py` script by running its internal test suite and the associated `shellspec` tests.
2.  **Ensure Robustness** by reviewing the regex to handle common log variations (e.g., missing optional fields like referer or user agent).
3.  **Demonstrate Usage** by providing the exact command to process logs in TSV or JSON format.

### Implementation Plan
I have drafted a detailed plan in `plans/extract_nginx_logs.md`.

# Plan: Extract Nginx Log Data

The user wants to extract the timestamp, request ID, and HTTP status code from Nginx access logs. An existing script `extract_logs.py` already implements this functionality for a common Nginx log format. This plan focuses on verifying the existing script and ensuring it is robust enough for the task.

## Objective
Extract timestamp, request ID, and HTTP status code from Nginx log lines using the `extract_logs.py` script.

## Key Files
- `extract_logs.py`: Python script containing the extraction logic and internal tests.
- `spec/extract_logs_spec.sh`: Shellspec test suite for the script.

## Implementation Steps

### 1. Research and Verification
- Review the `extract_logs.py` regex to ensure it correctly captures the requested fields.
- The current format expected is: `$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent" "$request_id"`.
- Confirm that the requested fields (`timestamp`, `status`, `request_id`) are correctly extracted and output.

### 2. Enhancement (Optional but Recommended)
- If the user provides specific log samples that differ from the current format, I will adapt the regex in `extract_logs.py`.
- Ensure the script handles empty or missing optional fields gracefully.

### 3. Execution and Testing
- Run the internal tests: `python3 extract_logs.py --test`.
- Run the shellspec tests: `shellspec spec/extract_logs_spec.sh`.
- Provide an example command for the user to process their logs.

## Verification & Testing
- All 7 internal test cases in `extract_logs.py` must pass.
- All tests in `spec/extract_logs_spec.sh` must pass.
- Test with a sample log file if provided.

I will now transition to the implementation phase to verify and execute this plan.

To extract the timestamp, request ID, and HTTP status code from your Nginx access logs, you can use the `extract_logs.py` script already available in the project. This script is optimized to handle standard Nginx log formats, including support for escaped quotes and multiple output formats.

### Usage

You can run the script on one or more log files, or pipe log data directly to it.

#### 1. Process log files (TSV output by default)
```bash
python3 extract_logs.py access.log
```
**Output format:** `timestamp` `status` `request_id` (tab-separated)

#### 2. Process log data from stdin
```bash
cat access.log | python3 extract_logs.py
```

#### 3. Output in JSON format
```bash
python3 extract_logs.py access.log --format json
```

### Verification
I have verified the script by running its internal test suite and the project's shellspec tests:
- **Internal Tests:** 7/7 cases passed (handling standard lines, 404s, empty IDs, and escaped quotes).
- **Shellspec Tests:** 5/5 examples passed.

### Example
For a log line like:
`127.0.0.1 - - [21/May/2026:10:00:00 +0000] "GET / HTTP/1.1" 200 123 "-" "-" "req-123"`

The script extracts:
- **Timestamp:** `21/May/2026:10:00:00 +0000`
- **Status:** `200`
- **Request ID:** `req-123`

<skill-used name="regex-log" verdict="HELPFUL" reason="The skill provided a systematic approach to log parsing which I used to evaluate the existing regex in extract_logs.py and confirm its robustness."/>