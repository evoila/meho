// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/cmd/workref"
)

// filterReadCommands are the read commands that filter by change-ticket
// reference with --filter-work-ref.
var filterReadCommands = map[string][]string{
	"audit query":      {"audit", "query"},
	"approvals list":   {"approvals", "list"},
	"scheduler list":   {"scheduler", "list"},
	"runbook runs":     {"runbook", "runs"},
	"agent run-list":   {"agent", "run-list"},
	"broadcast recent": {"broadcast", "recent"},
}

// tagWorkRefCommands are the commands whose own --work-ref has the same
// meaning as the global flag: it tags the run with the reference.
var tagWorkRefCommands = map[string]bool{
	"meho operation call":     true,
	"meho scheduler create":   true,
	"meho runbook start":      true,
	"meho agent run":          true,
	"meho broadcast announce": true,
}

// seenRequest is what the fake backplane keeps from one request: the
// Meho-Work-Ref header (the tag) and the work_ref filter from the query
// string or the JSON body.
type seenRequest struct {
	header, filter string
}

type filterRecorder struct {
	mu   sync.Mutex
	seen []seenRequest
}

func (r *filterRecorder) requests(t *testing.T) []seenRequest {
	t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.seen) == 0 {
		t.Fatal("the backplane saw no request")
	}
	return append([]seenRequest(nil), r.seen...)
}

