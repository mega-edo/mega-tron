I have analyzed your request and the existing codebase. You already have a robust implementation of unit tests for the `parse_args` function in `spec/args_parser_spec.sh` using **ShellSpec**.

The current tests correctly handle functions that `exit` by using the `When run` directive, which executes the function in a subshell, allowing ShellSpec to capture the exit status and stderr without terminating the test runner.

### Proposed Strategy

I recommend two possible approaches depending on your goals:

#### Option 1: Enhance Existing Tests (Status Quo)
Continue using the `When run` approach. It is the most direct way to test existing code that relies on `exit`. I can add more edge cases (e.g., duplicate flags, invalid value types) to ensure complete coverage.

*   **Pros:** Requires no changes to production code.
*   **Cons:** Slightly slower than `When call` because it spawns a subshell for every test.

#### Option 2: Refactor for Better Testability (Recommended for Libraries)
Refactor `parse_args` to use `return` instead of `exit`. In Bash, library functions should generally return a non-zero status and let the caller decide whether to exit. This allows you to use `When call` in tests, which is faster and provides better isolation.

*   **Pros:** Better software architecture; faster tests; function becomes more reusable.
*   **Cons:** Requires modifying the production script.

### Comparison

| Feature | `When run` (Current) | `When call` (Refactored to `return`) |
| :--- | :--- | :--- |
| **Speed** | Slower (subshell) | Faster (same shell) |
| **Exit Handling** | Captures `exit` status | Captures `return` status |
| **Isolation** | High (separate process) | Moderate (same process) |
| **Code Impact** | None | Requires replacing `exit` with `return` |

Which approach would you like me to proceed with? If you're open to refactoring, I can show you how to maintain the `exit` behavior for CLI usage while using `return` for library usage.

---
<skill-used name="shellspec" verdict="HELPFUL" reason="Provided the distinction between 'When call' and 'When run', and identified 'Pattern 3' regarding exit vs return for testability."/>I have analyzed your request and developed a plan to improve the unit testing of your bash function.

While your current tests work correctly using `When run` to catch `exit` calls, refactoring the function to use `return` instead of `exit` is a best practice for bash libraries. This makes the function more reusable and allows for faster, more isolated tests using `When call`.

Here is the full plan:

# Plan: Refactor Argument Parser for Enhanced Testability

This plan aims to improve the testability and reusability of the `parse_args` function by replacing `exit` calls with `return` and updating the test suite to use faster ShellSpec execution modes.

## Objective
- Refactor `lib/args_parser.sh` to use `return` for error handling inside functions.
- Update `spec/args_parser_spec.sh` to use `When call` instead of `When run`.
- Expand test coverage with additional edge cases.

## Key Files & Context
- `lib/args_parser.sh`: Contains the argument parsing logic.
- `spec/args_parser_spec.sh`: Contains the existing ShellSpec tests.

## Implementation Steps

### 1. Refactor `lib/args_parser.sh`
- Replace all instances of `exit 1` inside the `parse_args` function with `return 1`.
- Ensure the source guard at the bottom of the file still handles the exit status correctly for CLI usage:
  ```bash
  if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    parse_args "$@" || exit $?
  fi
  ```

### 2. Update `spec/args_parser_spec.sh`
- Change `When run parse_args ...` to `When call parse_args ...` for all tests.
- Note that `When call` captures stdout/stderr and the return status without spawning a subshell, making tests faster and more reliable.

### 3. Expand Test Coverage
- Add a test case for multiple occurrences of `--verbose`.
- Add a test case for valid arguments with different values.

## Verification & Testing
- Run `shellspec` to ensure all tests pass.
- Verify that `lib/args_parser.sh` still works as a standalone script by running it manually with valid and invalid arguments.

---

Does this plan meet your requirements? If you agree, I will proceed with the implementation.

---
<skill-used name="shellspec" verdict="HELPFUL" reason="Followed 'Pattern 3' for Exit Call Interception and 'Execution Modes' advice to prefer 'When call' over 'When run' for unit tests."/>I have completed the unit testing setup for your bash function.

Following best practices for bash library development, I refactored the `parse_args` function to use `return` instead of `exit`. This allows the function to be easily tested in isolation while still maintaining its ability to exit with the correct status when run as a standalone script.

### Changes Made:

1.  **Refactored `lib/args_parser.sh`**:
    *   Replaced `exit 1` calls within the `parse_args` function with `return 1`.
    *   Ensured the source guard (`if [[ "${BASH_SOURCE[0]}" == "${0}" ]]`) continues to handle the exit status for CLI usage.

2.  **Updated `spec/args_parser_spec.sh`**:
    *   Migrated all test cases from `When run` (subshell execution) to `When call` (faster, same-shell execution).
    *   Added new test cases to verify multiple `--verbose` flags and different input values.
    *   The test suite now includes 10 comprehensive unit tests, all of which are passing.

### Verification Results:

```bash
$ ./shellspec_local/bin/shellspec spec/args_parser_spec.sh
Running: /bin/sh [bash 3.2.57(1)-release]
..........

Finished in 0.20 seconds (user 0.20 seconds, sys 0.04 seconds)
10 examples, 0 failures
```

You can now continue adding features to your parser with the confidence that any regressions in argument handling will be immediately caught by the unit tests.

---
<skill-used name="shellspec" verdict="HELPFUL" reason="The implementation strictly followed the skill's recommendations: using source guards, preferring 'return' over 'exit' in library functions (Pattern 3), and using 'When call' for faster unit tests."/>