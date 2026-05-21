I have analyzed the current implementation of `lib/args_parser.sh` and the existing test suite in `spec/args_parser_spec.sh`.

The `parse_args` function is designed for testability with a source guard and uses `exit 1` for error states. The current tests in `spec/args_parser_spec.sh` already demonstrate the best practice for testing functions that call `exit`: wrapping the call in a **subshell** (e.g., `( parse_args "$@" )`) within a helper function. This prevents the `exit` command from terminating the ShellSpec process itself, allowing you to assert on both the error output and the exit status.

### Proposed Strategy

I will enhance the test suite to ensure comprehensive coverage and follow ShellSpec best practices:

1.  **Refine Isolated Testing**: Maintain the `parse_args_isolated` helper to safely capture `exit` behaviors.
2.  **Expand Test Coverage**:
    *   Add tests for combined short and long flags if applicable.
    *   Test edge cases like multiple `--name` calls or `--name` followed by a value that looks like a flag but is intended as a literal (if supported).
    *   Verify `stderr` output specifically for error messages.
3.  **Validation**: Run the existing and new tests using `shellspec` to ensure they pass and correctly handle the `exit` calls.

Since this is a **Directive** to unit-test the function, I will now proceed to create a detailed implementation plan.

---

### Plan: Enhance Unit Tests for Bash Argument Parser

**Objective:**
Improve the test suite for `parse_args` to robustly handle exit conditions and expand edge-case coverage.

**Key Files & Context:**
- `lib/args_parser.sh`: Contains the logic for `parse_args`.
- `spec/args_parser_spec.sh`: The ShellSpec test file.

**Implementation Steps:**
1.  **Modify `spec/args_parser_spec.sh`**:
    *   Ensure `parse_args_isolated` is used for all tests that might trigger an `exit`.
    *   Add a context for "Edge Cases" (e.g., empty argument list, redundant flags).
    *   Refine "Invalid Arguments" assertions to explicitly check `stderr` using `The error should...`.
2.  **Verify Tests**: Execute `shellspec` and confirm all specs pass.

**Verification & Testing:**
- Run `shellspec spec/args_parser_spec.sh`.
- Confirm that `exit 1` in the script is correctly caught as `status 1` in the test.

---

Do you agree with this plan for enhancing the argument parser tests?

I have analyzed the `lib/args_parser.sh` script and the existing `spec/args_parser_spec.sh` file. The current test suite already uses the recommended pattern of wrapping the function in a subshell—`( parse_args "$@" )`—to prevent `exit` calls from terminating the test runner.

I've created a plan to enhance these tests by adding more edge cases and refining the assertions for better coverage.

### Proposed Plan

1.  **Refine Helper**: Keep the `parse_args_isolated` helper to safely test `exit` behavior.
2.  **Add Coverage**:
    *   **Valid Cases**: Add tests for the `-v` short flag and no-argument defaults.
    *   **Edge Cases**: Test redundant flags (e.g., multiple `--verbose`).
    *   **Invalid Cases**: Ensure all error paths (missing `--name` value, unknown flags) are verified against `stderr` and `status 1`.
3.  **Verification**: Execute the suite using `shellspec`.

**Implementation Plan:** `/private/tmp/feedback_loop/c0/fake_home/.gemini/tmp/workspace/2beb5855-c81c-4d26-930e-b8cb38a7a5a1/plans/test-bash-arg-parser.md`

```markdown
# Implementation Plan - Bash Argument Parser Unit Testing

## Objective
Thoroughly test `parse_args` in `lib/args_parser.sh`, specifically handling its `exit` behavior for invalid inputs.

## Key Files
- `lib/args_parser.sh`
- `spec/args_parser_spec.sh`

## Implementation Steps
1. **Enhance Test Structure**: Use `parse_args_isolated() { ( parse_args "$@" ); }` for all exit-prone paths.
2. **Expand Tests**:
    - Default behavior (no args).
    - Short flags ( `-v`).
    - Redundant flags.
3. **Refine Assertions**: Explicitly check `stderr` for errors and `status 1` for failures.
```

Do you agree with this plan? If so, I will proceed with the implementation.

<skill-used name="shellspec" verdict="HELPFUL" reason="The existing spec file correctly implemented the subshell wrapper pattern recommended by ShellSpec for testing functions that call exit."/>