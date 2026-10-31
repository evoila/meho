// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

const (
	tenantAID = "aaaaaaaa-0000-4000-8000-000000000001"
	tenantBID = "bbbbbbbb-0000-4000-8000-000000000002"
)

// tenantBackplane is a fake backplane for a person in tenants lab-a
// (read_only) and lab-b (operator), with the rules of #5239: the
// status endpoint answers without a tenant and lists both; a read
// without the Meho-Tenant header is refused with 400 tenant_not_chosen;
// a tenant outside the list gets 403 tenant_not_member. With single
// set, the person is in lab-a only and no header is needed.
//
// As on the real backplane, the status endpoint with a current tenant
// needs the operator role there (403 insufficient_role for lab-a), and
// the liveness endpoint /api/v1/health/live answers every role but
// needs a chosen tenant.
type tenantBackplane struct {
	single bool

	mu      sync.Mutex
	tenants []string // Meho-Tenant header of every request
	seen    []string // path of every request
	bearers map[string]bool
}

type fakeTenant struct{ id, name, role string }

var fakeTenants = []fakeTenant{
	{tenantAID, "lab-a", "read_only"},
	{tenantBID, "lab-b", "operator"},
}

func (b *tenantBackplane) mine() []fakeTenant {
	if b.single {
		return fakeTenants[:1]
	}
	return fakeTenants
}

// pick returns the tenant the header names, whether the request may
// go on, and the refusal otherwise.
func (b *tenantBackplane) pick(header string) (*fakeTenant, int, string) {
	mine := b.mine()
	if header == "" {
		if len(mine) == 1 {
			return &mine[0], http.StatusOK, ""
		}
		return nil, http.StatusBadRequest, "tenant_not_chosen: this login is in 2 tenants (lab-a, lab-b). " +
			"Choose one with `meho tenants use <name>`, or send the Meho-Tenant header with a tenant name or ID."
	}
	for i := range mine {
		if header == mine[i].id || header == mine[i].name {
			return &mine[i], http.StatusOK, ""
		}
	}
	return nil, http.StatusForbidden, "tenant_not_member: this login is not in the tenant named by the " +
		"Meho-Tenant header. Your tenants: lab-a, lab-b."
}

func (b *tenantBackplane) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	header := r.Header.Get(api.TenantHeader)
	b.mu.Lock()
	b.tenants = append(b.tenants, header)
	b.seen = append(b.seen, r.URL.Path)
	b.bearers[r.Header.Get("Authorization")] = true
	b.mu.Unlock()

	w.Header().Set("Content-Type", "application/json")
	current, status, detail := b.pick(header)
	if r.URL.Path == "/api/v1/health" && status == http.StatusBadRequest {
		current, status = nil, http.StatusOK // the status answer needs no tenant
	}
	if r.URL.Path == "/api/v1/health" && current != nil && current.role == "read_only" {
		status, detail = http.StatusForbidden, "insufficient_role"
	}
	if status != http.StatusOK {
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": detail})
		return
	}
	switch r.URL.Path {
	case "/api/v1/health":
		_ = json.NewEncoder(w).Encode(b.health(current))
	case "/api/v1/health/live":
		_ = json.NewEncoder(w).Encode(map[string]any{
			"operator": b.operator(current), "db": map[string]any{"migrated": true},
		})
	case "/api/v1/targets":
		_ = json.NewEncoder(w).Encode(map[string]any{
			"items": []map[string]any{{
				"name": current.name + "-vcenter", "product": "vcenter", "host": "vc.example.test",
				"aliases": []string{}, "tenant_id": current.id,
			}},
			"next_cursor": nil,
		})
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func (b *tenantBackplane) operator(current *fakeTenant) map[string]any {
	entries := []map[string]any{}
	for _, t := range b.mine() {
		entries = append(entries, map[string]any{
			"tenant_id": t.id, "name": t.name, "tenant_role": t.role,
			"current": current != nil && current.id == t.id,
		})
	}
	op := map[string]any{"sub": "alice-sub", "tenants": entries, "tenant_id": nil, "tenant_role": nil}
	if current != nil {
		op["tenant_id"], op["tenant_role"] = current.id, current.role
	}
	return op
}

func (b *tenantBackplane) health(current *fakeTenant) map[string]any {
	vault := map[string]any{"reachable": false, "read_ok": false, "detail": "tenant_not_chosen"}
	if current != nil {
		vault = map[string]any{"reachable": true, "read_ok": true}
	}
	return map[string]any{"operator": b.operator(current), "vault": vault, "db": map[string]any{"migrated": true}}
}

// livenessCall returns the path of the request before the last
// liveness call and the Meho-Tenant header of that liveness call.
func (b *tenantBackplane) livenessCall(t *testing.T) (before, tenant string) {
	t.Helper()
	b.mu.Lock()
	defer b.mu.Unlock()
	for i := len(b.seen) - 1; i > 0; i-- {
		if b.seen[i] == "/api/v1/health/live" {
			return b.seen[i-1], b.tenants[i]
		}
	}
	t.Fatal("backplane saw no liveness call")
	return "", ""
}

func (b *tenantBackplane) lastTenant(t *testing.T) string {
	t.Helper()
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.tenants) == 0 {
		t.Fatal("backplane saw no request")
	}
	return b.tenants[len(b.tenants)-1]
}

