// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// TestCommandHeaderNamesTheCommandPath runs commands through the root
// command and checks the Meho-Command header the backplane gets. A
// command such as `meho vmware vm list` sends the op_id GET:/vcenter/vm,
// which is not in its command line, so the backplane needs the path to
// link the audit row to the agent session that ran the command.
func TestCommandHeaderNamesTheCommandPath(t *testing.T) {
	cases := map[string][]string{
		"vmware vm list": {"vmware", "vm", "list", "--target", "vc-1", "--json"},
		"audit query":    {"--work-ref", "CHG-1", "audit", "query", "--json"},
	}
	for want, args := range cases {
		t.Run(want, func(t *testing.T) {
			var (
				mu   sync.Mutex
				seen []string
			)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				mu.Lock()
				seen = append(seen, r.Header.Get(api.CommandHeader))
				mu.Unlock()
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = w.Write([]byte(`{"detail":"test backplane"}`))
			}))
			t.Cleanup(srv.Close)
			xdg := t.TempDir()
			t.Setenv("XDG_CONFIG_HOME", xdg)
			t.Setenv("MEHO_KEYRING_DISABLE", "1")
			t.Setenv(api.WorkRefEnv, "")
			seedCreds(t, xdg, srv.URL, auth.StoredToken{
				BackplaneURL: srv.URL,
				AccessToken:  "eyJ.test.token",
				TokenType:    "Bearer",
				Expiry:       time.Now().Add(time.Hour),
			})

			runRootOutErr(t, args...)

			mu.Lock()
			defer mu.Unlock()
			if len(seen) == 0 {
				t.Fatal("the backplane saw no request")
			}
			for _, got := range seen {
				if got != want {
					t.Errorf("%s = %q; want %q", api.CommandHeader, got, want)
				}
			}
		})
	}
}