func newFilterBackplane(t *testing.T) *filterRecorder {
	t.Helper()
	rec := &filterRecorder{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen := seenRequest{header: r.Header.Get(api.WorkRefHeader), filter: r.URL.Query().Get("work_ref")}
		if body, _ := io.ReadAll(r.Body); len(body) > 0 {
			var m map[string]any
			if json.Unmarshal(body, &m) == nil {
				if v, ok := m["work_ref"].(string); ok {
					seen.filter = v
				}
			}
		}
		rec.mu.Lock()
		rec.seen = append(rec.seen, seen)
		rec.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"detail":"test backplane"}`))
	}))
	t.Cleanup(srv.Close)

	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	seedCreds(t, xdg, srv.URL, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(time.Hour),
	})
	return rec
}

func runRootOutErr(t *testing.T, args ...string) (stdout, stderr string) {
	t.Helper()
	t.Cleanup(setDynamicRegistrar(func(*cobra.Command) {}))
	root := newRootCmdFor(args)
	var out, errOut bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetArgs(args)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	_ = root.ExecuteContext(ctx)
	return out.String(), errOut.String()
}

// TestFilterWorkRefOnReadCommands checks, for each read command that
// filters by change-ticket reference, that the global --work-ref tags
// the request and that --filter-work-ref filters it.
func TestFilterWorkRefOnReadCommands(t *testing.T) {
	const tag, filter = "gh:o/r#1", "gh:o/r#2"
	for name, cmdArgs := range filterReadCommands {
		t.Run(name, func(t *testing.T) {
			check := func(t *testing.T, env string, args []string, wantHeader, wantFilter string, wantWarning bool) {
				t.Helper()
				t.Setenv(api.WorkRefEnv, env)
				rec := newFilterBackplane(t)
				stdout, stderr := runRootOutErr(t, args...)
				for _, got := range rec.requests(t) {
					if got.header != wantHeader {
						t.Errorf("%s header = %q; want %q", api.WorkRefHeader, got.header, wantHeader)
					}
					if got.filter != wantFilter {
						t.Errorf("work_ref filter = %q; want %q", got.filter, wantFilter)
					}
				}
				hasWarning := strings.Contains(stderr, "--"+workref.FilterFlag+" to filter")
				if hasWarning != wantWarning {
					t.Errorf("warning on stderr = %v; want %v; stderr=%q", hasWarning, wantWarning, stderr)
				}
				if strings.Contains(stdout, workref.FilterFlag) {
					t.Errorf("the warning must not go to stdout; stdout=%q", stdout)
				}
			}
			with := func(pre []string, post ...string) []string {
				out := append(append(append([]string{}, pre...), cmdArgs...), post...)
				return append(out, "--json")
			}

			t.Run("filter flag filters and does not tag", func(t *testing.T) {
				check(t, "", with(nil, "--"+workref.FilterFlag, filter), "", filter, false)
			})
			t.Run("env tags and does not filter", func(t *testing.T) {
				check(t, tag, with(nil), tag, "", false)
			})
			t.Run("global flag tags; filter flag filters", func(t *testing.T) {
				check(t, "", with([]string{"--work-ref", tag}, "--"+workref.FilterFlag, filter), tag, filter, false)
			})
			// For one release the old spelling still filters, with a
			// warning on stderr. It also tags the request now.
			t.Run("old spelling tags, filters and warns", func(t *testing.T) {
				check(t, "", with([]string{"--work-ref", tag}), tag, tag, true)
			})
			t.Run("old spelling after the command name", func(t *testing.T) {
				check(t, "", with(nil, "--work-ref", tag), tag, tag, true)
			})
			// A filter value that is not in the tag shape still
			// filters; it is not sent as the header.
			t.Run("old spelling with a non-tag value filters and warns", func(t *testing.T) {
				check(t, "", with(nil, "--work-ref", "CHG-123"), "", "CHG-123", true)
			})
			// An empty --filter-work-ref means "no filter". The old
			// spelling then only tags, with no warning.
			t.Run("empty filter flag clears the filter; global flag tags", func(t *testing.T) {
				check(t, "", with([]string{"--work-ref", tag}, "--"+workref.FilterFlag+"="), tag, "", false)
			})
			// With --filter-work-ref given, --work-ref only tags, so a
			// value that is not a tag is an error before any request.
			t.Run("non-tag value with the filter flag is rejected", func(t *testing.T) {
				t.Setenv(api.WorkRefEnv, "")
				rec := newFilterBackplane(t)
				stdout, stderr := runRootOutErr(t, with([]string{"--work-ref", "CHG-123"}, "--"+workref.FilterFlag, filter)...)
				if !strings.Contains(stderr, "--work-ref must match") {
					t.Errorf("stderr = %q; want the shape error", stderr)
				}
				if strings.Contains(stderr, "--"+workref.FilterFlag+" to filter") {
					t.Errorf("stderr = %q; want no fallback warning", stderr)
				}
				if stdout != "" {
					t.Errorf("stdout = %q; want nothing", stdout)
				}
				rec.mu.Lock()
				defer rec.mu.Unlock()
				if len(rec.seen) != 0 {
					t.Errorf("the backplane saw %d request(s); want none", len(rec.seen))
				}
			})
		})
	}
}

// TestWorkRefFlagHasOneMeaning walks the command tree. A command may
// define its own --work-ref only when that flag tags the run like the
// global flag does. Filtering by reference uses --filter-work-ref.
func TestWorkRefFlagHasOneMeaning(t *testing.T) {
	t.Cleanup(setDynamicRegistrar(func(*cobra.Command) {}))
	root := newRootCmdFor(nil)
	var walk func(c *cobra.Command)
	walk = func(c *cobra.Command) {
		if f := c.LocalNonPersistentFlags().Lookup("work-ref"); f != nil && !tagWorkRefCommands[c.CommandPath()] {
			t.Errorf("%s defines its own --work-ref (%q). The global --work-ref tags the request; "+
				"use --%s to filter by reference", c.CommandPath(), f.Usage, workref.FilterFlag)
		}
		if c.HasParent() && c.PersistentFlags().Lookup("work-ref") != nil {
			t.Errorf("%s redefines the persistent --work-ref flag", c.CommandPath())
		}
		for _, sub := range c.Commands() {
			walk(sub)
		}
	}
	walk(root)

	for path := range tagWorkRefCommands {
		c, _, err := root.Find(strings.Fields(path)[1:])
		if err != nil || c.CommandPath() != path || c.LocalNonPersistentFlags().Lookup("work-ref") == nil {
			t.Errorf("%s: want a command with its own tagging --work-ref; update tagWorkRefCommands", path)
		}
	}
	for name, args := range filterReadCommands {
		c, _, err := root.Find(args)
		if err != nil || c.Flags().Lookup(workref.FilterFlag) == nil {
			t.Errorf("meho %s: want a --%s flag", name, workref.FilterFlag)
		}
	}
}