// newTenantBackplane starts the fake backplane and logs the test
// person in to it (one stored token, one config file).
func newTenantBackplane(t *testing.T, single bool) (*tenantBackplane, string, string) {
	t.Helper()
	bp := &tenantBackplane{single: single, bearers: map[string]bool{}}
	srv := httptest.NewServer(bp)
	t.Cleanup(srv.Close)
	xdg := withTempXDG(t)
	t.Setenv(api.TenantEnv, "")
	seedCreds(t, xdg, srv.URL, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(time.Hour),
	})
	return bp, srv.URL, filepath.Join(xdg, "meho", "config.json")
}

// runMeho runs the real root command and returns stdout and stderr.
func runMeho(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	restore := setDynamicRegistrar(func(*cobra.Command) {})
	t.Cleanup(restore)
	root := newRootCmdFor(args)
	var out, errOut bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetArgs(args)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := root.ExecuteContext(ctx)
	return out.String(), errOut.String(), err
}

// TestTenantsUse_SwitchWithoutNewLogin is the main path: a person in
// two tenants reads in lab-a, switches with `meho tenants use lab-b`,
// reads again and gets lab-b's results. The stored login never
// changes: one bearer for every request.
func TestTenantsUse_SwitchWithoutNewLogin(t *testing.T) {
	bp, _, cfgPath := newTenantBackplane(t, false)

	_, stderr, err := runMeho(t, "targets", "list")
	if err == nil || !strings.Contains(stderr, "You are in several tenants. Pick one with `meho tenants use <name>`.") {
		t.Fatalf("read without a tenant: err=%v stderr=%q", err, stderr)
	}

	if out, stderr, err := runMeho(t, "tenants", "use", "lab-a"); err != nil || out != "Now working in tenant lab-a (read_only).\n" {
		t.Fatalf("use lab-a: err=%v out=%q stderr=%q", err, out, stderr)
	}
	out, stderr, err := runMeho(t, "targets", "list")
	if err != nil || !strings.Contains(out, "lab-a-vcenter") {
		t.Fatalf("read in lab-a: err=%v out=%q stderr=%q", err, out, stderr)
	}
	if got := bp.lastTenant(t); got != tenantAID {
		t.Errorf("Meho-Tenant = %q; want the ID of lab-a", got)
	}

	if out, stderr, err := runMeho(t, "tenants", "use", "lab-b"); err != nil || out != "Now working in tenant lab-b (operator).\n" {
		t.Fatalf("use lab-b: err=%v out=%q stderr=%q", err, out, stderr)
	}
	out, _, err = runMeho(t, "targets", "list")
	if err != nil || !strings.Contains(out, "lab-b-vcenter") || strings.Contains(out, "lab-a-vcenter") {
		t.Fatalf("read in lab-b: err=%v out=%q", err, out)
	}
	if got := bp.lastTenant(t); got != tenantBID {
		t.Errorf("Meho-Tenant = %q; want the ID of lab-b", got)
	}

	if len(bp.bearers) != 1 || !bp.bearers["Bearer eyJ.test.token"] {
		t.Errorf("bearers seen = %v; want only the one stored login", bp.bearers)
	}
	cfg, err := auth.LoadConfigAt(cfgPath)
	if err != nil || cfg.Tenant == nil || *cfg.Tenant != (auth.Tenant{ID: tenantBID, Name: "lab-b"}) {
		t.Errorf("stored config = %+v (%v); want tenant lab-b", cfg, err)
	}

	out, _, err = runMeho(t, "status")
	if err != nil || !strings.Contains(out, "    lab-b  operator  (current)\n") || !strings.Contains(out, "    lab-a  read_only\n") {
		t.Errorf("status must show the current tenant and the other one: err=%v out=%q", err, out)
	}
}

