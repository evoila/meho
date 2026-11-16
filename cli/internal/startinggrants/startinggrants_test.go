// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package startinggrants

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

func TestBodyWithoutFlagsSendsNoPick(t *testing.T) {
	pick, err := Flags{}.Body()
	if err != nil || pick != nil {
		t.Fatalf("Body() = %+v, %v; want nil, nil", pick, err)
	}
}

func TestBodyBuildsEachSet(t *testing.T) {
	for _, set := range []string{SetNone, SetReadEverything} {
		pick, err := Flags{Set: set}.Body()
		if err != nil {
			t.Fatalf("%s: %v", set, err)
		}
		if string(pick.Set) != set || pick.Targets != nil {
			t.Errorf("%s: got %+v", set, pick)
		}
	}
	pick, err := Flags{Set: SetReadTargets, Targets: []string{"target-a", " target-b "}}.Body()
	if err != nil {
		t.Fatalf("read-targets: %v", err)
	}
	if pick.Set != api.StartingGrantSetReadTargets || pick.Targets == nil ||
		strings.Join(*pick.Targets, ",") != "target-a,target-b" {
		t.Errorf("read-targets: got %+v", pick)
	}
	raw, _ := json.Marshal(pick)
	if string(raw) != `{"set":"read-targets","targets":["target-a","target-b"]}` {
		t.Errorf("wire body: %s", raw)
	}
}

func TestBodyRefusesWrongCombinations(t *testing.T) {
	cases := []struct {
		name  string
		flags Flags
		want  string
	}{
		{"target without grants", Flags{Targets: []string{"a"}}, "--target needs --grants read-targets"},
		{"target with none", Flags{Set: SetNone, Targets: []string{"a"}}, "only used with --grants read-targets"},
		{"target with read-everything", Flags{Set: SetReadEverything, Targets: []string{"a"}}, "only used with --grants read-targets"},
		{"read-targets without target", Flags{Set: SetReadTargets}, "needs at least one --target"},
		{"empty target", Flags{Set: SetReadTargets, Targets: []string{" "}}, "must not be empty"},
		{"unknown set", Flags{Set: "everything"}, "--grants read-everything"},
		{"wrong case", Flags{Set: "Read-Everything"}, "unknown --grants value"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pick, err := tc.flags.Body()
			if err == nil {
				t.Fatalf("want an error, got pick %+v", pick)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error %q does not contain %q", err, tc.want)
			}
		})
	}
}

func TestAddFlagsRegistersBothFlags(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	var f Flags
	AddFlags(cmd, &f)
	if err := cmd.ParseFlags([]string{"--grants", "read-targets", "--target", "a", "--target", "b,c"}); err != nil {
		t.Fatalf("parse: %v", err)
	}
	// A comma stays inside one name; only a repeated flag adds a name.
	if f.Set != SetReadTargets || len(f.Targets) != 2 || f.Targets[1] != "b,c" {
		t.Errorf("flags: %+v", f)
	}
}

func TestRequestErrorPrintsTheChoicesWhenPickIsMissing(t *testing.T) {
	body := []byte(`{"detail":"starting_grants_required: pick the agent's starting grants"}`)
	got := RequestError(http.StatusUnprocessableEntity, body)
	if got == nil {
		t.Fatal("want an error")
	}
	for _, want := range []string{"Nothing was created", "--grants none", "--grants read-everything", "--grants read-targets --target"} {
		if !strings.Contains(got.Detail, want) {
			t.Errorf("detail %q does not contain %q", got.Detail, want)
		}
	}
}

// The limits of none and read-targets hold only with AGENT_GRANT_REQUIRED=on.
// The CLI cannot see the server's setting, so both texts say so.
func TestTextsSayTheLimitsNeedTheSwitchOn(t *testing.T) {
	cmd := &cobra.Command{Use: "x"}
	AddFlags(cmd, &Flags{})
	texts := map[string]string{"choices": Choices, "--grants help": cmd.Flags().Lookup("grants").Usage}
	for name, text := range texts {
		for _, want := range []string{
			"with AGENT_GRANT_REQUIRED=on the agent runs nothing until an admin adds a grant",
			"with AGENT_GRANT_REQUIRED=on, only there",
			"with AGENT_GRANT_REQUIRED off (the default) or observe, the agent can also run safe operations that no grant covers",
		} {
			if !strings.Contains(text, want) {
				t.Errorf("%s %q does not contain %q", name, text, want)
			}
		}
		for _, promise := range []string{"none: the agent runs nothing", "none                      the agent runs nothing", "the same, only on the targets"} {
			if strings.Contains(text, promise) {
				t.Errorf("%s still promises %q without the switch", name, promise)
			}
		}
	}
}

func TestRequestErrorShowsOtherPickErrors(t *testing.T) {
	body := []byte(`{"detail":"starting_grants_target_unknown: no target named 'x' in this tenant. Nothing was created."}`)
	got := RequestError(http.StatusUnprocessableEntity, body)
	if got == nil || !strings.Contains(got.Detail, "no target named 'x'") {
		t.Fatalf("got %+v", got)
	}
}

func TestRequestErrorIgnoresOtherAnswers(t *testing.T) {
	cases := []struct {
		status int
		body   string
	}{
		{http.StatusConflict, `{"detail":"starting_grants_required"}`},
		{http.StatusUnprocessableEntity, `{"detail":"identity_ref_unknown"}`},
		{http.StatusUnprocessableEntity, `{"detail":[{"loc":["body","name"],"msg":"bad"}]}`},
		{http.StatusUnprocessableEntity, `not json`},
	}
	for _, tc := range cases {
		if got := RequestError(tc.status, []byte(tc.body)); got != nil {
			t.Errorf("%d %s: want nil, got %+v", tc.status, tc.body, got)
		}
	}
}

