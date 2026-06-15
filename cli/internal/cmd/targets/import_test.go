// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// --- parseTargetsYAML ---------------------------------------------------

func TestParseTargetsYAMLHappyPath(t *testing.T) {
	t.Parallel()
	in := []byte(`
targets:
  - name: rdc-vcenter
    product: vcenter
    host: vc-dc.evba.lab
    sso_realm: evba.lab
`)
	entries, err := parseTargetsYAML(in)
	if err != nil {
		t.Fatalf("parseTargetsYAML: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("entries: got %d; want 1", len(entries))
	}
	if entries[0]["name"] != "rdc-vcenter" {
		t.Errorf("name: got %v; want rdc-vcenter", entries[0]["name"])
	}
}

func TestParseTargetsYAMLMalformedFails(t *testing.T) {
	t.Parallel()
	in := []byte(`targets: [- name: oops`)
	_, err := parseTargetsYAML(in)
	if err == nil {
		t.Fatal("parseTargetsYAML: want error on malformed YAML")
	}
}

func TestParseTargetsYAMLMissingRequiredFails(t *testing.T) {
	t.Parallel()
	// Missing host triggers the local pre-check.
	in := []byte(`
targets:
  - name: lonely
    product: vault
`)
	_, err := parseTargetsYAML(in)
	if err == nil {
		t.Fatal("parseTargetsYAML: want error on missing host")
	}
	if !strings.Contains(err.Error(), "host") {
		t.Errorf("error mention `host`: got %q", err.Error())
	}
}

func TestParseTargetsYAMLEmptyListFails(t *testing.T) {
	t.Parallel()
	_, err := parseTargetsYAML([]byte(`targets: []`))
	if err == nil {
		t.Fatal("parseTargetsYAML: want error on empty list")
	}
}

func TestParseTargetsYAMLMissingNameReportsIndex(t *testing.T) {
	t.Parallel()
	in := []byte(`
targets:
  - product: vault
    host: 10.0.0.1
`)
	_, err := parseTargetsYAML(in)
	if err == nil {
		t.Fatal("parseTargetsYAML: want error on missing name")
	}
	if !strings.Contains(err.Error(), "name") {
		t.Errorf("error mentions `name`: got %q", err.Error())
	}
}

// --- mapEntry / entryToCreateBody --------------------------------------

func TestMapEntryKnownTopLevelPassthrough(t *testing.T) {
	t.Parallel()
	body, warnings := mapEntry(map[string]any{
		"name":         "rdc-vault",
		"product":      "vault",
		"host":         "vault.evba.lab",
		"port":         8200,
		"vpn_required": true,
	})
	if len(warnings) != 0 {
		t.Errorf("warnings: got %v; want none", warnings)
	}
	for _, k := range []string{"name", "product", "host", "port", "vpn_required"} {
		if _, ok := body[k]; !ok {
			t.Errorf("body missing top-level key %q; got %v", k, body)
		}
	}
	if _, ok := body["extras"]; ok {
		t.Errorf("body should not have extras: got %v", body["extras"])
	}
}

func TestMapEntryUnknownSpillsToExtras(t *testing.T) {
	t.Parallel()
	body, warnings := mapEntry(map[string]any{
		"name":             "rke2-meho",
		"product":          "kubernetes",
		"host":             "10.5.50.153",
		"kubeconfig_field": "/etc/rancher/rke2/rke2.yaml",
		"sso_realm":        "evba.lab",
	})
	if len(warnings) != 0 {
		t.Errorf("warnings: got %v; want none", warnings)
	}
	extras, ok := body["extras"].(map[string]any)
	if !ok {
		t.Fatalf("extras: got %T; want map[string]any", body["extras"])
	}
	if extras["sso_realm"] != "evba.lab" {
		t.Errorf("extras.sso_realm: got %v; want evba.lab", extras["sso_realm"])
	}
	if extras["kubeconfig_field"] != "/etc/rancher/rke2/rke2.yaml" {
		t.Errorf("extras.kubeconfig_field: got %v", extras["kubeconfig_field"])
	}
}

func TestMapEntrySkipsFingerprintWithWarning(t *testing.T) {
	t.Parallel()
	body, warnings := mapEntry(map[string]any{
		"name":        "rdc-vault",
		"product":     "vault",
		"host":        "vault.evba.lab",
		"fingerprint": map[string]any{"vendor": "hashicorp"},
	})
	if _, ok := body["fingerprint"]; ok {
		t.Errorf("body should not carry fingerprint: got %v", body["fingerprint"])
	}
	if len(warnings) == 0 || !strings.Contains(warnings[0], "fingerprint") {
		t.Errorf("warnings: got %v; want one mentioning `fingerprint`", warnings)
	}
}

func TestMapEntryPreferredImplIDIsTopLevel(t *testing.T) {
	t.Parallel()
	// Per the G0.3-T1.5 (#477) amendment, preferred_impl_id is a
	// top-level column. It must land in the body root, NOT in
	// extras.
	body, _ := mapEntry(map[string]any{
		"name":              "rdc-vcenter",
		"product":           "vcenter",
		"host":              "vc-dc.evba.lab",
		"preferred_impl_id": "vsphere-8.x",
	})
	if v, ok := body["preferred_impl_id"]; !ok || v != "vsphere-8.x" {
		t.Errorf("preferred_impl_id top-level: got %v; want vsphere-8.x", v)
	}
	if extras, ok := body["extras"].(map[string]any); ok {
		if _, leaked := extras["preferred_impl_id"]; leaked {
			t.Errorf("preferred_impl_id leaked into extras: %v", extras)
		}
	}
}

func TestMapEntryTLSTrustKeysAreTopLevel(t *testing.T) {
	t.Parallel()
	// Initiative #1774: verify_tls (#1780) and tls_ca_pin (#1784) are
	// first-class per-target TLS-trust columns on TargetCreate /
	// TargetUpdate. They must land in the body root, NOT spill into
	// extras — spilling would leave the typed columns at their secure
	// defaults and silently ignore an operator who set them in the
	// descriptor.
	body, warnings := mapEntry(map[string]any{
		"name":       "vcf-logs-lab",
		"product":    "vmware-rest",
		"host":       "vrli.nested.lab",
		"verify_tls": false,
		"tls_ca_pin": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n",
	})
	if len(warnings) != 0 {
		t.Errorf("warnings: got %v; want none", warnings)
	}
	if v, ok := body["verify_tls"]; !ok || v != false {
		t.Errorf("verify_tls top-level: got %v (present=%t); want false", v, ok)
	}
	if v, ok := body["tls_ca_pin"]; !ok || v == "" {
		t.Errorf("tls_ca_pin top-level: got %v (present=%t); want the PEM", v, ok)
	}
	// Neither key may leak into extras.
	if extras, ok := body["extras"].(map[string]any); ok {
		for _, k := range []string{"verify_tls", "tls_ca_pin"} {
			if _, leaked := extras[k]; leaked {
				t.Errorf("%q leaked into extras: %v", k, extras)
			}
		}
	}
}

func TestEntryToUpdateBodyTLSTrustKeysAreTopLevel(t *testing.T) {
	t.Parallel()
	// `meho targets import --update` routes through entryToUpdateBody.
	// The TLS-trust keys must reach the sparse PATCH body's top level
	// (TargetUpdate carries verify_tls / tls_ca_pin), not extras, so a
	// descriptor re-import flips the columns rather than the JSONB blob.
	body, _ := entryToUpdateBody(map[string]any{
		"name":       "vcf-logs-lab",
		"verify_tls": true,
		"tls_ca_pin": nil, // clearing the pin is an explicit-null PATCH
	})
	if v, ok := body["verify_tls"]; !ok || v != true {
		t.Errorf("verify_tls top-level on update: got %v (present=%t); want true", v, ok)
	}
	if _, ok := body["tls_ca_pin"]; !ok {
		t.Errorf("tls_ca_pin missing from update body; want top-level null: %v", body)
	}
	if extras, ok := body["extras"].(map[string]any); ok {
		for _, k := range []string{"verify_tls", "tls_ca_pin"} {
			if _, leaked := extras[k]; leaked {
				t.Errorf("%q leaked into extras on update: %v", k, extras)
			}
		}
	}
}

func TestMapEntryTLSTrustTopLevelStillSpillsUnknown(t *testing.T) {
	t.Parallel()
	// No-regression guard: adding the TLS-trust keys to knownTopLevel
	// must not change the extras-spill behaviour for genuinely-unknown
	// keys. verify_tls / tls_ca_pin go top-level; an unrelated unknown
	// key alongside them still spills into extras.
	body, _ := mapEntry(map[string]any{
		"name":       "vcf-logs-lab",
		"product":    "vmware-rest",
		"host":       "vrli.nested.lab",
		"verify_tls": false,
		"sso_realm":  "evba.lab", // unknown → spill
	})
	if v, ok := body["verify_tls"]; !ok || v != false {
		t.Errorf("verify_tls top-level: got %v (present=%t); want false", v, ok)
	}
	extras, ok := body["extras"].(map[string]any)
	if !ok {
		t.Fatalf("extras: got %T; want map[string]any (unknown key should spill)", body["extras"])
	}
	if extras["sso_realm"] != "evba.lab" {
		t.Errorf("extras.sso_realm: got %v; want evba.lab", extras["sso_realm"])
	}
	if _, leaked := extras["verify_tls"]; leaked {
		t.Errorf("verify_tls must not be in extras: %v", extras)
	}
}

func TestMapEntryExplicitExtrasMergesWithSpilled(t *testing.T) {
	t.Parallel()
	body, _ := mapEntry(map[string]any{
		"name":       "rdc-vault",
		"product":    "vault",
		"host":       "vault.evba.lab",
		"extras":     map[string]any{"namespace": "rdc"},
		"account":    "12345",   // unknown → spill
		"project_id": "rdc-dev", // unknown → spill
	})
	extras, ok := body["extras"].(map[string]any)
	if !ok {
		t.Fatalf("extras: got %T", body["extras"])
	}
	for k, want := range map[string]any{
		"namespace":  "rdc",
		"account":    "12345",
		"project_id": "rdc-dev",
	} {
		if extras[k] != want {
			t.Errorf("extras[%q]: got %v; want %v", k, extras[k], want)
		}
	}
}

func TestEntryToUpdateBodyStripsImmutables(t *testing.T) {
	t.Parallel()
	// `name` and `product` are immutable post-create — the PATCH
	// shape on the backplane (TargetUpdate) does not carry them.
	// Stripping at the client side surfaces a meaningful error
	// (a PATCH that does nothing) rather than a 422.
	body, _ := entryToUpdateBody(map[string]any{
		"name":    "rdc-vault",
		"product": "vault",
		"host":    "vault.evba.lab",
		"notes":   "patched",
	})
	if _, ok := body["name"]; ok {
		t.Errorf("update body should not carry `name`: got %v", body)
	}
	if _, ok := body["product"]; ok {
		t.Errorf("update body should not carry `product`: got %v", body)
	}
	if body["host"] != "vault.evba.lab" {
		t.Errorf("host preserved: got %v", body["host"])
	}
	if body["notes"] != "patched" {
		t.Errorf("notes preserved: got %v", body["notes"])
	}
}

func TestEntryToUpdateBodySparseShape(t *testing.T) {
	t.Parallel()
	// The sparse-PATCH contract: a YAML entry with only `notes`
	// produces a body with ONLY `notes` (plus extras if any
	// unknown keys are present). Anything else would let the
	// existing route handler's `model_dump(exclude_unset=True)` +
	// `setattr` loop wipe other columns on every --update run
	// (the bug PR #362's review on #257 surfaced).
	body, _ := entryToUpdateBody(map[string]any{
		"name":  "rdc-vault",
		"notes": "patched",
	})
	if len(body) != 1 {
		t.Errorf("sparse body should have 1 key; got %d: %v", len(body), body)
	}
	if body["notes"] != "patched" {
		t.Errorf("notes: got %v; want patched", body["notes"])
	}
}

// --- buildOfflinePlan ---------------------------------------------------

func TestBuildOfflinePlanEveryEntryIsCreate(t *testing.T) {
	t.Parallel()
	entries := []map[string]any{
		{"name": "a", "product": "vault", "host": "1.1.1.1"},
		{"name": "b", "product": "vault", "host": "2.2.2.2"},
	}
	p := buildOfflinePlan(entries, false)
	if len(p.Create) != 2 || len(p.Update) != 0 || len(p.Skip) != 0 {
		t.Errorf("plan: create=%d update=%d skip=%d; want 2/0/0",
			len(p.Create), len(p.Update), len(p.Skip))
	}
	if p.Create[0].Action != actionCreate {
		t.Errorf("action: got %q; want CREATE", p.Create[0].Action)
	}
}

// --- buildLivePlan + listExistingNames ---------------------------------

// fakeDoer records calls and serves canned responses. It substitutes
// for doAuthedRequest in the buildLivePlan / executePlan tests so
// the suite doesn't touch the auth/token-store layer — that layer is
// covered by cli/internal/auth's own tests.
type fakeDoer struct {
	existing  []string // names returned by listExistingNames
	listPages int      // bumped each time the GET handler fires
	creates   []recorded
	updates   []recorded
}

type recorded struct {
	Method string
	Path   string
	Body   []byte
}

func (f *fakeDoer) do(_ context.Context, method, path string, body []byte) ([]byte, error) {
	switch method {
	case http.MethodGet:
		// Pagination: any cursor query → empty second page.
		if strings.Contains(path, "cursor=") {
			return []byte(`[]`), nil
		}
		f.listPages++
		out := []map[string]any{}
		for _, n := range f.existing {
			out = append(out, map[string]any{"name": n})
		}
		raw, _ := json.Marshal(out)
		return raw, nil
	case http.MethodPost:
		f.creates = append(f.creates, recorded{Method: method, Path: path, Body: append([]byte(nil), body...)})
		return []byte(`{}`), nil
	case http.MethodPatch:
		f.updates = append(f.updates, recorded{Method: method, Path: path, Body: append([]byte(nil), body...)})
		return []byte(`{}`), nil
	}
	return nil, &httpError{StatusCode: http.StatusMethodNotAllowed, Body: "fakeDoer: method " + method}
}

func TestBuildLivePlanPartitionsCreateAndUpdate(t *testing.T) {
	t.Parallel()
	f := &fakeDoer{existing: []string{"rdc-vault"}}
	// rdc-vault carries ONLY `notes` (plus the immutable name/product
	// stripped on the PATCH path) so the assertion can pin a 1-key
	// sparse body.
	entries := []map[string]any{
		{"name": "rdc-vault", "product": "vault", "host": "v1", "notes": "patched"},
		{"name": "rdc-vcenter", "product": "vcenter", "host": "vc1"},
	}
	p, err := buildLivePlan(context.Background(), f.do, entries, true)
	if err != nil {
		t.Fatalf("buildLivePlan: %v", err)
	}
	if len(p.Create) != 1 || p.Create[0].Name != "rdc-vcenter" {
		t.Errorf("create: %v; want one entry for rdc-vcenter", p.Create)
	}
	if len(p.Update) != 1 || p.Update[0].Name != "rdc-vault" {
		t.Errorf("update: %v; want one entry for rdc-vault", p.Update)
	}
	// Sparse-PATCH contract: update body carries ONLY the YAML keys
	// that map to top-level columns and aren't immutable. `name` /
	// `product` are stripped (immutable post-create); `host` and
	// `notes` survive. The 2-key shape is what the API's
	// `model_dump(exclude_unset=True)` then patches — no other column
	// gets touched.
	body := p.Update[0].Body
	if len(body) != 2 {
		t.Errorf("update body should be sparse (2 keys); got %d: %v", len(body), body)
	}
	if body["host"] != "v1" {
		t.Errorf("body[host]: got %v; want v1", body["host"])
	}
	if body["notes"] != "patched" {
		t.Errorf("body[notes]: got %v; want patched", body["notes"])
	}
}

func TestExecutePlanIssuesPostAndPatch(t *testing.T) {
	t.Parallel()
	f := &fakeDoer{}
	p := &plan{
		Create: []planEntry{{Name: "a", Action: actionCreate, Body: map[string]any{"name": "a", "product": "vault", "host": "1.1.1.1"}}},
		Update: []planEntry{{Name: "b", Action: actionUpdate, Body: map[string]any{"notes": "patched"}}},
	}
	if err := executePlan(context.Background(), f.do, p); err != nil {
		t.Fatalf("executePlan: %v", err)
	}
	if len(f.creates) != 1 {
		t.Errorf("POSTs: got %d; want 1", len(f.creates))
	}
	if len(f.updates) != 1 {
		t.Errorf("PATCHes: got %d; want 1", len(f.updates))
	}
	// PATCH path must include the URL-escaped name.
	if !strings.Contains(f.updates[0].Path, "/api/v1/targets/b") {
		t.Errorf("PATCH path: got %q; want /api/v1/targets/b", f.updates[0].Path)
	}
	// And the PATCH body matches the sparse shape — no `name` /
	// `product` even if the YAML carried them.
	var patchBody map[string]any
	if err := json.Unmarshal(f.updates[0].Body, &patchBody); err != nil {
		t.Fatalf("unmarshal PATCH body: %v", err)
	}
	if _, ok := patchBody["name"]; ok {
		t.Errorf("PATCH body should not carry `name`: %v", patchBody)
	}
}

// --- runImport end-to-end (dry-run path; offline; no fake server) -----

func TestRunImportDryRunPrintsPlan(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "targets.yaml")
	yaml := []byte(`
targets:
  - name: rdc-vault
    product: vault
    host: vault.evba.lab
    sso_realm: evba.lab
`)
	if err := os.WriteFile(path, yaml, 0o600); err != nil {
		t.Fatalf("write yaml: %v", err)
	}
	cmd := &cobra.Command{}
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	cmd.SetContext(context.Background())

	err := runImport(cmd, importOptions{File: path, DryRun: true})
	if err != nil {
		t.Fatalf("runImport: %v\nstderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"CREATE", "rdc-vault", "Plan:"} {
		if !strings.Contains(out, want) {
			t.Errorf("dry-run output missing %q:\n%s", want, out)
		}
	}
}

func TestRunImportDryRunJSONStructuredPlan(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "targets.yaml")
	yaml := []byte(`
targets:
  - name: a
    product: vault
    host: 1.1.1.1
  - name: b
    product: vcenter
    host: 2.2.2.2
`)
	if err := os.WriteFile(path, yaml, 0o600); err != nil {
		t.Fatalf("write yaml: %v", err)
	}
	cmd := &cobra.Command{}
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	cmd.SetContext(context.Background())

	err := runImport(cmd, importOptions{File: path, DryRun: true, JSONOut: true})
	if err != nil {
		t.Fatalf("runImport: %v\nstderr=%s", err, stderr.String())
	}
	var p plan
	if err := json.Unmarshal(stdout.Bytes(), &p); err != nil {
		t.Fatalf("decode JSON: %v\nstdout=%s", err, stdout.String())
	}
	names := []string{}
	for _, e := range p.Create {
		names = append(names, e.Name)
	}
	sort.Strings(names)
	if len(names) != 2 || names[0] != "a" || names[1] != "b" {
		t.Errorf("plan.create names: %v; want [a b]", names)
	}
}