// TestTenantsUse_UnknownNameIsRefused: a name that is not one of the
// person's tenants is refused, nothing is stored, and the tenants are
// listed.
func TestTenantsUse_UnknownNameIsRefused(t *testing.T) {
	_, _, cfgPath := newTenantBackplane(t, false)

	out, stderr, err := runMeho(t, "tenants", "use", "lab-c")
	if err == nil || out != "" {
		t.Fatalf("want a refusal; err=%v out=%q", err, out)
	}
	for _, want := range []string{`not in a tenant named "lab-c"`, "Your tenants: lab-a (read_only), lab-b (operator)."} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr missing %q: %q", want, stderr)
		}
	}
	if cfg, _ := auth.LoadConfigAt(cfgPath); cfg.Tenant != nil {
		t.Errorf("a refused name must not be stored: %+v", cfg.Tenant)
	}
}

// TestTenantsUse_ListMarksCurrent: without a name, the tenants are
// listed with the role in each, and the current one is marked. The
// list is read without the header, so a stored tenant does not hide it.
func TestTenantsUse_ListMarksCurrent(t *testing.T) {
	bp, _, _ := newTenantBackplane(t, false)

	out, _, err := runMeho(t, "tenants", "use")
	if err != nil || out != "Your tenants:\n  lab-a  read_only\n  lab-b  operator\n"+
		"No tenant chosen. Pick one with `meho tenants use <name>`.\n" {
		t.Fatalf("list without a choice: err=%v out=%q", err, out)
	}

	if _, _, err := runMeho(t, "tenants", "use", tenantBID); err != nil {
		t.Fatalf("use by ID: %v", err)
	}
	out, _, err = runMeho(t, "tenants", "use")
	if err != nil || out != "Your tenants:\n  lab-a  read_only\n  lab-b  operator  (current)\n" {
		t.Fatalf("list after the choice: err=%v out=%q", err, out)
	}
	if got := bp.lastTenant(t); got != "" {
		t.Errorf("the list must be read without the header; got %q", got)
	}

	out, _, err = runMeho(t, "tenants", "use", "--json")
	var entries []api.TenantEntry
	if err != nil || json.Unmarshal([]byte(out), &entries) != nil || len(entries) != 2 || !entries[1].Current {
		t.Errorf("--json list: err=%v out=%q", err, out)
	}
}

// TestTenantEnvWinsOverStoredChoice: MEHO_TENANT overrides the stored
// choice for one shell.
func TestTenantEnvWinsOverStoredChoice(t *testing.T) {
	bp, _, _ := newTenantBackplane(t, false)
	if _, _, err := runMeho(t, "tenants", "use", "lab-a"); err != nil {
		t.Fatalf("use lab-a: %v", err)
	}

	t.Setenv(api.TenantEnv, "lab-b")
	out, _, err := runMeho(t, "targets", "list")
	if err != nil || !strings.Contains(out, "lab-b-vcenter") {
		t.Fatalf("read with MEHO_TENANT=lab-b: err=%v out=%q", err, out)
	}
	if got := bp.lastTenant(t); got != "lab-b" {
		t.Errorf("Meho-Tenant = %q; want the MEHO_TENANT value", got)
	}
	out, _, _ = runMeho(t, "tenants", "use")
	if !strings.Contains(out, "  lab-b  operator  (current)\n") {
		t.Errorf("list must mark the MEHO_TENANT tenant as current: %q", out)
	}
	_, stderr, err := runMeho(t, "tenants", "use", "lab-a")
	if err != nil || !strings.Contains(stderr, "MEHO_TENANT is set") {
		t.Errorf("use with MEHO_TENANT set must warn: err=%v stderr=%q", err, stderr)
	}
}

