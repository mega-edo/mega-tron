I have analyzed the existing `lib/args_parser.sh` and `spec/args_parser_spec.sh` files. The current setup already uses **ShellSpec**, which is the recommended framework for unit-testing Bash functions.

### Proposed Strategy

To effectively test functions that use `exit`, I recommend the following patterns already partially implemented in your codebase:

1.  **Isolation via Subshells**: Since `exit` terminates the current shell process, calling a function that exits directly within ShellSpec would kill the test runner. Wrapping the call in a subshell `( function_name )` allows you to capture the exit status and `stderr` without crashing the tests.
2.  **Source Guards**: The `lib/args_parser.sh` file uses a source guard (`if [[ "${BASH_SOURCE[0]}" == "${0}" ]]`). This is critical as it allows ShellSpec to `Include` the file (sourcing it) to load the function into the test environment without actually executing the script logic.
3.  **Parameterized Testing**: For handling multiple "bad input" cases, ShellSpec's `Parameters` block is the most efficient way to maintain clean and DRY (Don't Repeat Yourself) tests.

### Plan

I will create a formal plan to enhance these tests, ensuring we cover edge cases for the `parse_args` function.

1.  **Refine Helper**: Ensure the `parse_args_isolated` helper is used consistently for all tests where an `exit` might occur.
2.  **Add Edge Cases**: Expand the `Parameters` block to include more variations of invalid input.
3.  **Verification**: Run `shellspec` to confirm all tests pass.

Do you agree with this approach, or would you like to see a different testing framework?

<skill-used name="shellspec" verdict="HELPFUL" reason="The project already uses ShellSpec, and the skill provides the necessary patterns for testing Bash functions that exit (subshells and source guards)."/>