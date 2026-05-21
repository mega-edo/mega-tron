I have analyzed your request and the existing codebase. To unit-test a bash function that calls `exit`, the most effective strategy with **ShellSpec** is to wrap the function call in a **subshell**. This prevents the test runner itself from exiting when the function terminates.

### Proposed Strategy

1.  **Source Guarding:** Ensure the script uses a guard (e.g., `if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then ... fi`) so that `Include` in ShellSpec only loads the function definition without executing the script logic.
2.  **Isolation Wrapper:** Define a helper function within your spec file that calls the target function inside parentheses `( function_name "$@" )`. This forces the `exit` to only terminate the subshell.
3.  **Status and Error Verification:** Use ShellSpec's `The status should eq ...` and `The error should eq ...` to verify the exit code and the message sent to `stderr`.

### Plan

- **Step 1: Verify Script Structure** (Completed): `lib/args_parser.sh` already implements a source guard and the `parse_args` function.
- **Step 2: Draft/Update Spec File**: I will create or update a spec file that implements the subshell isolation pattern for testing the failure cases.
- **Step 3: Execution**: Run ShellSpec to verify the tests pass.

I will now prepare the formal implementation plan.

<skill-used name="shellspec" verdict="HELPFUL" reason="The skill provided the necessary patterns for testing bash functions that exit using subshells and status/error expectations."/>