// TestTenantHeader_OtherBackplaneGetsNoStoredTenant: the stored choice
// belongs to the backplane it was made on. A --backplane flag that
// names another backplane sends no stored tenant.
func TestTenantHeader_OtherBackplaneGetsNoStoredTenant(t *testing.T) {
	bp, _, _ := newTenantBackplane(t, true)
	if _, _, err := runMeho(t, "tenants", "use", "lab-a"); err != nil {
		t.Fatalf("use lab-a: %v", err)
	}
	other := &tenantBackplane{single: true, bearers: map[string]bool{}}
	otherSrv := httptest.NewServer(other)
	t.Cleanup(otherSrv.Close)
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(otherSrv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: otherSrv.URL, AccessToken: "eyJ.other.token", Expiry: time.Now().Add(time.Hour),
	})

	if _, _, err := runMeho(t, "targets", "list", "--backplane", otherSrv.URL); err != nil {
		t.Fatalf("read on the other backplane: %v", err)
	}
	if got := other.lastTenant(t); got != "" {
		t.Errorf("other backplane got Meho-Tenant %q; want none", got)
	}
	if _, _, err := runMeho(t, "targets", "list"); err != nil {
		t.Fatalf("read on the stored backplane: %v", err)
	}
	if got := bp.lastTenant(t); got != tenantAID {
		t.Errorf("stored backplane got Meho-Tenant %q; want the stored ID", got)
	}
}

// TestTenantHeader_OneTenantSeesNoChange: a person in one tenant sends
// no header, needs no `meho tenants use`, and gets a plain answer when
// they run it.
func TestTenantHeader_OneTenantSeesNoChange(t *testing.T) {
	bp, _, cfgPath := newTenantBackplane(t, true)

	out, _, err := runMeho(t, "targets", "list")
	if err != nil || !strings.Contains(out, "lab-a-vcenter") {
		t.Fatalf("read: err=%v out=%q", err, out)
	}
	if got := bp.lastTenant(t); got != "" {
		t.Errorf("Meho-Tenant = %q; want none", got)
	}
	out, _, err = runMeho(t, "tenants", "use")
	if err != nil || out != "Your tenants:\n  lab-a  read_only\n" {
		t.Errorf("list with one tenant: err=%v out=%q", err, out)
	}
	if cfg, _ := auth.LoadConfigAt(cfgPath); cfg.Tenant != nil {
		t.Errorf("nothing must be stored: %+v", cfg.Tenant)
	}
}

// TestStatus_StaleTenantSaysHowToPickAnother: a stored tenant the
// person has left gets the backplane's reason and how to pick another.
func TestStatus_StaleTenantSaysHowToPickAnother(t *testing.T) {
	_, url, cfgPath := newTenantBackplane(t, false)
	if err := auth.SaveConfigAt(cfgPath, auth.Config{
		BackplaneURL: url,
		Tenant:       &auth.Tenant{ID: "cccccccc-0000-4000-8000-000000000003", Name: "lab-c"},
	}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	_, stderr, err := runMeho(t, "status")
	if err == nil || !strings.Contains(stderr, "tenant_not_member:") || !strings.Contains(stderr, "`meho tenants use`") {
		t.Errorf("status with a stale tenant: err=%v stderr=%q", err, stderr)
	}
	// The way out: the list and the switch send no stored tenant.
	if out, stderr, err := runMeho(t, "tenants", "use", "lab-a"); err != nil || out != "Now working in tenant lab-a (read_only).\n" {
		t.Errorf("switch away from the stale tenant: err=%v out=%q stderr=%q", err, out, stderr)
	}
}

// TestStatus_ReaderTenantShowsCurrentTenant: after a switch to a
// tenant where the person only reads, the deep check is refused with
// 403 insufficient_role. status then shows the liveness answer: who
// the person is, the current tenant, the database state, and that the
// other checks need the operator role.
func TestStatus_ReaderTenantShowsCurrentTenant(t *testing.T) {
	bp, _, _ := newTenantBackplane(t, false)
	if _, _, err := runMeho(t, "tenants", "use", "lab-a"); err != nil {
		t.Fatalf("use lab-a: %v", err)
	}

	out, stderr, err := runMeho(t, "status")
	if err != nil {
		t.Fatalf("status in a read_only tenant: err=%v stderr=%q", err, stderr)
	}
	for _, want := range []string{
		"Logged in as alice-sub\n",
		"    lab-a  read_only  (current)\n",
		"    lab-b  operator\n",
		"  DB:    migrated\n",
		"need the operator role in this tenant",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("status output missing %q: %q", want, out)
		}
	}
	if strings.Contains(out, "Vault:") {
		t.Errorf("the liveness answer carries no credential-store line: %q", out)
	}
	before, tenant := bp.livenessCall(t)
	if before != "/api/v1/health" {
		t.Errorf("status must try the deep check first, then the liveness route; the call before was %q", before)
	}
	if tenant != tenantAID {
		t.Errorf("Meho-Tenant on the liveness call = %q; want the ID of lab-a", tenant)
	}

	out, _, err = runMeho(t, "status", "--json")
	var live api.LivenessResponse
	if err != nil || json.Unmarshal([]byte(out), &live) != nil || live.Operator.TenantId == nil ||
		live.Operator.TenantId.String() != tenantAID || strings.Contains(out, "\"vault\"") {
		t.Errorf("status --json in a read_only tenant: err=%v out=%q", err, out)
	}
}