func TestWithGrantsAddsTheGrants(t *testing.T) {
	entry := map[string]any{"name": "bot"}
	out, err := WithGrants(entry, []api.AgentGrantRead{sampleGrant(0)}, nil)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(out)
	if !strings.Contains(string(raw), `"name":"bot"`) || !strings.Contains(string(raw), `"op_pattern":"*"`) {
		t.Errorf("json: %s", raw)
	}
	if _, ok := out["grants_error"]; ok {
		t.Errorf("no grants_error on success: %s", raw)
	}
}

func TestWithGrantsMarksAFailedReadAsUnknown(t *testing.T) {
	out, err := WithGrants(map[string]any{"name": "bot"}, nil, errors.New("HTTP 500: boom"))
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(out)
	if !strings.Contains(string(raw), `"grants":null`) || !strings.Contains(string(raw), `"grants_error":"HTTP 500: boom"`) {
		t.Errorf("json: %s", raw)
	}
}

func TestWithGrantsKeepsAnEmptyListAsEmpty(t *testing.T) {
	out, _ := WithGrants(map[string]any{}, []api.AgentGrantRead{}, nil)
	raw, _ := json.Marshal(out)
	if string(raw) != `{"grants":[]}` {
		t.Errorf("json: %s", raw)
	}
}

func TestPrintGrants(t *testing.T) {
	var buf bytes.Buffer
	scope := "33333333-3333-3333-3333-333333333333"
	g := sampleGrant(0)
	g.TargetScope = &scope
	PrintGrants(&buf, "agent:bot", []api.AgentGrantRead{sampleGrant(1), g}, nil)
	out := buf.String()
	if !strings.Contains(out, "grants of agent:bot:") || !strings.Contains(out, "target=every target") ||
		!strings.Contains(out, "target="+scope) {
		t.Errorf("output:\n%s", out)
	}

	buf.Reset()
	PrintGrants(&buf, "agent:bot", nil, nil)
	if !strings.Contains(buf.String(), "grants of agent:bot: none") {
		t.Errorf("empty output: %q", buf.String())
	}

	buf.Reset()
	PrintGrants(&buf, "agent:bot", nil, errors.New("HTTP 500"))
	if !strings.Contains(buf.String(), "could not be read") || strings.Contains(buf.String(), ": none") {
		t.Errorf("failed read output: %q", buf.String())
	}
}

func TestReadGrantsFollowsEveryPage(t *testing.T) {
	var offsets []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/grants" || r.URL.Query().Get("principal_sub") != "agent:bot" {
			t.Errorf("unexpected request %s", r.URL)
		}
		offset := r.URL.Query().Get("offset")
		offsets = append(offsets, offset)
		n := pageSize
		if offset != "0" {
			n = 3
		}
		grants := make([]api.AgentGrantRead, n)
		for i := range grants {
			grants[i] = sampleGrant(i)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AgentGrantListResponse{Grants: grants})
	}))
	defer srv.Close()

	grants, err := ReadGrants(context.Background(), testClient(t, srv), "agent:bot")
	if err != nil {
		t.Fatal(err)
	}
	if len(grants) != pageSize+3 {
		t.Errorf("got %d grants, want %d", len(grants), pageSize+3)
	}
	if strings.Join(offsets, ",") != "0,"+strconv.Itoa(pageSize) {
		t.Errorf("offsets %v", offsets)
	}
}

func TestReadGrantsReturnsAnErrorOnAFailedRead(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		fmt.Fprint(w, `{"detail":"boom"}`)
	}))
	defer srv.Close()

	grants, err := ReadGrants(context.Background(), testClient(t, srv), "agent:bot")
	if err == nil || !strings.Contains(err.Error(), "HTTP 500") || grants != nil {
		t.Fatalf("got %v, %v", grants, err)
	}
}

func TestReadGrantsStopsAtThePageLimit(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		grants := make([]api.AgentGrantRead, pageSize)
		for i := range grants {
			grants[i] = sampleGrant(i)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AgentGrantListResponse{Grants: grants})
	}))
	defer srv.Close()

	grants, err := ReadGrants(context.Background(), testClient(t, srv), "agent:bot")
	if err == nil || !strings.Contains(err.Error(), "meho agent grant list") || grants != nil {
		t.Fatalf("got %d grants, %v", len(grants), err)
	}
}

func sampleGrant(i int) api.AgentGrantRead {
	at := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	return api.AgentGrantRead{
		Id:           uuid.MustParse(fmt.Sprintf("00000000-0000-0000-0000-%012d", i)),
		TenantId:     uuid.MustParse("22222222-2222-2222-2222-222222222222"),
		PrincipalSub: "agent:bot",
		OpPattern:    "*",
		Verdict:      "auto-execute",
		CreatedBySub: "person-sub",
		CreatedAt:    at,
		UpdatedAt:    at,
	}
}

type memStore map[string]auth.StoredToken

func (m memStore) Save(service, user string, tok auth.StoredToken) error {
	m[service+"|"+user] = tok
	return nil
}

func (m memStore) Load(service, user string) (auth.StoredToken, error) {
	tok, ok := m[service+"|"+user]
	if !ok {
		return auth.StoredToken{}, auth.ErrTokenNotFound
	}
	return tok, nil
}

func (m memStore) Delete(service, user string) error {
	delete(m, service+"|"+user)
	return nil
}

func (memStore) Describe() string { return "test store" }

func testClient(t *testing.T, srv *httptest.Server) *api.AuthedClient {
	t.Helper()
	store := memStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL, AccessToken: "test-bearer", Expiry: time.Now().Add(time.Hour),
	})
	client, err := api.NewAuthedClient(context.Background(), srv.URL,
		api.AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	return client
}
