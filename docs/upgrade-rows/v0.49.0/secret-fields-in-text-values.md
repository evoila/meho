## Note
The default answer of every operation also hides secret-named fields inside text values, for example a password in a JSON or YAML file of a ConfigMap or in the last-applied note of kubectl. `k8s.cr.list` and `k8s.cr.info` hide credential fields in `spec_excerpt`, `status_excerpt` and `fields` for every kind, and mask the Helm values of a `HelmChartConfig`.

## Action
None. A script that reads such a text value sees `"[REDACTED:param_name]"` in place of the secret. A person who needs the value sends the call with `reveal_secret: true`.
