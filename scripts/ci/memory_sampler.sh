#!/usr/bin/env bash
# The memory sampler of the CI test jobs.
#
# Usage:
#   memory_sampler.sh run    Sample in a loop. Run it in the background.
#   memory_sampler.sh line   Print one sample line and stop.
#
# "run" reads the memory files of this container once a second. It keeps the
# highest values in $RUNNER_TEMP/memory-samples.txt for the "Print peak memory"
# step. Every MEMORY_LOG_EVERY seconds (default 15) it also adds one short line
# to $RUNNER_TEMP/memory-log.txt. The pytest step shows that file in the job
# log with tail (see ci.yml). GitHub keeps the log lines it already got, so
# the lines are still there when the runner is killed for memory. The file
# itself is lost with the container.
#
# A line has the time, the memory in use, the limit, and the pytest worker
# that holds the most memory. Above 85 % of the limit it adds one line for
# each worker: its memory and the test it runs at that moment. Each worker
# writes its process id and its running test to a progress file (see
# tests/conftest.py, MEHO_CI_PROGRESS_DIR), so only those processes count:
# a bigger process that is not a pytest worker is never named. Without
# progress files the line says so.
#
# It never fails the job. Settings (for tests): MEMORY_CG_DIR, RUNNER_TEMP,
# MEMORY_LOG_EVERY, MEMORY_PROGRESS_DIR, MEMORY_WARN_PERCENT, MEMORY_PROC_DIR.
set -u

cg="${MEMORY_CG_DIR:-/sys/fs/cgroup}"
tmp="${RUNNER_TEMP:-/tmp}"
every="${MEMORY_LOG_EVERY:-15}"
progress_dir="${MEMORY_PROGRESS_DIR:-${MEHO_CI_PROGRESS_DIR:-${tmp}/pytest-progress}}"
warn_percent="${MEMORY_WARN_PERCENT:-85}"
proc="${MEMORY_PROC_DIR:-/proc}"

mib() { echo "$(( $1 / 1048576 ))"; }

# The memory of a process in MiB, from /proc/<pid>/status. Empty when the
# process has ended.
rss_mib() {
  local key value rest
  [ -r "${proc}/$1/status" ] || return 0
  while read -r key value rest; do
    if [ "${key}" = "VmRSS:" ]; then
      echo "$(( value / 1024 ))"
      return 0
    fi
  done < "${proc}/$1/status" 2>/dev/null
}

# Read the progress files. Sets workers: one "<name> <pid> <MiB> <test>"
# line for each file; MiB is "-" when the process has ended.
read_workers() {
  local f pid test mib
  workers=""
  [ -d "${progress_dir}" ] || return 0
  for f in "${progress_dir}"/*.txt; do
    [ -r "${f}" ] || continue
    pid=""
    test=""
    read -r pid test < "${f}"
    case "${pid}" in
      ''|*[!0-9]*) continue ;;
    esac
    mib="$(rss_mib "${pid}")"
    workers+="$(basename "${f}" .txt) ${pid} ${mib:--} ${test}"$'\n'
  done
}

# The pytest worker with the most memory: "<name> pid <pid> <MiB> MiB".
largest_worker() {
  local name pid mib test best="" best_mib=-1
  while read -r name pid mib test; do
    [ -n "${name}" ] || continue
    case "${mib}" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "${mib}" -gt "${best_mib}" ]; then
      best_mib=${mib}
      best="${name} pid ${pid} ${mib} MiB"
    fi
  done <<< "${workers}"
  echo "${best:-unknown}"
}

# One line for each worker: its memory and the test it runs now.
worker_tests() {
  local name pid mib test found=0
  while read -r name pid mib test; do
    [ -n "${name}" ] || continue
    found=1
    case "${mib}" in
      ''|*[!0-9]*) echo "[memory]   ${name} pid ${pid} (ended) ran: ${test}" ;;
      *) echo "[memory]   ${name} pid ${pid} ${mib} MiB runs: ${test}" ;;
    esac
  done <<< "${workers}"
  if [ "${found}" -eq 0 ]; then
    echo "[memory]   no worker progress files in ${progress_dir}, so the running tests are unknown"
  fi
}

# Print the line for the memory numbers given: used, limit ("max" or empty = none).
print_line() {
  local used="$1" limit="$2" stamp percent=""
  stamp="$(date -u +%H:%M:%S)"
  read_workers
  case "${limit}" in
    ''|*[!0-9]*) limit="" ;;
  esac
  if [ -n "${limit}" ] && [ "${limit}" -gt 0 ]; then
    percent=$(( used * 100 / limit ))
    echo "[memory] ${stamp} in use $(mib "${used}") MiB of $(mib "${limit}") MiB (${percent}%); largest pytest worker: $(largest_worker)"
    if [ "${percent}" -ge "${warn_percent}" ]; then
      echo "[memory] ${stamp} above ${warn_percent}% of the limit. Tests running now:"
      worker_tests
    fi
  else
    echo "[memory] ${stamp} in use $(mib "${used}") MiB, no limit known; largest pytest worker: $(largest_worker)"
  fi
}

read_limit() {
  local limit=""
  [ -r "${cg}/memory.max" ] && read -r limit < "${cg}/memory.max"
  echo "${limit}"
}

cmd_line() {
  local used inactive_file=0 key value
  if [ ! -r "${cg}/memory.current" ]; then
    echo "[memory] no cgroup v2 memory files here"
    return 0
  fi
  read -r used < "${cg}/memory.current"
  if [ -r "${cg}/memory.stat" ]; then
    while read -r key value; do
      [ "${key}" = "inactive_file" ] && inactive_file=${value}
    done < "${cg}/memory.stat"
  fi
  print_line "$(( ${used:-0} - inactive_file ))" "$(read_limit)"
}

cmd_run() {
  local total_max=0 ws_max=0 anon_max=0 n=0 total anon inactive_file key value ws limit
  if [ ! -r "${cg}/memory.current" ] || [ ! -r "${cg}/memory.stat" ]; then
    echo "No cgroup v2 memory files here, so no sampling."
    return 0
  fi
  limit="$(read_limit)"
  while :; do
    read -r total < "${cg}/memory.current"
    anon=0
    inactive_file=0
    while read -r key value; do
      case "${key}" in
        anon) anon=${value} ;;
        inactive_file) inactive_file=${value} ;;
      esac
    done < "${cg}/memory.stat"
    total=${total:-0}
    ws=$(( total - inactive_file ))
    if [ "${total}" -gt "${total_max}" ]; then total_max=${total}; fi
    if [ "${ws}" -gt "${ws_max}" ]; then ws_max=${ws}; fi
    if [ "${anon}" -gt "${anon_max}" ]; then anon_max=${anon}; fi
    n=$(( n + 1 ))
    echo "${total_max} ${ws_max} ${anon_max} ${n}" > "${tmp}/memory-samples.tmp"
    mv -f "${tmp}/memory-samples.tmp" "${tmp}/memory-samples.txt"
    # The working set is what the kernel cannot free first, so the line shows it.
    if [ $(( (n - 1) % every )) -eq 0 ]; then
      print_line "${ws}" "${limit}" >> "${tmp}/memory-log.txt"
    fi
    sleep 1
  done
}

case "${1:-}" in
  run) cmd_run ;;
  line) cmd_line ;;
  *)
    echo "usage: $0 run|line" >&2
    exit 2
    ;;
esac
exit 0
