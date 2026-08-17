// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package typedops reads the backplane's per-connector typed-op
// registries so the per-connector typed_opid_dispatch_test.go guards
// can reconcile the CLI's dispatched op_ids against the backend's
// typed-op inventory (#2942). A typed connector resolves only dotted
// typed op_ids on a zero-catalog boot, so a CLI verb left on a legacy
// `METHOD:/path` op_id after its backend read went typed is a dead
// end; the guards fail closed when the backend gains a typed op the
// CLI has not classified. Test support only — no runtime code imports
// this package.
package typedops

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
)

// opIDPattern matches typed-op registrations (`op_id="dotted.op.id"`)
// in the backend connector modules. The dataclass field declaration
// (`op_id: str`) and interpolated error messages (`{op.op_id!r}`) do
// not match. The character class is deliberately wide so an
// unconventionally named future op still lands in the inventory
// instead of silently escaping the guard.
var opIDPattern = regexp.MustCompile(`op_id="([A-Za-z0-9_.-]+)"`)

// BackendOpIDs returns the sorted, de-duplicated set of typed op_ids
// declared under backend/src/meho_backplane/connectors/<connectorDir>.
//
// The backend tree is located by ascending from the current working
// directory (the package directory under `go test`), so the helper
// works from any package depth inside cli/ as long as the monorepo
// checkout is intact. A checkout without backend/ is an error, not a
// skip — the guard must stay fail-closed.
func BackendOpIDs(connectorDir string) ([]string, error) {
	root, err := findRepoRoot()
	if err != nil {
		return nil, err
	}
	dir := filepath.Join(root, "backend", "src", "meho_backplane", "connectors", connectorDir)
	seen := map[string]struct{}{}
	walkErr := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || filepath.Ext(path) != ".py" {
			return nil
		}
		raw, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		for _, m := range opIDPattern.FindAllSubmatch(raw, -1) {
			seen[string(m[1])] = struct{}{}
		}
		return nil
	})
	if walkErr != nil {
		return nil, walkErr
	}
	if len(seen) == 0 {
		return nil, fmt.Errorf("no op_id=%q declarations found under %s", "...", dir)
	}
	ids := make([]string, 0, len(seen))
	for id := range seen {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids, nil
}

// findRepoRoot ascends from the working directory until it finds the
// directory that contains backend/src/meho_backplane/connectors.
func findRepoRoot() (string, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", err
	}
	start := dir
	for {
		marker := filepath.Join(dir, "backend", "src", "meho_backplane", "connectors")
		if info, statErr := os.Stat(marker); statErr == nil && info.IsDir() {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf(
				"backend/src/meho_backplane/connectors not found above %s "+
					"(the typed-op dispatch guard needs the full monorepo checkout)",
				start,
			)
		}
		dir = parent
	}
}
