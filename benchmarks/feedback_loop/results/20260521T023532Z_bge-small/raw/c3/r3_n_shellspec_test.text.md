I've analyzed your request to unit-test a Bash function that exits on bad input.

In ShellSpec, testing functions that call `exit` requires using the `run` directive instead of `call`. This is because `run` executes the function in a **subshell**, allowing ShellSpec to capture the exit status and output without terminating the test runner itself.

### Proposed Strategy
1.  **Modify `lib/args_parser.sh`**: I will update the `parse_args` function to use `exit 1` instead of `return 1` for error cases, strictly adhering to your requirement that the function "exits on bad input."
2.  **Update `spec/args_parser_spec.sh`**: I will transition the test suite from `When call` to `When run`. This ensures that even when `exit` is called, the test suite continues to run and can verify the status code.
3.  **Add Integration Tests**: I will add a new test section to verify the script's behavior when executed directly (e.g., `./lib/args_parser.sh --name "Test"`), ensuring the entry point correctly handles the exit logic.

### Plan Summary
The full implementation plan is stored in `plans/test-exit-function.md`.

```markdown
# Plan: Unit-Testing a Bash Function that Exits

## Objective
- Update `lib/args_parser.sh` to use `exit 1` for error conditions in `parse_args()`.
- Update `spec/args_parser_spec.sh` to use `When run` instead of `When call`.

## Implementation Steps
1. **Modify `lib/args_parser.sh`**: Replace `return 1` with `exit 1`.
2. **Update `spec/args_parser_spec.sh`**: Change `When call parse_args ...` to `When run parse_args ...`.
3. **Add Script-Level Tests**: Use `run script lib/args_parser.sh` to verify full script execution.
```

Does this approach meet your requirements, or would you prefer to keep `return` in the function and only test the `exit` behavior at the script level?

<skill-used name="shellspec" verdict="HELPFUL" reason="Provided guidance on Pattern 3: Exit Call Interception and the difference between run and call for testing functions that exit."/>
<skill-used name="regex-log" verdict="NEUTRAL" reason="Not applicable to the current task of bash argument parsing and testing."/>
<skill-used name="test-fixing" verdict="NEUTRAL" reason="Not applicable as there are no failing tests to fix yet."/>