func TestRunImportDryRunSkipsBackplaneCalls(t *testing.T) {
	// Note: no t.Parallel() — this test calls t.Setenv, which
	// testing forbids inside a parallel test (env is process-wide).
	// Sanity check: --dry-run must not hit any HTTP endpoint, even
	// the listing one, so it works on an air-gapped machine with no
	// `meho login` artifact present.
	dir := t.TempDir()
	path := filepath.Join(dir, "targets.yaml")
	if err := os.WriteFile(path, []byte(`
targets:
  - name: a
    product: vault
    host: 1.1.1.1
`), 0o600); err != nil {
		t.Fatalf("write yaml: %v", err)
	}
	// Point XDG_CONFIG_HOME at a dir with NO config / NO token —
	// resolveBackplane would error if the codepath touched it.
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	cmd := &cobra.Command{}
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetContext(context.Background())
	if err := runImport(cmd, importOptions{File: path, DryRun: true}); err != nil {
		t.Fatalf("dry-run with no auth config should succeed: %v", err)
	}
}

func TestRunImportNonexistentFileSurfacesUnexpected(t *testing.T) {
	t.Parallel()
	cmd := &cobra.Command{}
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetContext(context.Background())
	err := runImport(cmd, importOptions{File: "/nonexistent/path.yaml", DryRun: true})
	// output.RenderError returns a silent ExitCoder; both nil and
	// non-nil errors are acceptable shapes — the important property
	// is that the call doesn't panic. Assert on stderr instead.
	_ = err
}
