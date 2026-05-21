I have analyzed the requirement. To unit-test a Bash function that uses `exit`, we must use ShellSpec's `When run` syntax instead of `When call`. `When run` executes the function in a subshell, which allows ShellSpec to capture the exit status without terminating the test runner.

### Proposed Strategy
1.  **Modify `lib/args_parser.sh`**: Change the `parse_args` function to use `exit 1` instead of `return 1` for all error conditions. This aligns with your requirement for a function that "exits on bad input."
2.  **Update `spec/args_parser_spec.sh`**: Update all test cases to use `When run parse_args`. This ensures ShellSpec correctly traps the `exit` calls and verifies the status and output.

### Implementation Plan
The full plan has been drafted in `plans/unit_test_exit_function.md`:

```markdown
# Plan: Unit-test a Bash Function that Exits

The goal is to modify a Bash function to use `exit` for error handling (instead of `return`) and update the ShellSpec tests to correctly handle this behavior by using a subshell.

## User Requirements
- Unit-test a bash function that parses command-line arguments.
- The function should **exit** on bad input.

## Analysis
- **Current State**: `lib/args_parser.sh` uses `return 1` for errors. `spec/args_parser_spec.sh` uses `When call parse_args`, which runs in the same shell process and would crash ShellSpec if `exit` was called.
- **Proposed Solution**: 
    - Change `return 1` to `exit 1` in `lib/args_parser.sh`.
    - Change `When call parse_args` to `When run parse_args` in `spec/args_parser_spec.sh`. `When run` executes the function in a subshell, allowing ShellSpec to capture the exit status.

## Proposed Changes

### 1. lib/args_parser.sh
- Replace all instances of `return 1` within the `parse_args` function with `exit 1`.

### 2. spec/args_parser_spec.sh
- Replace all occurrences of `When call parse_args` with `When run parse_args`.

## Verification Plan

### Automated Tests
- Run the updated test suite using `shellspec_local/bin/shellspec`.
- All tests should pass, confirming that:
    - Valid arguments result in success (exit status 0).
    - Invalid arguments result in failure (exit status 1).
    - The output matches the expected error messages.
```

I will proceed with these changes upon your approval.

I have updated the argument parser to use `exit` for error handling and adjusted the unit tests to correctly verify this behavior.

### Key Changes
1.  **Modified `lib/args_parser.sh`**: The `parse_args` function now uses `exit 1` instead of `return 1` for all error conditions (missing required arguments, unknown options, or invalid flag values). This ensures the script terminates immediately on bad input.
2.  **Updated `spec/args_parser_spec.sh`**: Switched from `When call parse_args` to `When run parse_args`. In ShellSpec, `When run` executes the function in a subshell, which is necessary to capture an `exit` status without crashing the test runner.

### Verification Results
I ran the full test suite using ShellSpec, and all tests passed, confirming that the `exit` status and error messages are correctly captured:

```bash
$ ./shellspec_local/bin/shellspec spec/args_parser_spec.sh
Running: /bin/sh [bash 3.2.57(1)-release]
........

Finished in 0.17 seconds (user 0.16 seconds, sys 0.04 seconds)
8 examples, 0 failures
```

You can now use `When run <function_name>` in your ShellSpec tests whenever a function is expected to terminate the process.

<skill-used name="shellspec" verdict="HELPFUL" reason="The skill correctly identified that `When run` is required to capture `exit` calls in Bash functions, which was the core of the user's request. Used this knowledge to refactor `spec/args_parser_spec.sh` successfully."/>