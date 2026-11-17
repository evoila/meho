// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/startinggrants"
)

// savedAnswer returns a create or edit answer with the given live grants.
func savedAnswer(grants []api.LiveGrant, truncated bool) map[string]any {
	raw, _ := json.Marshal(sampleDefinition())
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	out["live_grants"] = grants
	out["live_grants_truncated"] = truncated
	return out
}

func liveServer(t *testing.T, path string, answer map[string]any, status int) string {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc(path, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(answer)
	})
	mux.HandleFunc("/api/v1/agents/grants", func(_ http.ResponseWriter, _ *http.Request) {
		t.Errorf("no grant read without --grants")
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv.URL
}

func TestRunCreateWithoutPickPrintsTheLiveGrants(t *testing.T) {
	target := "cluster-a"
	ends := time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)
	grants := []api.LiveGrant{
		{Id: uuid.New(), OpPattern: "*", TargetScope: "*", Verdict: "auto-execute"},
		{Id: uuid.New(), OpPattern: "k8s.*", TargetScope: uuid.NewString(), TargetName: &target,
			Verdict: "needs-approval", ExpiresAt: &ends},
	}
	url := liveServer(t, "/api/v1/agents", savedAnswer(grants, false), http.StatusCreated)

	cmd, stdout, stderr := newTestCmd(t)
	opts := createOpts(url, startinggrants.Flags{})
	opts.JSONOut = false
	if err := runCreate(cmd, opts); err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		startinggrants.LiveNote,
		"op=* target=every target verdict=auto-execute ends=never",
		"op=k8s.* target=cluster-a verdict=needs-approval ends=2026-12-01T00:00:00Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in output:\n%s", want, out)
		}
	}
	if strings.Contains(out, "more grants") {
		t.Errorf("a whole list must not say it was cut:\n%s", out)
	}
}

func TestRunEditWithoutPickPrintsTheLiveGrants(t *testing.T) {
	grants := []api.LiveGrant{{Id: uuid.New(), OpPattern: "vault.*", TargetScope: "*", Verdict: "deny"}}
	url := liveServer(t, "/api/v1/agents/incident-triage", savedAnswer(grants, true), http.StatusOK)

	cmd, stdout, stderr := newTestCmd(t)
	opts := editOpts(url, startinggrants.Flags{})
	opts.JSONOut = false
	if err := runEdit(cmd, opts); err != nil {
		t.Fatalf("runEdit: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	if !strings.Contains(out, startinggrants.LiveNote) || !strings.Contains(out, "op=vault.* target=every target verdict=deny") {
		t.Errorf("output:\n%s", out)
	}
	if !strings.Contains(out, "meho agent grant list --principal agent:incident-triage") {
		t.Errorf("a cut list must say so:\n%s", out)
	}
}

func TestRunCreateWithoutLiveGrantsPrintsNoNote(t *testing.T) {
	url := liveServer(t, "/api/v1/agents", savedAnswer([]api.LiveGrant{}, false), http.StatusCreated)

	cmd, stdout, stderr := newTestCmd(t)
	opts := createOpts(url, startinggrants.Flags{})
	opts.JSONOut = false
	if err := runCreate(cmd, opts); err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	if strings.Contains(stdout.String(), startinggrants.LiveNote) {
		t.Errorf("no note without live grants:\n%s", stdout.String())
	}
}

func TestRunCreateJSONHasTheLiveGrants(t *testing.T) {
	grants := []api.LiveGrant{{Id: uuid.New(), OpPattern: "*", TargetScope: "*", Verdict: "auto-execute"}}
	url := liveServer(t, "/api/v1/agents", savedAnswer(grants, false), http.StatusCreated)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runCreate(cmd, createOpts(url, startinggrants.Flags{})); err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	var out map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
		t.Fatalf("decode %q: %v", stdout.String(), err)
	}
	if live, ok := out["live_grants"].([]any); !ok || len(live) != 1 {
		t.Errorf("json output: %s", stdout.String())
	}
}
