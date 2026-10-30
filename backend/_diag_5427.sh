#!/usr/bin/env bash
# TEMPORARY (issue 5427): check how a container finds another one by its
# network alias on a runner. Removed again before the pull request is ready.
set -uo pipefail
TOOL="${MEHO_TEST_VAULT_IMAGE}"
BAO=$(uv run python -c 'import yaml; i = yaml.safe_load(open("../deploy/charts/meho/charts/openbao/values.yaml"))["image"]; print(f"{i[\"repository\"]}@{i[\"digest\"]}")')
docker pull -q "$BAO" >/dev/null
docker pull -q "$TOOL" >/dev/null

check() {
  local tag="$1"
  shift
  docker network create "diag-$tag" >/dev/null
  docker run -d --name "kc-$tag" --network "diag-$tag" --network-alias keycloak \
    -e SKIP_SETCAP=1 -e VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200 -e VAULT_DEV_ROOT_TOKEN_ID=diag \
    "$TOOL" server -dev >/dev/null
  sleep 5
  local want
  want=$(docker inspect -f "{{(index .NetworkSettings.Networks \"diag-$tag\").IPAddress}}" "kc-$tag")
  for image in "$TOOL" "$BAO"; do
    docker run --rm --network "diag-$tag" "$@" -e WANT="$want" --entrypoint sh "$image" -c '
      if grep -q "^search" /etc/resolv.conf; then echo "  search line: present, $(grep -c . /etc/resolv.conf) lines"; else echo "  search line: none"; fi
      i=0
      for d in $(sed -n "s/^search //p" /etc/resolv.conf); do
        i=$((i + 1))
        if nslookup "keycloak.$d." >/dev/null 2>&1; then echo "  keycloak + search domain $i: has an answer"; else echo "  keycloak + search domain $i: no answer"; fi
      done
      for n in 1 2 3; do
        got=$(getent hosts keycloak | cut -d" " -f1)
        if [ -z "$got" ]; then echo "  keycloak -> nothing"; elif [ "$got" = "$WANT" ]; then echo "  keycloak -> the container"; else echo "  keycloak -> ANOTHER address"; fi
      done
      if wget -T 5 -qO- http://keycloak:8200/v1/sys/health >/dev/null 2>&1; then echo "  http://keycloak:8200 -> ok"; else echo "  http://keycloak:8200 -> FAIL"; fi
    ' 2>&1 | sed "s|^|$tag $(basename "${image%%@*}") |"
  done
  docker rm -f "kc-$tag" >/dev/null
  docker network rm "diag-$tag" >/dev/null
}

echo "boot_id: $(cat /proc/sys/kernel/random/boot_id)"
check plain
check no-search --dns-search=.

passed=0
for i in $(seq 1 10); do
  if uv run pytest -q -o tmp_path_retention_policy=failed \
      tests/integration/test_core_vault_init_openbao.py >/tmp/ob.log 2>&1; then
    passed=$((passed + 1))
    echo "openbao run $i: passed"
  else
    echo "openbao run $i: FAILED"
    grep -E "VaultInitError: " /tmp/ob.log | head -2
  fi
done
echo "openbao with the fix: $passed of 10 runs passed"
check plain-again
