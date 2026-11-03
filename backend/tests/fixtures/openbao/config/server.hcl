# The server config the chart renders (charts/openbao/templates/configmap.yaml),
# with the addresses the StatefulSet gives through the environment. Used by
# tests/integration/test_core_vault_init_openbao.py. It lives in the repo, not
# in a temp folder: the CI runner's Docker sees only the checkout.
ui           = false
api_addr     = "http://openbao:8200"
cluster_addr = "https://openbao:8201"
storage "raft" {
  path    = "/openbao/file"
  node_id = "openbao-0"
}
listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable     = 1
}
audit "file" "stdout" {
  options {
    file_path = "stdout"
  }
}
