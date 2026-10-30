// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"slices"
	"strings"
	"testing"

	"github.com/google/uuid"
)

const labBID = "5d0f7a3e-1c2b-4e8d-9f6a-7b3c2d1e0f9a"

var extraRoles = []string{"Readers", "Operators", "SA-Operators", "Admins", "Approvers"}

func groupWrites(fake *fakeKeycloak) int {
	return fake.calls["POST /groups"] + fake.calls["PUT /groups/{uuid}"]
}

// --add-tenant makes the five groups of the extra tenant. They carry only
// meho_tenant, so an older MEHO gives a person who is only in them no
// tenant. A second run with the same flags changes nothing.
func TestBootstrap_AddTenantMakesFiveGroupsWithOnlyMehoTenant(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	opts.AddTenantName = "lab-b"
	opts.AddTenantID = labBID
	res, _, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if res.AddedTenantID != labBID || res.AddedTenantIDNew {
		t.Errorf("AddedTenantID = %q new=%v, want %q new=false",
			res.AddedTenantID, res.AddedTenantIDNew, labBID)
	}
	for _, role := range extraRoles {
		g := fake.groupByName("MEHO-lab-b-" + role)
		if g == nil {
			t.Errorf("group MEHO-lab-b-%s not created", role)
			continue
		}
		want := map[string][]string{"meho_tenant": {labBID + ":lab-b:" + role}}
		if len(g.Attributes) != 1 || !slices.Equal(g.Attributes["meho_tenant"], want["meho_tenant"]) {
			t.Errorf("group %s attributes = %v, want %v", g.Name, g.Attributes, want)
		}
	}
	// 5 + platform + 5 extra.
	if len(fake.groups) != 11 {
		t.Errorf("expected 11 groups, got %d", len(fake.groups))
	}

	before := groupWrites(fake)
	res, _, err = runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if after := groupWrites(fake); after != before {
		t.Errorf("second run wrote groups: %d -> %d", before, after)
	}
	for _, line := range res.MapperEventsLog {
		if strings.Contains(line, "group") && !strings.HasPrefix(line, "[skip]") {
			t.Errorf("second run event %q, want [skip]", line)
		}
	}
}

// Without --add-tenant-id the first run makes a new UUID, and a later run
// reads it back from the groups, so it changes nothing.
func TestBootstrap_AddTenantWithoutIDMakesOneAndReusesIt(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	opts.AddTenantName = "lab-b"
	res, _, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if !res.AddedTenantIDNew {
		t.Errorf("AddedTenantIDNew = false, want true on the first run")
	}
	if _, perr := uuid.Parse(res.AddedTenantID); perr != nil {
		t.Fatalf("AddedTenantID %q is not a UUID", res.AddedTenantID)
	}
	first := res.AddedTenantID
	got := fake.groupByName("MEHO-lab-b-Admins").Attributes["meho_tenant"]
	if !slices.Equal(got, []string{first + ":lab-b:Admins"}) {
		t.Errorf("MEHO-lab-b-Admins meho_tenant = %v", got)
	}

	before := groupWrites(fake)
	res, _, err = runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if res.AddedTenantID != first || res.AddedTenantIDNew {
		t.Errorf("second run id = %q new=%v, want %q new=false",
			res.AddedTenantID, res.AddedTenantIDNew, first)
	}
	if after := groupWrites(fake); after != before {
		t.Errorf("second run wrote groups: %d -> %d", before, after)
	}
}

