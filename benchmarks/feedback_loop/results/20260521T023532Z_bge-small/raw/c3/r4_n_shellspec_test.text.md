I have researched the current implementation of `lib/args_parser.sh` and the existing `shellspec` tests. I've also consulted the `shellspec` skill for best practices on testing functions that use `exit`.

While your current function uses `return` (which is generally preferred for library functions), I've prepared a plan to demonstrate how to test it if it used `exit` directly, as per your request. 

### Proposed Strategy

We will use **ShellSpec** to test the function. The key insight is that ShellSpec's `When call` executes the function in an isolated subshell. When the function calls `exit`, it terminates the subshell, but ShellSpec catches the exit code and reports it as the `status` without breaking the rest of the test suite.

**I propose the following plan:**

1.  **Refactor `lib/args_parser.sh`**: Update the `parse_args` function to use `exit 1` instead of `return 1` on errors to directly address your requirement.
2.  **Update `spec/args_parser_spec.sh`**: Ensure the existing tests (which use `When call`) correctly handle the `exit` calls by verifying the `status`.
3.  **Add Integration Tests**: Add a `Describe` block using `When run script` to test the script as a standalone executable, ensuring the exit codes are correctly propagated to the OS.

**Plan Content:**

```markdown
# Plan: Unit-Testing Bash Functions with Exit Calls

The goal is to demonstrate and implement unit tests for a Bash function that parses arguments and uses `exit` on failure, while ensuring the test suite remains stable and isolated.

## Proposed Strategy

We will use **ShellSpec** to implement these tests. We will focus on two main scenarios:
1.  **Unit Testing the Function**: Testing the `parse_args` function directly using `When call`.
2.  **Integration Testing the Script**: Testing the script execution (entry point) using `When run script`.

### 1. Handling `exit` in Functions
In ShellSpec, `When call` runs the function in a subshell. If the function calls `exit`, it terminates the subshell but **not** the test suite. ShellSpec captures the exit status of the subshell and makes it available as `status`.

## Changes

### 1. `lib/args_parser.sh`
Modify `parse_args` to use `exit 1` on error instead of `return 1`.

### 2. `spec/args_parser_spec.sh`
*   Update existing tests to verify they still pass when the function uses `exit`.
*   Add a new `Describe` block for "Script Execution" using `When run script` to test the full lifecycle.

## Verification Plan
*   Run `shellspec spec/args_parser_spec.sh` to ensure all unit tests pass.
*   Verify that `The status should equal 1` correctly catches the `exit 1`.
```

Does this strategy align with what you're looking for? If so, I will proceed with the implementation.

<skill-used name="shellspec" verdict="HELPFUL" reason="Provided the critical insight that When call handles exit via subshell isolation, which is the core of the proposed solution."/>
<skill-used name="test-fixing" verdict="NEUTRAL" reason="Not explicitly used as the task was about adding/modifying tests, but the general grouping logic is good context."/>