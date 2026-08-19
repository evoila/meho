// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package typedops

import (
	"slices"
	"testing"
)

// TestBackendOpIDsParsesVcfAutomation sanity-checks the parser against
// a real registry: the vcf_automation connector must expose the #2839
// deployment-list typed op and nothing that isn't a dotted op_id.
func TestBackendOpIDsParsesVcfAutomation(t *testing.T) {
	ids, err := BackendOpIDs("vcf_automation")
	if err != nil {
		t.Fatalf("BackendOpIDs: %v", err)
	}
	if !slices.Contains(ids, "vcfa.tenant.deployment.list") {
		t.Errorf("expected vcfa.tenant.deployment.list in inventory; got %v", ids)
	}
	if !slices.IsSorted(ids) {
		t.Errorf("inventory should be sorted; got %v", ids)
	}
}

// TestBackendOpIDsUnknownConnector fails closed on a missing dir.
func TestBackendOpIDsUnknownConnector(t *testing.T) {
	if _, err := BackendOpIDs("no-such-connector"); err == nil {
		t.Fatal("expected an error for a nonexistent connector dir")
	}
}