// --add-tenant-id in the urn:uuid form (or in upper case) is stored in
// the standard form, so the meho_tenant value keeps its three parts and
// a later run without --add-tenant-id reads the same id back.
func TestBootstrap_AddTenantIDInUrnFormIsStoredInStandardForm(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	opts.AddTenantName = "lab-b"
	opts.AddTenantID = "urn:uuid:" + strings.ToUpper(labBID)
	res, _, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if res.AddedTenantID != labBID || res.AddedTenantIDNew {
		t.Errorf("AddedTenantID = %q new=%v, want %q new=false",
			res.AddedTenantID, res.AddedTenantIDNew, labBID)
	}
	got := fake.groupByName("MEHO-lab-b-Admins").Attributes["meho_tenant"]
	if !slices.Equal(got, []string{labBID + ":lab-b:Admins"}) {
		t.Errorf("MEHO-lab-b-Admins meho_tenant = %v", got)
	}

	before := groupWrites(fake)
	opts.AddTenantID = ""
	res, _, err = runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if res.AddedTenantID != labBID || res.AddedTenantIDNew {
		t.Errorf("second run id = %q new=%v, want %q new=false",
			res.AddedTenantID, res.AddedTenantIDNew, labBID)
	}
	if after := groupWrites(fake); after != before {
		t.Errorf("second run wrote groups: %d -> %d", before, after)
	}
}

// --tenant-id in the urn:uuid form (or in upper case) keeps that text in
// tenant_id, as the tenant_id claim must stay as it was, but meho_tenant
// takes the standard UUID form, so its three parts hold.
func TestBootstrap_FirstTenantIDInUrnFormGivesStandardMehoTenant(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	std := strings.ToLower(opts.TenantID)
	opts.TenantID = "urn:uuid:" + strings.ToUpper(std)
	if _, _, err := runBootstrapOnce(t, fake, opts); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	for _, g := range roleGroups {
		got := fake.groupByName(g.Name)
		if got == nil {
			t.Errorf("group %s not created", g.Name)
			continue
		}
		if !slices.Equal(got.Attributes["tenant_id"], []string{opts.TenantID}) {
			t.Errorf("%s tenant_id = %v, want %q", g.Name, got.Attributes["tenant_id"], opts.TenantID)
		}
		if !slices.Equal(got.Attributes["meho_tenant"], []string{std + ":main:" + g.Role}) {
			t.Errorf("%s meho_tenant = %v, want %q", g.Name, got.Attributes["meho_tenant"], std+":main:"+g.Role)
		}
	}
}

// A realm made by the older command (groups with tenant_id and
// tenant_role only, no platform group) gets meho_tenant on the five
// groups and the platform group. The values it had stay; other
// attributes stay too.
func TestBootstrap_OneTenantRealmGetsMehoTenant(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	for _, g := range roleGroups {
		attrs := map[string][]string{
			"tenant_id":   {opts.TenantID},
			"tenant_role": {g.TenantRole},
			"note":        {"kept"},
		}
		if g.Approver {
			attrs["approver"] = []string{"true"}
		}
		id := "uuid-old-" + g.Role
		fake.groups[id] = &groupRep{ID: id, Name: g.Name, Attributes: attrs}
	}
	if _, _, err := runBootstrapOnce(t, fake, opts); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	for _, g := range roleGroups {
		got := fake.groups["uuid-old-"+g.Role].Attributes
		if !slices.Equal(got["meho_tenant"], []string{opts.TenantID + ":main:" + g.Role}) {
			t.Errorf("%s meho_tenant = %v", g.Name, got["meho_tenant"])
		}
		if !slices.Equal(got["tenant_id"], []string{opts.TenantID}) ||
			!slices.Equal(got["tenant_role"], []string{g.TenantRole}) ||
			!slices.Equal(got["note"], []string{"kept"}) {
			t.Errorf("%s lost a value: %v", g.Name, got)
		}
	}
	if fake.calls["PUT /groups/{uuid}"] != 5 {
		t.Errorf("PUT /groups/{uuid} = %d, want 5", fake.calls["PUT /groups/{uuid}"])
	}
	if fake.groupByName(platformAdminGroupName) == nil {
		t.Errorf("%s not created", platformAdminGroupName)
	}
}

