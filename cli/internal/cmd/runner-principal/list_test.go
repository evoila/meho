// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runnerprincipal

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func TestPrintListTableShowsWireVersionAndPackageSet(t *testing.T) {
	wire := "1"
	digest := "sha256:" + strings.Repeat("ab", 32)
	r := &api.RunnerPrincipalListResponse{Principals: []api.RunnerPrincipalRead{
		{Name: "overlay-runner", OwnerSub: "op", KeycloakClientId: "runner:overlay-runner",
			WireVersion: &wire, PackageSetDigest: &digest},
		{Name: "old-runner", OwnerSub: "op", KeycloakClientId: "runner:old-runner"},
	}}

	var buf bytes.Buffer
	printListTable(&buf, r)
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")

	if len(lines) != 3 {
		t.Fatalf("want header and two rows, got %q", buf.String())
	}
	if !strings.Contains(lines[0], "WIRE") || !strings.Contains(lines[0], "PACKAGES") {
		t.Errorf("header misses a column: %q", lines[0])
	}
	if !strings.Contains(lines[1], "sha256:abababababab ") {
		t.Errorf("row does not show the start of the digest: %q", lines[1])
	}
	if strings.Contains(lines[1], digest) {
		t.Errorf("row shows the full digest: %q", lines[1])
	}
	if got := strings.Fields(lines[2]); got[3] != "-" || got[4] != "-" {
		t.Errorf("a runner that sent nothing shows -: %q", lines[2])
	}
}
