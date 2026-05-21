# mega-tron codex() wrapper.
# Bypass:  MEGA_ROUTER=0 codex exec ...      (falls through to the real codex)
# Direct:  command codex exec ...            (also bypasses)
# Knobs:   MEGA_MODE=semantic|agentic  MEGA_SKILLS_DIR=...  MEGA_CODEX_HOME=...
#          MEGA_TOP_K=5   MEGA_BUDGET_TOK=1500  MEGA_QUIET=1
#
# Compatibility: codex-cli 0.130+ accepts the prompt as the trailing
# positional argument (the legacy `--prompt`/`--prompt=…` flag was removed
# in 0.130). The wrapper recognises both shapes so it works against
# old and new codex builds.

codex() {
  # Only intercept `codex exec`; everything else passes through unchanged.
  if [ "$1" != "exec" ] || [ "${MEGA_ROUTER:-1}" = "0" ]; then
    command codex "$@"
    return $?
  fi
  shift

  # Walk codex-cli flags so we can separate prompt from passthrough args
  # without needing the obsolete `--prompt`. Flags that take a value are
  # recognised explicitly; anything not matched and not starting with `-`
  # is treated as the positional prompt (codex accepts at most one).
  local _mega_prompt=""
  local _mega_passthru=()
  local _mega_have_prompt=0
  while [ $# -gt 0 ]; do
    case "$1" in
      # Legacy explicit prompt flag (codex < 0.130).
      --prompt) _mega_prompt="$2"; _mega_have_prompt=1; shift 2 ;;
      --prompt=*) _mega_prompt="${1#--prompt=}"; _mega_have_prompt=1; shift ;;
      # End-of-options marker — everything after is positional/passthrough.
      --) _mega_passthru+=("$1"); shift
          while [ $# -gt 0 ]; do _mega_passthru+=("$1"); shift; done ;;
      # Flags that take a value (must consume two tokens).
      -c|--config|-m|--model|-i|--image|-s|--sandbox|-C|--cd|--profile|--add-dir|--enable|--disable|--fallback-model|--effort|--debug|--debug-file|--last-message-file|--output-schema|--system-prompt|--system-prompt-file|--append-system-prompt|--append-system-prompt-file)
        _mega_passthru+=("$1" "$2"); shift 2 ;;
      # Other flags (boolean / --flag=value form) → single token.
      -*) _mega_passthru+=("$1"); shift ;;
      # First bare positional is the prompt.
      *)
        if [ "$_mega_have_prompt" = "0" ]; then
          _mega_prompt="$1"; _mega_have_prompt=1
        else
          _mega_passthru+=("$1")
        fi
        shift ;;
    esac
  done

  # No prompt → nothing to route (e.g. `codex exec resume`). Pass through.
  if [ "$_mega_have_prompt" = "0" ] || [ -z "$_mega_prompt" ] || [ "$_mega_prompt" = "-" ]; then
    command codex exec "${_mega_passthru[@]}"
    return $?
  fi

  # Per-PID staging dir so parallel calls don't trample each other.
  local _mega_root="${MEGA_CODEX_HOME:-__MEGA_DEFAULT_CODEX_HOME__}"
  local _mega_target="${_mega_root}/$$"
  mkdir -p "$_mega_target"

  # Stage and capture the must-use prefix. If staging fails for any reason
  # (CLI missing, model not downloaded, etc.), the prefix stays empty and
  # the user's codex call runs unrouted — the wrapper never breaks the
  # workflow.
  local _mega_skills_args=()
  if [ -n "${MEGA_SKILLS_DIR:-__MEGA_DEFAULT_SKILLS_DIR__}" ]; then
    _mega_skills_args+=(--skills-dir "${MEGA_SKILLS_DIR:-__MEGA_DEFAULT_SKILLS_DIR__}")
  fi
  local _mega_prefix
  _mega_prefix=$(mega-tron search \
      "$_mega_prompt" \
      --output stage \
      --target "$_mega_target" \
      --top-k "${MEGA_TOP_K:-__MEGA_DEFAULT_TOP_K__}" \
      --budget-tok "${MEGA_BUDGET_TOK:-__MEGA_DEFAULT_BUDGET_TOK__}" \
      "${_mega_skills_args[@]}" 2>/dev/null) || _mega_prefix=""

  # Bash command substitution strips trailing newlines from the captured
  # prefix; the staged prefix ends with `\n\n` so prompt and must-use line
  # stay on separate lines. Re-attach a blank-line separator manually when
  # the prefix is non-empty so the model doesn't see `task.<prompt>` glued
  # together.
  if [ -n "$_mega_prefix" ]; then
    _mega_prefix="${_mega_prefix}"$'\n\n'
  fi

  # codex-cli 0.130+ takes the prompt as the trailing positional. We
  # pass it last so any flags the user supplied come first.
  CODEX_HOME="$_mega_target" command codex exec \
    "${_mega_passthru[@]}" "${_mega_prefix}${_mega_prompt}"
  local _mega_rc=$?

  # Best-effort cleanup; the embedding cache persists elsewhere.
  rm -rf "$_mega_target" 2>/dev/null
  return $_mega_rc
}
