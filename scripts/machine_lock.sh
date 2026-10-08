# shellcheck shell=sh
# The machine's test lock (S099). SOURCED, never run: a recipe of the Makefile
# says `MACHINE_LOCK_LABEL=<target> . scripts/machine_lock.sh` and the shell of
# that recipe then holds one exclusive flock until it ends, with every process
# it starts. `make pytest`, `make pytest-db` and `make alerts` take it, so two
# sessions on one machine never run a test database, a suite or a rules check
# at once: the second waits and says who holds the lock, and one that waited
# too long runs nothing (exit 75). `make eval` and `make eval-baseline` reach
# it through `make pytest-db`.
#
# Why sourced: the recipes stay where the tests that read them expect them,
# `make -n` prints the line and takes no lock, and the kernel frees the lock
# when the last process of the run is gone, however it ended.
#
# What it reads:
#   MACHINE_LOCK_LABEL  who asks (the make target); for the messages only.
#   MERIDIAN_LOCK_WAIT  whole seconds to wait for a held lock (default 1800).
#                       0 does not wait; it is no way past a held lock.
#   MERIDIAN_LOCK_DIR   where the lock and its record live (default
#                       ~/.cache/meridian-locks). It exists for the tests of
#                       this file: another folder is another lock.
#
# What it is not: a lock on pytest. A test file run by hand (`uv run pytest
# <file>`) takes none, and neither does the cluster, which has a holder record
# of its own (`make cluster-holder`). Without flock on the machine it says so
# and the run goes on. It takes the lock in CI too, where nothing else holds
# it: GITHUB_ACTIONS is set by hand on a development machine before a push, so
# it cannot be the switch that turns a lock off.
#
# POSIX sh (the recipes run in /bin/sh). It uses descriptor 9 and names that
# start with machine_lock_.

machine_lock_label="${MACHINE_LOCK_LABEL:-a run}"
if ! command -v flock >/dev/null 2>&1; then
  echo "machine lock: no flock on this machine, $machine_lock_label runs without the lock" >&2
else
  machine_lock_wait="${MERIDIAN_LOCK_WAIT:-1800}"
  case "$machine_lock_wait" in
    *[!0-9]*)
      echo "machine lock: MERIDIAN_LOCK_WAIT is whole seconds, not '$machine_lock_wait'; nothing was run" >&2
      exit 64
      ;;
  esac
  machine_lock_dir="${MERIDIAN_LOCK_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/meridian-locks}"
  # dash leaves at a redirection that fails on exec; bash goes on, and flock
  # on a descriptor that is not open would read as a lock that is held.
  if ! mkdir -p "$machine_lock_dir" || ! exec 9>>"$machine_lock_dir/tests.lock"; then
    echo "machine lock: cannot open $machine_lock_dir/tests.lock; nothing was run" >&2
    exit 73
  fi
  if ! flock -n 9; then
    # The holder writes its record after it has the lock, so a run that asks
    # in between finds the last holder's line or none.
    machine_lock_holder="$(cat "$machine_lock_dir/tests.holder" 2>/dev/null || true)"
    machine_lock_holder="${machine_lock_holder:-(holder not yet recorded)}"
    echo "machine lock: held by $machine_lock_holder; $machine_lock_label in $PWD waits up to $machine_lock_wait s" >&2
    if ! flock -w "$machine_lock_wait" 9; then
      machine_lock_holder="$(cat "$machine_lock_dir/tests.holder" 2>/dev/null || true)"
      echo "machine lock: still held after $machine_lock_wait s by ${machine_lock_holder:-(holder not yet recorded)}; nothing was run" >&2
      exit 75
    fi
  fi
  printf '%s in %s since %s (pid %s)\n' "$machine_lock_label" "$PWD" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" >"$machine_lock_dir/tests.holder"
fi