// An extra tenant's group with a tenant_id set by hand loses it, and a
// changed meho_tenant is set back. The platform group keeps attributes
// the command does not own.
func TestBootstrap_FixesExtraTenantAndPlatformGroups(t *testing.T) {
	fake := newFakeKeycloak()
	fake.groups["uuid-x"] = &groupRep{
		ID: "uuid-x", Name: "MEHO-lab-b-Operators",
		Attributes: map[string][]string{
			"tenant_id":   {fullOpts().TenantID},
			"meho_tenant": {labBID + ":lab-b:Admins"},
		},
	}
	fake.groups["uuid-p"] = &groupRep{
		ID: "uuid-p", Name: platformAdminGroupName,
		Attributes: map[string][]string{"platform_admin": {"false"}, "note": {"kept"}},
	}
	opts := fullOpts()
	opts.AddTenantName = "lab-b"
	opts.AddTenantID = labBID
	if _, _, err := runBootstrapOnce(t, fake, opts); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	ops := fake.groups["uuid-x"].Attributes
	if _, ok := ops["tenant_id"]; ok {
		t.Errorf("MEHO-lab-b-Operators still has tenant_id: %v", ops)
	}
	if !slices.Equal(ops["meho_tenant"], []string{labBID + ":lab-b:Operators"}) {
		t.Errorf("MEHO-lab-b-Operators meho_tenant = %v", ops["meho_tenant"])
	}
	p := fake.groups["uuid-p"].Attributes
	if !slices.Equal(p["platform_admin"], []string{"true"}) || !slices.Equal(p["note"], []string{"kept"}) {
		t.Errorf("platform group attributes = %v", p)
	}
}

func TestBootstrap_TenantFlagsAreChecked(t *testing.T) {
	cases := []struct {
		name string
		edit func(*BootstrapOptions)
		want string
	}{
		{"bad first name", func(o *BootstrapOptions) { o.TenantName = "Main" }, "--tenant-name"},
		{"bad extra name", func(o *BootstrapOptions) { o.AddTenantName = "lab:b" }, "--add-tenant"},
		{"reserved name", func(o *BootstrapOptions) { o.AddTenantName = "platform" }, "taken"},
		{"same name as first", func(o *BootstrapOptions) { o.AddTenantName = "main" }, "first tenant's name"},
		{"id is not a UUID", func(o *BootstrapOptions) {
			o.AddTenantName = "lab-b"
			o.AddTenantID = "lab-b"
		}, "not a UUID"},
		{"id of the first tenant", func(o *BootstrapOptions) {
			o.AddTenantName = "lab-b"
			o.AddTenantID = strings.ToUpper(o.TenantID)
		}, "first tenant's id"},
		{"id of the first tenant as a urn", func(o *BootstrapOptions) {
			o.AddTenantName = "lab-b"
			o.AddTenantID = "urn:uuid:" + o.TenantID
		}, "first tenant's id"},
		{"id without a name", func(o *BootstrapOptions) { o.AddTenantID = labBID }, "needs --add-tenant"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			opts := fullOpts()
			tc.edit(&opts)
			fake := newFakeKeycloak()
			_, _, err := runBootstrapOnce(t, fake, opts)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("err = %v, want it to mention %q", err, tc.want)
			}
			if len(fake.calls) != 0 {
				t.Errorf("Keycloak was called before the check: %v", fake.calls)
			}
		})
	}
}

// The dry run lists the platform group and the extra tenant's groups.
func TestBootstrap_DryRunListsTenantGroups(t *testing.T) {
	opts := fullOpts()
	opts.DryRun = true
	opts.AddTenantName = "lab-b"
	opts.AddTenantID = labBID
	_, stdout, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err != nil {
		t.Fatalf("Bootstrap dry-run: %v", err)
	}
	for _, want := range []string{
		"MEHO-Platform-Admins: platform_admin=true",
		"MEHO-lab-b-SA-Operators: meho_tenant=" + labBID + ":lab-b:SA-Operators",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("dry-run stdout missing %q; got:\n%s", want, stdout)
		}
	}
}
