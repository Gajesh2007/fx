#!/usr/bin/env bash
# Run E2E files one at a time, each in its own Bun process with its own tmux
# server, so terminal fixtures and process state cannot leak between files.
# A failing file gets one retry after its tmux server is reset. A file that
# passes only on retry is reported as a warning so flaky files stay visible.
#
# An entry is `<test-file>` or `<test-file>#<part>/<count>` (1 <= part <=
# count <= 16). A part entry runs only the tests whose full name length is
# congruent to part - 1 modulo count, matching partPattern() in ci-shards.ts.
set -uo pipefail

usage() {
  printf 'usage: %s <test-file>[#<part>/<count>]...\n' "$0" >&2
  exit 2
}

if [ "$#" -eq 0 ]; then
  usage
fi

cd "$(dirname "$0")" || exit 1

tmux_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/fx-e2e-tmux"
status=0
index=0

parse_entry() {
  local entry="$1"
  local part
  local count
  RUN_FILE="$entry"
  RUN_PATTERN=''
  if [[ $entry == *#* ]]; then
    if [[ ! $entry =~ ^([^#]+)#([0-9]+)/([0-9]+)$ ]]; then
      printf 'invalid part entry: %s\n' "$entry" >&2
      usage
    fi
    RUN_FILE="${BASH_REMATCH[1]}"
    part="${BASH_REMATCH[2]}"
    count="${BASH_REMATCH[3]}"
    if (( part < 1 || count < 1 || count > 16 || part > count )); then
      printf 'invalid part suffix (need 1 <= part <= count <= 16): %s\n' "$entry" >&2
      usage
    fi
    # Same construction as partPattern() in ci-shards.ts.
    RUN_PATTERN="^(?:[\s\S]{$count})*[\s\S]{$((part - 1))}$"
  fi
}

run_test() {
  if [ -n "$RUN_PATTERN" ]; then
    # No --pass-with-no-tests: a part that selects nothing fails loudly.
    TMUX_TMPDIR="$tmux_dir" bun test --max-concurrency 1 -t "$RUN_PATTERN" "./$RUN_FILE"
  else
    TMUX_TMPDIR="$tmux_dir" bun test --max-concurrency 1 "./$RUN_FILE"
  fi
}

# Reject a bad entry before running any of them.
for entry in "$@"; do
  parse_entry "$entry"
done

for entry in "$@"; do
  parse_entry "$entry"
  tmux_dir="$tmux_root-$index"
  mkdir -p "$tmux_dir"
  if ! run_test; then
    printf 'Retrying failed E2E entry after resetting tmux: %s\n' "$entry"
    TMUX_TMPDIR="$tmux_dir" tmux kill-server 2>/dev/null || true
    if run_test; then
      printf '::warning title=E2E passed only on retry::%s failed and then passed on retry. Investigate it as a possible race.\n' "$entry"
    else
      status=1
    fi
  fi
  TMUX_TMPDIR="$tmux_dir" tmux kill-server 2>/dev/null || true
  index=$((index + 1))
done

exit "$status"