// TestTenantsUse_ReaderInOneTenant: a person with the read_only role
// in their only tenant is always in that tenant, so the status
// endpoint refuses them with 403. They can still list and choose the
// tenant (from the liveness route), and status shows it. This also
// gets them out of a stored tenant they have left.
func TestTenantsUse_ReaderInOneTenant(t *testing.T) {
	bp, url, cfgPath := newTenantBackplane(t, true)
	if err := auth.SaveConfigAt(cfgPath, auth.Config{
		BackplaneURL: url,
		Tenant:       &auth.Tenant{ID: "cccccccc-0000-4000-8000-000000000003", Name: "lab-c"},
	}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}

	out, stderr, err := runMeho(t, "tenants", "use")
	if err != nil || out != "Your tenants:\n  lab-a  read_only\n" {
		t.Fatalf("list as a reader in one tenant: err=%v out=%q stderr=%q", err, out, stderr)
	}
	if got := bp.lastTenant(t); got != "" {
		t.Errorf("the list must be read without the header; got %q", got)
	}
	if out, stderr, err := runMeho(t, "tenants", "use", "lab-a"); err != nil || out != "Now working in tenant lab-a (read_only).\n" {
		t.Fatalf("choose as a reader in one tenant: err=%v out=%q stderr=%q", err, out, stderr)
	}
	if cfg, _ := auth.LoadConfigAt(cfgPath); cfg.Tenant == nil || cfg.Tenant.Name != "lab-a" {
		t.Errorf("stored tenant = %+v; want lab-a", cfg.Tenant)
	}
	out, stderr, err = runMeho(t, "status")
	if err != nil || !strings.Contains(out, "    lab-a  read_only  (current)\n") {
		t.Errorf("status as a reader in one tenant: err=%v out=%q stderr=%q", err, out, stderr)
	}
}

// TestConfigAfterLogin_KeepsTenantOfSameBackplane: a new login to the
// same backplane keeps the choice; a login to another one drops it.
func TestConfigAfterLogin_KeepsTenantOfSameBackplane(t *testing.T) {
	xdg := withTempXDG(t)
	chosen := &auth.Tenant{ID: tenantBID, Name: "lab-b"}
	if err := auth.SaveConfigAt(filepath.Join(xdg, "meho", "config.json"),
		auth.Config{BackplaneURL: "https://meho.example.test", Tenant: chosen}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	if got := configAfterLogin("https://meho.example.test"); got.Tenant == nil || *got.Tenant != *chosen {
		t.Errorf("same backplane: tenant = %+v; want lab-b", got.Tenant)
	}
	if got := configAfterLogin("https://other.example.test"); got.Tenant != nil || got.BackplaneURL != "https://other.example.test" {
		t.Errorf("other backplane: config = %+v; want no tenant", got)
	}
}
