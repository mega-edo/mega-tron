I have analyzed the request and the existing codebase. There is already an implementation in `extract_nginx_data.py` and a test suite in `test_extract_nginx_data.py` that addresses this task.

### Proposed Strategy
1.  **Refine Regex**: I will slightly enhance the existing regex in `extract_nginx_data.py` to be more robust, specifically by adding word boundaries and better status code validation (100-599 range) as recommended by the `regex-log` skill.
2.  **Verification**: I will run the existing tests to ensure the current logic is sound.
3.  **Execution**: I will run the script against `sample_nginx.log` to extract the timestamp, request ID, and HTTP status code for each line.

Does this approach meet your requirements, or would you like the output in a specific format (e.g., CSV, JSON)?

<skill-used name="regex-log" verdict="HELPFUL" reason="The skill provided guidance on constructing robust regex patterns for log files, which confirmed the validity of the existing pattern in extract_nginx_data.py and suggested improvements."/>