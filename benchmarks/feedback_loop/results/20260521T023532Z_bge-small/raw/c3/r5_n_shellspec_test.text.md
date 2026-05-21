I have analyzed the `lib/args_parser.sh` script and its existing test suite in `spec/args_parser_spec.sh`.

The current implementation uses `return 1` to signal errors, which is the recommended pattern for library functions because it allows for both sourcing and direct execution (via the source guard at the bottom of the file).

### Key Findings & Strategy

1.  **Subshell Isolation**: ShellSpec's `When call` executes functions in a subshell. This means that even if a function calls `exit`, it will only terminate the subshell, allowing ShellSpec to capture the exit status without killing the test runner.
2.  **Unit vs. Integration Testing**:
    *   **Unit Tests (`When call`)**: Best for testing the logic of the `parse_args` function in isolation.
    *   **Integration Tests (`When run`)**: Best for testing the script as a standalone executable, verifying that the source guard correctly handles the exit status.
3.  **Assertion Patterns**: We can use `The status should equal 1` to verify the exit/return code and `The error should equal ...` to verify the error message on `stderr`.

### Proposed Plan

I have drafted a plan to enhance the test suite by adding integration tests and more explicit error message validations. This will demonstrate both "unit testing a function" and "testing a script that exits."

**Plan Highlights:**
*   Add integration tests using `When run script lib/args_parser.sh` to verify process exit behavior.
*   Retain the use of `return` in the function for source-safety, but explain how ShellSpec handles `exit` if needed.
*   Ensure full coverage of error messages on `stderr`.

I have saved the detailed implementation plan to `plans/test-bash-exit-function.md`. Please let me know if you would like me to proceed with these changes or if you have a specific "exit" scenario you'd like me to focus on.

<skill-used name="shellspec" verdict="HELPFUL" reason="Provided the critical insight that ShellSpec's 'When call' handles exit via subshell isolation, which is the core of the proposed solution."/>

I have successfully demonstrated how to unit-test a Bash function that parses arguments and exits on bad input.

### Key Implementation Details

1.  **Unit Testing with Subshell Isolation**: I used ShellSpec's `When call` to test the `parse_args` function. This executes the function in a subshell, which means if the function calls `exit`, only the subshell terminates. This allows ShellSpec to capture the exit status and continue the test suite without interruption.
2.  **Integration Testing for Script Execution**: I added a "Script Execution" section to `spec/args_parser_spec.sh` using `When run script lib/args_parser.sh`. This verifies that when the file is executed as a standalone command, it correctly exits with status `1` and prints error messages to `stderr` for invalid inputs.
3.  **Comprehensive Validation**:
    *   **Exit Status**: Verified using `The status should equal 1`.
    *   **Error Messages**: Verified using `The error should equal ...` to ensure specific feedback is given to the user on `stderr`.
    *   **Permissions**: Ensured `lib/args_parser.sh` is executable to allow for integration testing.

### Test Results
The test suite now includes 18 examples, all passing, covering both unit-level function logic and script-level exit behavior.

```bash
$ ./shellspec_local/bin/shellspec
Running: /bin/sh [bash 3.2.57(1)-release]
..................

Finished in 0.46 seconds
18 examples, 0 failures
```

### Best Practices Recommendation
While ShellSpec can handle functions that call `exit`, it is generally recommended to use `return` within functions intended for library use. This makes them easier to source and compose. The "Source Guard" pattern used in `lib/args_parser.sh` effectively bridges this by allowing functions to `return` while ensuring the script as a whole `exits` with the correct status.

<skill-used name="shellspec" verdict="HELPFUL" reason="The skill provided the critical 'Pattern 3: Exit Call Interception' and 'When call' subshell isolation context, which allowed me to correctly implement and test the exit behavior."/>