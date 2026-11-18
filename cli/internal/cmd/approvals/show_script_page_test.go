// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// runShowAgainst runs `meho approvals show <id> --script-page N` against
// a fake backplane that answers the detail and the page read.
func runShowAgainst(t *testing.T, scriptPage int, jsonOut bool, pageStatus int, pageBody string) (string, string, error) {
	t.Helper()
	d := scriptRunView(t, map[string]interface{}{
		"interpreter":      "/bin/bash",
		"script":           "page one\n",
		"script_bytes":     float64(70000),
		"script_sha256":    testScriptSHA,
		"script_truncated": true,
		"script_pages":     float64(2),
	})
	detail, err := json.Marshal(d)
	if err != nil {
		t.Fatalf("marshal detail: %v", err)
	}
	var pageQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "/script") {
			pageQuery = r.URL.RawQuery
			w.WriteHeader(pageStatus)
			_, _ = w.Write([]byte(pageBody))
			return
		}
		_, _ = w.Write(detail)
	}))
	t.Cleanup(srv.Close)

	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cfgDir := filepath.Join(dir, "meho")
	if err := os.MkdirAll(cfgDir, 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	cfgBlob, _ := json.Marshal(map[string]string{"backplane_url": srv.URL})
	if err := os.WriteFile(filepath.Join(cfgDir, "config.json"), cfgBlob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{AccessToken: "test-bearer", BackplaneURL: srv.URL}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}

	cmd := &cobra.Command{Use: "show"}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetContext(context.Background())
	runErr := runShow(cmd, d.Id.String(), jsonOut, srv.URL, scriptPage)
	if scriptPage > 0 && pageQuery != "page="+string(rune('0'+scriptPage)) {
		t.Errorf("page query = %q", pageQuery)
	}
	return stdout.String(), stderr.String(), runErr
}

func TestShowScriptPageTwoPrintsPageTwoOfTwo(t *testing.T) {
	page := `{"approval_request_id":"00000000-0000-0000-0000-000000000001","page":2,"pages":2,` +
		`"script":"last line\n","script_bytes":70000,"script_sha256":"` + testScriptSHA + `"}`
	out, _, err := runShowAgainst(t, 2, false, 200, page)
	if err != nil {
		t.Fatalf("runShow: %v", err)
	}
	// The size of the whole script is on every page, also on a page read with --script-page.
	for _, want := range []string{"Script to run:", "Size:             70000 bytes", "Page:             2 of 2", "| last line"} {
		if !strings.Contains(out, want) {
			t.Errorf("output misses %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, "page one") {
		t.Errorf("page 2 output holds page 1:\n%s", out)
	}
}

func TestShowWithoutTheFlagPrintsPageOneAndNamesTheFlag(t *testing.T) {
	out, _, err := runShowAgainst(t, 0, false, 200, "")
	if err != nil {
		t.Fatalf("runShow: %v", err)
	}
	if !strings.Contains(out, "| page one") || !strings.Contains(out, "--script-page N") {
		t.Errorf("default output:\n%s", out)
	}
}

func TestShowScriptPageAfterTheDecisionSaysMEHODoesNotKeepIt(t *testing.T) {
	body := `{"detail":"MEHO no longer keeps this script. It keeps the script only while the request waits for approval."}`
	_, stderr, err := runShowAgainst(t, 2, false, 410, body)
	if err == nil || !strings.Contains(stderr, "MEHO no longer keeps this script") {
		t.Errorf("err=%v stderr=%q", err, stderr)
	}
}

func TestShowScriptPageOfAScriptMEHOCannotReadSaysSo(t *testing.T) {
	body := `{"detail":"MEHO cannot read the stored script of this request, so it cannot show it."}`
	_, stderr, err := runShowAgainst(t, 2, false, 409, body)
	if err == nil || !strings.Contains(stderr, "MEHO cannot read the stored script") {
		t.Errorf("err=%v stderr=%q", err, stderr)
	}
}
