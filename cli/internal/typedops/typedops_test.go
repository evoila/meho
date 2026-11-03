// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package typedops

import (
	"slices"
	"testing"
)

// TestBackendOpIDsParsesHarbor sanity-checks the parser against a real
// registry: the harbor connector must expose the robot-create typed op
// and nothing that isn't a dotted op_id.
func TestBackendOpIDsParsesHarbor(t *testing.T) {
	ids, err := BackendOpIDs("harbor")
	if err != nil {
		t.Fatalf("BackendOpIDs: %v", err)
	}
	if !slices.Contains(ids, "harbor.robot.create") {
		t.Errorf("expected harbor.robot.create in inventory; got %v", ids)
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
