// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runnerprincipal

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

const setAsideBase = `"id":"11111111-1111-1111-1111-111111111111",` +
	`"tenant_id":"22222222-2222-2222-2222-222222222222","keycloak_internal_id":"kc",` +
	`"owner_sub":"op","created_by_sub":"op","revoked":false,` +
	`"created_at":"2026-11-01T00:00:00Z","updated_at":"2026-11-01T00:00:00Z","last_seen_at":null`

// The answers of the server with RUNNER_CHECKS_ENABLED on: one runner with a
// count and a time, one with 0 and no time.
const (
	withCountJSON = `{"name":"with-count","keycloak_client_id":"runner:with-count",` + setAsideBase +
		`,"set_aside_count":3,"set_aside_last_at":"2026-11-20T10:30:00Z"}`
	noRowJSON = `{"name":"no-row","keycloak_client_id":"runner:no-row",` + setAsideBase +
		`,"set_aside_count":0}`
	switchOffJSON = `{"name":"off","keycloak_client_id":"runner:off",` + setAsideBase + `}`
)

func readPrincipal(t *testing.T, body string) api.RunnerPrincipalRead {
	t.Helper()
	var e api.RunnerPrincipalRead
	if err := json.Unmarshal([]byte(body), &e); err != nil {
		t.Fatalf("decode %s: %v", body, err)
	}
	return e
}

func listLines(t *testing.T, bodies ...string) []string {
	t.Helper()
	r := &api.RunnerPrincipalListResponse{}
	for _, body := range bodies {
		r.Principals = append(r.Principals, readPrincipal(t, body))
	}
	var buf bytes.Buffer
	printListTable(&buf, r)
	return strings.Split(strings.TrimSpace(buf.String()), "\n")
}

func TestListShowsTheSetAsideCountAndTime(t *testing.T) {
	lines := listLines(t, withCountJSON, noRowJSON)

	if !strings.Contains(lines[0], " SET ASIDE ") || !strings.Contains(lines[0], "LAST SET ASIDE") {
		t.Errorf("header misses a column: %q", lines[0])
	}
	if got := strings.Fields(lines[1]); got[5] != "3" || got[6] != "2026-11-20T10:30:00Z" {
		t.Errorf("row misses the count or the time: %q", lines[1])
	}
	if got := strings.Fields(lines[2]); got[5] != "0" || got[6] != "-" {
		t.Errorf("a runner that set nothing aside shows 0 and -: %q", lines[2])
	}
	if got := strings.Fields(lines[1]); got[7] != "runner:with-count" {
		t.Errorf("row misses the client id: %q", lines[1])
	}
}

func TestListHasNoSetAsideColumnsWithRunnerChecksOff(t *testing.T) {
	lines := listLines(t, switchOffJSON)

	if strings.Contains(lines[0], "SET ASIDE") {
		t.Errorf("header has a set-aside column: %q", lines[0])
	}
	if got := strings.Fields(lines[1]); len(got) != 6 {
		t.Errorf("want six values, got %q", lines[1])
	}
}

func TestShowPrintsTheSetAsideCountAndTime(t *testing.T) {
	for body, want := range map[string][]string{
		withCountJSON: {"set_aside_count:     3\n", "set_aside_last_at:   2026-11-20T10:30:00Z\n"},
		noRowJSON:     {"set_aside_count:     0\n", "set_aside_last_at:   -\n"},
	} {
		e := readPrincipal(t, body)
		var buf bytes.Buffer
		printEntrySummary(&buf, &e)
		for _, line := range want {
			if !strings.Contains(buf.String(), line) {
				t.Errorf("output of %s lacks %q:\n%s", e.Name, line, buf.String())
			}
		}
	}

	off := readPrincipal(t, switchOffJSON)
	var buf bytes.Buffer
	printEntrySummary(&buf, &off)
	if strings.Contains(buf.String(), "set_aside") {
		t.Errorf("output has a set-aside line with runner checks off:\n%s", buf.String())
	}
}
