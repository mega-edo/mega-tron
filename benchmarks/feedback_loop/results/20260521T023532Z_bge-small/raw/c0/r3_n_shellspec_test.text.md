To unit-test a Bash function that uses `exit`, I will utilize **ShellSpec**, which is already configured in this project. Testing functions that call `exit` requires a specific approach because a direct function call would terminate the test runner process itself.

### Proposed Strategy

1.  **Isolation Wrapper**: I will use a subshell wrapper (e.g., `( function_name )`) within the test suite. This ensures that when the function calls `exit`, only the subshell terminates, allowing ShellSpec to capture the exit status and `stderr` without crashing.
2.  **Source Guarding**: I will ensure the script uses a source guard (e.g., `if [[ "${BASH_SOURCE[0]}" == "${0}" ]]`) so that the function can be included in the test spec without executing the main logic.
3.  **Comprehensive Cases**: I'll implement tests for:
    *   **Success paths**: Valid arguments, flags, and default values.
    *   **Error paths**: Missing required values (e.g., `--name` without a value) and unknown flags.
    *   **Boundary cases**: Empty input or unexpected argument order.

I have reviewed your existing `lib/args_parser.sh` and `spec/args_parser_spec.sh`. The current implementation already follows these best practices.

Would you like me to add more specific test cases (e.g., handling combined flags like `-vn` or testing the output of specific help messages), or should I proceed with creating a plan to verify and expand the existing suite?

<skill-used name="shellspec" verdict="HELPFUL" reason="The project already uses ShellSpec, and the recommended isolation pattern for 'exit' testing matches the existing spec/args_parser_spec.sh implementation."/>