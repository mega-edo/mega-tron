I have researched the current implementation of the argument parser in `lib/args_parser.sh` and the existing tests in `spec/args_parser_spec.sh`. The tests correctly use ShellSpec's `run` command, which executes the function in a subshell, safely handling the `exit` calls.

I propose to enhance the test suite by:
1.  **Refactoring with `Parameters`**: Using ShellSpec's data-driven testing feature to group validation failures and successful parses, making the tests more maintainable and readable.
2.  **Adding Edge Cases**: Testing for repeated flags (e.g., `--name A --name B`) and ensuring robust handling of empty values or combined flags.
3.  **Mocking Example**: Providing an example of the "mock exit" pattern (replacing `exit` with `return` in a test-specific mock) to demonstrate how to test such functions without subshells if ever needed.

Does this strategy sound good to you? Once you confirm, I will finalize the plan and proceed to implementation.

<skill-used name="shellspec" verdict="HELPFUL" reason="The skill provided the critical 'Exit Call Interception' pattern and best practices for using 'When run' vs 'When call' with functions that exit."/>