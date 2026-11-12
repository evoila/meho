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
# A line has the time, the memory in use, the limit, and the process that
# holds the most memory. Above 85 % of the limit it adds one more line: the
# test that each pytest worker runs at that moment. The workers write that
# test name to a progress file (see tests/conftest.py,
# MEHO_CI_PROGRESS_DIR). Without progress files the line says so.
#
# It never fails the job. Settings (for tests): MEMORY_CG_DIR, RUNNER_TEMP,
# MEMORY_LOG_EVERY, MEMORY_PROGRESS_DIR, MEMORY_WARN_PERCENT.
set -u

cg="${MEMORY_CG_DIR:-/sys/fs/cgroup}"
tmp="${RUNNER_TEMP:-/tmp}"
every="${MEMORY_LOG_EVERY:-15}"
progress_dir="${MEMORY_PROGRESS_DIR:-${MEHO_CI_PROGRESS_DIR:-${tmp}/pytest-progress}}"
warn_percent="${MEMORY_WARN_PERCENT:-85}"

mib() { echo "$(( $1 / 1048576 ))"; }

# The process with the most memory: "<name> pid <pid> <MiB> MiB".
top_process() {
  local rss pid name
  if command -v ps >/dev/null 2>&1; then
    read -r rss pid name < <(ps -eo rss=,pid=,comm= --sort=-rss 2>/dev/null | head -n 1)
  fi
  if [ -z "${rss:-}" ]; then
    echo "unknown"
    return
  fi
  echo "${name} pid ${pid} $(( rss / 1024 )) MiB"
}

# One line for each worker: the test it runs now.
worker_tests() {
  local f found=0
  if [ -d "${progress_dir}" ]; then
    for f in "${progress_dir}"/*.txt; do
      [ -r "${f}" ] || continue
      found=1
      echo "[memory]   $(basename "${f}" .txt) runs: $(head -n 1 "${f}")"
    done
  fi
  if [ "${found}" -eq 0 ]; then
    echo "[memory]   no worker progress files in ${progress_dir}, so the running tests are unknown"
  fi
}

# Print the line for the memory numbers given: used, limit ("max" or empty = none).
print_line() {
  local used="$1" limit="$2" stamp percent=""
  stamp="$(date -u +%H:%M:%S)"
  case "${limit}" in
    ''|*[!0-9]*) limit="" ;;
  esac
  if [ -n "${limit}" ] && [ "${limit}" -gt 0 ]; then
    percent=$(( used * 100 / limit ))
    echo "[memory] ${stamp} in use $(mib "${used}") MiB of $(mib "${limit}") MiB (${percent}%); top process: $(top_process)"
    if [ "${percent}" -ge "${warn_percent}" ]; then
      echo "[memory] ${stamp} above ${warn_percent}% of the limit. Tests running now:"
      worker_tests
    fi
  else
    echo "[memory] ${stamp} in use $(mib "${used}") MiB, no limit known; top process: $(top_process)"
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
