#!/usr/bin/env bash
# TEMPORARY (issue 5427): look at what the k3s tests leave behind on a
# runner. Removed again before the pull request is ready.
set -uo pipefail
MODE="${1:?full or startstop}"
TOOL="${MEHO_TEST_VAULT_IMAGE}"
K3S="${MEHO_TEST_K3S_IMAGE}"

mask() { sed -E 's/([0-9]{1,3}\.[0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}/\1.x.x/g; s/([0-9a-f]{2}:){5}[0-9a-f]{2}/xx:xx/g'; }

snap() {
  echo "::group::snapshot $1"
  echo "boot_id: $(cat /proc/sys/kernel/random/boot_id)"
  echo "kernel: $(uname -r)"
  docker version --format 'docker server {{.Server.Version}}'
  docker info --format 'cgroup={{.CgroupDriver}} v{{.CgroupVersion}} fw={{.FirewallBackend}} sec={{.SecurityOptions}}' 2>&1
  docker ps -a --format '{{.ID}} {{.Image}} {{.Status}} {{.Names}}'
  docker network ls
  for n in $(docker network ls -q); do docker network inspect "$n" --format '{{.Name}} {{.Driver}} ipv6={{.EnableIPv6}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' | mask; done
  docker run --rm --privileged --net=host --pid=host --cgroupns=host --entrypoint sh "$K3S" -c '
    export PATH=$PATH:/bin/aux
    echo "-- links"; ip -o link 2>&1 | cut -d" " -f2,3 | head -60
    echo "-- addrs"; ip -o -4 addr 2>&1 | awk "{print \$2, \$4}" | head -60
    echo "-- routes"; ip route 2>&1 | head -40
    echo "-- sysctl"; for f in /proc/sys/net/ipv4/ip_forward /proc/sys/net/bridge/bridge-nf-call-iptables /proc/sys/net/bridge/bridge-nf-call-ip6tables /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max /proc/sys/net/ipv4/conf/all/rp_filter /proc/sys/net/ipv4/conf/all/route_localnet /proc/sys/fs/inotify/max_user_instances /proc/sys/fs/inotify/max_user_watches; do echo "$f=$(cat $f 2>&1)"; done
    echo "-- modules"; cut -d" " -f1 /proc/modules 2>&1 | sort | tr "\n" " "; echo
    echo "-- iptables (filter, nat) legacy"; iptables-legacy-save 2>&1 | grep -v "^#" | head -150
    echo "-- iptables nft"; iptables-nft-save 2>&1 | grep -v "^#" | head -150
    echo "-- ip6tables nft"; ip6tables-nft-save 2>&1 | grep -v "^#" | head -60
    echo "-- nft"; nft list ruleset 2>&1 | head -200
    echo "-- cgroup root"; ls /sys/fs/cgroup 2>&1 | tr "\n" " "; echo; cat /sys/fs/cgroup/cgroup.subtree_control 2>&1
    echo "-- processes"; ps -o pid,comm 2>/dev/null | awk "{print \$2}" | sort | uniq -c | sort -rn | head -40
  ' 2>&1 | mask
  echo "::endgroup::"
}

alias_check() {
  local tag="$1"
  echo "::group::alias check $tag"
  docker network create "diag-$tag" >/dev/null
  docker run -d --name "srv-$tag" --network "diag-$tag" --network-alias srv \
    -e SKIP_SETCAP=1 -e VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200 -e VAULT_DEV_ROOT_TOKEN_ID=diag \
    "$TOOL" server -dev >/dev/null
  sleep 8
  docker run --rm --network "diag-$tag" --entrypoint sh "$TOOL" -c '
    echo "-- resolv.conf"; cat /etc/resolv.conf
    for i in 1 2 3; do
      if nslookup srv >/tmp/ns 2>&1; then echo "dns srv: ok"; else echo "dns srv: FAIL"; cat /tmp/ns; fi
      if wget -T 5 -qO- http://srv:8200/v1/sys/health >/dev/null 2>/tmp/w; then echo "http by name: ok"; else echo "http by name: FAIL $(cat /tmp/w)"; fi
    done
    ip=$(getent hosts srv | cut -d" " -f1)
    echo "getent srv: ${ip:-none}"
    if [ -n "$ip" ]; then
      if wget -T 5 -qO- "http://$ip:8200/v1/sys/health" >/dev/null 2>/tmp/w; then echo "http by ip: ok"; else echo "http by ip: FAIL $(cat /tmp/w)"; fi
    fi
  ' 2>&1 | mask
  docker rm -f "srv-$tag" >/dev/null
  docker network rm "diag-$tag" >/dev/null
  echo "::endgroup::"
}

openbao_once() {
  echo "::group::openbao test $1"
  if uv run pytest -q -rs -o tmp_path_retention_policy=failed \
      tests/integration/test_core_vault_init_openbao.py >/tmp/ob.log 2>&1; then
    echo "openbao $1: passed"
  else
    echo "openbao $1: FAILED"
    grep -E "VaultInitError|passed|failed" /tmp/ob.log | head -10
  fi
  echo "::endgroup::"
}

snap before
alias_check before
openbao_once before

if [ "$MODE" = full ]; then
  echo "::group::k3s test file"
  uv run pytest -q -o tmp_path_retention_policy=failed tests/integration/test_connectors_k8s_k3d.py 2>&1 | tail -3
  echo "::endgroup::"
else
  echo "::group::k3s start and stop"
  uv run python - <<'PY'
import os
import time

from testcontainers.k3s import K3SContainer

c = K3SContainer(image=os.environ["MEHO_TEST_K3S_IMAGE"])
c.start()
print("k3s started", flush=True)
time.sleep(30)
c.stop()
print("k3s stopped", flush=True)
PY
  echo "::endgroup::"
fi

sleep 15
snap after
alias_check after
openbao_once after
alias_check after-openbao
