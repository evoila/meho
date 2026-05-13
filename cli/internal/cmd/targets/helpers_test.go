// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// jwtMarker is the base64-URL prefix every JWT carries. Tests use it
// as a sentinel to verify no bearer value leaks into output.
const jwtMarker = "eyJ.TEST-DUMMY-TOKEN-TARGETS"

// withTempXDG redirects XDG_CONFIG_HOME + MEHO_KEYRING_DISABLE so the
// test exercises the file-backed token store in an isolated tmpdir.
func withTempXDG(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	return dir
}

// seedCreds persists a stored token + config at the supplied XDG dir.
func seedCreds(t *testing.T, xdg, backplaneURL string) {
	t.Helper()
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(filepath.Join(xdg, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
}

// runCobraCmd executes a cobra command with the given argv.
// Returns captured stdout, stderr, and the RunE error (if any).
func runCobraCmd(t *testing.T, cmd *cobra.Command, argv ...string) (stdout, stderr *bytes.Buffer, err error) {
	t.Helper()
	stdout = &bytes.Buffer{}
	stderr = &bytes.Buffer{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	cmd.SetArgs(argv)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd.SetContext(ctx)
	err = cmd.Execute()
	return
}
