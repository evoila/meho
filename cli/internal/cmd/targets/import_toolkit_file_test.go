// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The made-up toolkit targets file is shared with the Python tests. It uses
// every key of docs/schemas/target-record-v1.schema.json and must import
// without edits.
func TestToolkitTargetsFileImportsWithoutEdits(t *testing.T) {
	t.Parallel()
	path := filepath.Join("..", "..", "..", "..", "backend", "tests", "fixtures", "toolkit-targets-v1.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	entries, err := parseTargetsYAML(data)
	if err != nil {
		t.Fatalf("parseTargetsYAML: %v", err)
	}
	if len(entries) != 3 {
		t.Fatalf("entries: got %d; want 3", len(entries))
	}

	vc, vcWarnings := entryToCreateBody(entries[0])
	if vc["version"] != "8.0" {
		t.Errorf("version: got %v; want top-level \"8.0\"", vc["version"])
	}
	if _, ok := vc["fingerprint"]; ok {
		t.Errorf("body must not carry fingerprint")
	}
	extras, _ := vc["extras"].(map[string]any)
	if len(extras) != 1 || extras["sso_realm"] != "lab.example.com" {
		t.Errorf("extras: got %v; want only sso_realm", extras)
	}
	// One warning for the skipped fingerprint, one for the key the schema does not list.
	if len(vcWarnings) != 2 {
		t.Errorf("warnings: got %v; want 2", vcWarnings)
	}

	web, webWarnings := entryToCreateBody(entries[1])
	if _, ok := web["secret_field_map"]; !ok {
		t.Errorf("secret_field_map must be a top-level key")
	}
	if web["flight_recorder_capture"] != false {
		t.Errorf("flight_recorder_capture: got %v; want false", web["flight_recorder_capture"])
	}
	if len(webWarnings) != 0 {
		t.Errorf("warnings: got %v; want none", webWarnings)
	}

	k8s, _ := entryToCreateBody(entries[2])
	if k8s["verify_tls"] != false || k8s["version"] != "1.31" {
		t.Errorf("verify_tls/version: got %v/%v", k8s["verify_tls"], k8s["version"])
	}
	if _, spilled := k8s["extras"]; spilled {
		t.Errorf("no key should spill into extras: got %v", k8s["extras"])
	}
	for _, w := range append(vcWarnings, webWarnings...) {
		if strings.Contains(w, "version") {
			t.Errorf("unexpected warning about version: %s", w)
		}
	}
}
