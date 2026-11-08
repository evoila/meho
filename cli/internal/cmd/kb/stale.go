// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"encoding/json"
	"fmt"
	"strings"
)

// staleDetail is the `detail` of a 409 stale_revision answer from
// POST /api/v1/kb: the entry as it is now, and what changed since the
// revision the writer read. The body diff and the metadata key names
// are stored text, so the backplane wraps them as untrusted text.
type staleDetail struct {
	Error                 string   `json:"error"`
	Slug                  string   `json:"slug"`
	Cause                 string   `json:"cause"`
	CurrentRevisionID     string   `json:"current_revision_id"`
	CurrentRevisionNumber *int     `json:"current_revision_number"`
	ChangedBy             *string  `json:"changed_by"`
	ChangedAt             *string  `json:"changed_at"`
	ChangedMetadataKeys   []string `json:"changed_metadata_keys"`
	BodyDiff              *string  `json:"body_diff"`
	BodyDiffTruncated     bool     `json:"body_diff_truncated"`
}

// describeStale turns a 409 body into plain text for the operator.
// It reports false when the body is not a stale_revision answer.
func describeStale(body []byte) (string, bool) {
	var env struct {
		Detail staleDetail `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err != nil || env.Detail.Error != "stale_revision" {
		return "", false
	}
	d := env.Detail
	var b strings.Builder
	switch d.Cause {
	case "exists":
		fmt.Fprintf(&b, "kb entry %q exists already, and no --expected-revision was given.\n", d.Slug)
	case "gone":
		fmt.Fprintf(&b, "kb entry %q was deleted after you read it.\n", d.Slug)
	default:
		fmt.Fprintf(&b, "kb entry %q changed after the revision you read.\n", d.Slug)
	}
	if d.CurrentRevisionID != "" {
		number := ""
		if d.CurrentRevisionNumber != nil {
			number = fmt.Sprintf(" (revision %d)", *d.CurrentRevisionNumber)
		}
		fmt.Fprintf(&b, "current revision: %s%s\n", d.CurrentRevisionID, number)
	}
	if d.ChangedBy != nil {
		fmt.Fprintf(&b, "changed by: %s\n", *d.ChangedBy)
	}
	if d.ChangedAt != nil {
		fmt.Fprintf(&b, "changed at: %s\n", *d.ChangedAt)
	}
	if len(d.ChangedMetadataKeys) > 0 {
		fmt.Fprintf(&b, "metadata keys that changed: %s\n", strings.Join(d.ChangedMetadataKeys, ", "))
	}
	if d.BodyDiff != nil && *d.BodyDiff != "" {
		fmt.Fprintf(&b, "body changes since the revision you read:\n%s\n", *d.BodyDiff)
		if d.BodyDiffTruncated {
			b.WriteString("(the list of changes was cut)\n")
		}
	}
	if d.CurrentRevisionID != "" {
		fmt.Fprintf(&b, "Read the entry with `meho kb show %s --json`, merge your change, "+
			"then run `meho kb add` with --expected-revision %s.", d.Slug, d.CurrentRevisionID)
	}
	return strings.TrimSpace(b.String()), true
}
