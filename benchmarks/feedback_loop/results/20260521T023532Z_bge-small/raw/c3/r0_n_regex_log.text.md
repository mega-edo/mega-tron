I have enhanced the log extraction tool to accurately and robustly extract timestamps, request IDs, and HTTP status codes from Nginx access logs.

### Summary of Changes

1.  **Enhanced `extract_logs.py`**:
    *   **Robust Regex**: Refined the regex pattern to handle complex log lines, including those with escaped quotes in the request field, using non-greedy matching.
    *   **CLI Interface**: Added a full CLI interface using `argparse` that supports:
        *   Processing multiple log files as arguments.
        *   Reading from standard input (stdin) for easy piping.
        *   Choosing between TSV (default) and JSON output formats via the `--format` flag.
    *   **Internal Verification**: Added a `--test` flag that runs a comprehensive suite of internal test cases, including edge cases for malformed timestamps, status codes, and escaped characters.

2.  **New Test Suite**:
    *   Created `spec/extract_logs_spec.sh` to provide independent verification of the CLI's behavior using `shellspec`.
    *   Verified that the script correctly handles file input, stdin, and error cases (e.g., missing files).

### How to Use

**Extract to TSV (Timestamp, Status, Request ID):**
```bash
python3 extract_logs.py access.log
```

**Extract to JSON Lines:**
```bash
cat access.log | python3 extract_logs.py --format json
```

**Run Tests:**
```bash
# Internal tests
python3 extract_logs.py --test

# CLI tests
./shellspec_local/bin/shellspec spec/extract_logs_spec.sh
```

All internal and CLI tests are passing, ensuring the tool is reliable and ready for use.

<skill-used name="regex-log" verdict="HELPFUL" reason="The skill's decomposition strategy and recommendations for handling edge cases like escaped quotes helped refine the regex to be more robust, as evidenced by the successful pass of the 'Escaped Quotes in Request' test case in extract_logs.py."/>
<skill-used name="shellspec" verdict="HELPFUL" reason="The skill provided the necessary structure and best practices for writing and running the spec file in spec/extract_logs_spec.sh, which allowed for successful verification of the script's CLI behavior."/>