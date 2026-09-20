// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"testing"
)

// ---------- command-tree shape tests for the group verbs (#3280) ----------

func TestGroupHasVerbs(t *testing.T) {
	got := map[string]bool{}
	for _, s := range newGroupCmd().Commands() {
		got[s.Name()] = true
	}
	for _, name := range []string{"list", "create", "update-attributes", "member"} {
		if !got[name] {
			t.Errorf("group is missing sub-verb %q", name)
		}
	}
}

func TestGroupMemberHasVerbs(t *testing.T) {
	got := map[string]bool{}
	for _, s := range newGroupMemberCmd().Commands() {
		got[s.Name()] = true
	}
	for _, name := range []string{"add", "remove", "list"} {
		if !got[name] {
			t.Errorf("group member is missing sub-verb %q", name)
		}
	}
}

// TestGroupGraftedOntoRoot proves the group tree is reachable from the
// `meho keycloak` root command.
func TestGroupGraftedOntoRoot(t *testing.T) {
	found := false
	for _, s := range NewRootCmd().Commands() {
		if s.Name() == "group" {
			found = true
		}
	}
	if !found {
		t.Errorf("group sub-tree not grafted onto `meho keycloak`")
	}
}

// ---------- attribute-flag parser ----------

func TestParseAttributeFlagsAccumulates(t *testing.T) {
	attrs, serr := parseAttributeFlags([]string{"tenant_id=t-2", "roles=a", "roles=b"})
	if serr != nil {
		t.Fatalf("parseAttributeFlags: %v", serr)
	}
	if got, _ := attrs["tenant_id"].([]any); len(got) != 1 || got[0] != "t-2" {
		t.Errorf("tenant_id: got %v", attrs["tenant_id"])
	}
	roles, _ := attrs["roles"].([]any)
	if len(roles) != 2 || roles[0] != "a" || roles[1] != "b" {
		t.Errorf("repeated key should accumulate; got %v", attrs["roles"])
	}
}

func TestParseAttributeFlagsRejectsMissingEquals(t *testing.T) {
	if _, serr := parseAttributeFlags([]string{"noequalsign"}); serr == nil {
		t.Errorf("a flag without = must be rejected")
	}
}

func TestParseAttributeFlagsEmptyIsNil(t *testing.T) {
	attrs, serr := parseAttributeFlags(nil)
	if serr != nil || attrs != nil {
		t.Errorf("empty input should yield (nil, nil); got (%v, %v)", attrs, serr)
	}
}

// ---------- dispatch shape: every group write op uses its canonical op_id ----------

func TestGroupWriteOpsDispatchCanonicalOpIDs(t *testing.T) {
	dispatched := make(map[string]bool)
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "keycloak-admin-26.x" {
				t.Errorf("connector_id: got %q", body.ConnectorID)
			}
			dispatched[body.OpID] = true
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: body.OpID,
				Result: json.RawMessage(`{"created":true}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	writeOps := []string{
		"keycloak.group.create",
		"keycloak.group.update_attributes",
		"keycloak.group.member.add",
		"keycloak.group.member.remove",
	}
	for _, opID := range writeOps {
		if _, err := dispatchOp(context.Background(), srv.URL, opID, "rdc-keycloak", nil); err != nil {
			t.Fatalf("dispatchOp %s: %v", opID, err)
		}
	}
	for _, opID := range writeOps {
		if !dispatched[opID] {
			t.Errorf("group write op_id %q was not dispatched", opID)
		}
	}
}

// TestGroupCreateForwardsAttributes — the create verb folds repeatable
// --attribute key=value flags into the Keycloak Map<String,[]string> shape.
func TestGroupCreateForwardsAttributes(t *testing.T) {
	var captured map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "keycloak.group.create",
				Result: json.RawMessage(`{"created":true,"id":"g-1"}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newGroupCreateCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{
		"--target", "rdc-keycloak",
		"--name", "role-tenant-2",
		"--attribute", "tenant_id=t-2",
		"--attribute", "tenant_role=admin",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	params, _ := captured["params"].(map[string]any)
	if params == nil {
		t.Fatalf("no params captured: %v", captured)
	}
	if params["name"] != "role-tenant-2" {
		t.Errorf("name not forwarded: %v", params["name"])
	}
	attrs, _ := params["attributes"].(map[string]any)
	if attrs == nil {
		t.Fatalf("attributes not forwarded: %v", params)
	}
	tenantID, _ := attrs["tenant_id"].([]any)
	if len(tenantID) != 1 || tenantID[0] != "t-2" {
		t.Errorf("tenant_id attribute not in {key: [values]} shape: %v", attrs["tenant_id"])
	}
}

// TestGroupListForwardsAttributesFlag — --attributes toggles brief=false so
// the backplane returns the attribute-bearing group projection.
func TestGroupListForwardsAttributesFlag(t *testing.T) {
	var captured map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "keycloak.group.list",
				Result: json.RawMessage(`{"rows":[],"total":0}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newGroupListCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"--target", "rdc-keycloak", "--attributes", "--backplane", srv.URL})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	params, _ := captured["params"].(map[string]any)
	if params == nil {
		t.Fatalf("no params captured")
	}
	if brief, ok := params["brief"].(bool); !ok || brief {
		t.Errorf("--attributes should set brief=false; got %v", params["brief"])
	}
}

// TestGroupMemberAddForwardsRefs — the member add verb forwards the group +
// user references without inventing any inline secret.
func TestGroupMemberAddForwardsRefs(t *testing.T) {
	var captured map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "keycloak.group.member.add",
				Result: json.RawMessage(`{"added":true,"unchanged":false}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newGroupMemberAddCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{
		"--target", "rdc-keycloak",
		"--group-name", "role-tenant",
		"--username", "operator-a",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	params, _ := captured["params"].(map[string]any)
	if params == nil {
		t.Fatalf("no params captured")
	}
	if params["group_name"] != "role-tenant" {
		t.Errorf("group_name not forwarded: %v", params["group_name"])
	}
	if params["username"] != "operator-a" {
		t.Errorf("username not forwarded: %v", params["username"])
	}
}

// TestGroupMemberAddRequiresGroupAndUser — a member mutation with neither a
// group nor a user reference fails before dispatch.
func TestGroupMemberAddRequiresGroupAndUser(t *testing.T) {
	cmd := newGroupMemberAddCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"--target", "rdc-keycloak", "--backplane", "https://x.test"})
	if err := cmd.Execute(); err == nil {
		t.Errorf("member add with no group/user ref should error")
	}
}
