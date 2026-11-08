// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

const testScriptSHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func scriptRunView(t *testing.T, preview map[string]interface{}) api.ApprovalRequestView {
	t.Helper()
	d := newApprovalView(t, "pending", "")
	d.OpId = "linux.script.run"
	d.ProposedEffect = map[string]interface{}{
		"safety_level": "dangerous",
		"preview":      preview,
	}
	return d
}

func TestShowRequestPrintsTheScriptLineByLine(t *testing.T) {
	d := scriptRunView(t, map[string]interface{}{
		"interpreter":   "/bin/bash",
		"script":        "#!/bin/bash\nset -eu\n\tuname -a\n",
		"script_bytes":  float64(31),
		"script_sha256": testScriptSHA,
		"script_ref":    "app-repo@abc123:check.sh",
		"script_check":  "matches",
	})
	var buf bytes.Buffer
	showRequest(&buf, "Request 1 of 1:", &d)
	out := buf.String()

	for _, want := range []string{
		"  Script to run:\n",
		"    Size:             31 bytes\n",
		"    SHA-256:          " + testScriptSHA + "\n",
		"    Reviewed copy:    app-repo@abc123:check.sh\n",
		"    Expected SHA-256: matches\n",
		"    | #!/bin/bash\n    | set -eu\n    | \tuname -a\n",
		`"script": "(shown below under \"Script to run\")"`,
		`"interpreter": "/bin/bash"`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("showRequest output misses %q in:\n%s", want, out)
		}
	}
	// The JSON block does not repeat the script text.
	if strings.Count(out, "set -eu") != 1 {
		t.Errorf("the script text is printed more than once:\n%s", out)
	}
	// The caller's map is not changed.
	if d.ProposedEffect["preview"].(map[string]interface{})["script"] != "#!/bin/bash\nset -eu\n\tuname -a\n" {
		t.Errorf("printing changed the proposed effect")
	}
}

func TestPrintDetailPrintsTheScriptAndEscapesIt(t *testing.T) {
	d := scriptRunView(t, map[string]interface{}{
		"script":        "echo hi\x1b[2K\rdone\n",
		"script_bytes":  float64(15),
		"script_sha256": testScriptSHA,
		"script_check":  "does not match",
	})
	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	out := stdout.String()

	assertNoRawControls(t, out)
	for _, want := range []string{
		"Script to run:\n",
		"  SHA-256:          " + testScriptSHA + "\n",
		"  Expected SHA-256: does not match\n",
		`  | echo hi\x1b[2K\x0ddone`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printDetail output misses %q in:\n%s", want, out)
		}
	}
}

func TestPrintScriptSaysWhenTextIsHiddenCutOrMissing(t *testing.T) {
	d := scriptRunView(t, map[string]interface{}{
		"script":               "export DB_[REDACTED:api_key]\n",
		"script_bytes":         float64(40),
		"script_values_hidden": true,
		"script_truncated":     true,
	})
	var buf bytes.Buffer
	printScript(&buf, "", d.ProposedEffect)
	out := buf.String()
	for _, want := range []string{
		"Values that look like secrets are hidden below",
		"The script is longer than the text below",
		"  | export DB_[REDACTED:api_key]\n",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printScript output misses %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, "SHA-256:  ") {
		t.Errorf("printScript shows a SHA-256 line with no hash:\n%s", out)
	}
	if strings.Contains(out, "Compare its SHA-256") || !strings.Contains(out, "Read the full script in a reviewed copy") {
		t.Errorf("printScript asks to compare a SHA-256 it does not show:\n%s", out)
	}

	d = scriptRunView(t, map[string]interface{}{
		"script":           "uname -a\n",
		"script_bytes":     float64(70000),
		"script_sha256":    strings.Repeat("a", 64),
		"script_truncated": true,
	})
	buf.Reset()
	printScript(&buf, "", d.ProposedEffect)
	if out := buf.String(); !strings.Contains(out, "Compare its SHA-256 with a reviewed copy") {
		t.Errorf("printScript output for a cut script with a hash:\n%s", out)
	}

	d = scriptRunView(t, map[string]interface{}{
		"script_bytes":       float64(10),
		"script_unavailable": "MEHO could not check the script",
	})
	buf.Reset()
	printScript(&buf, "", d.ProposedEffect)
	if out := buf.String(); !strings.Contains(out, "Script to run:\n") ||
		!strings.Contains(out, "  MEHO could not check the script\n") || strings.Contains(out, "| ") {
		t.Errorf("printScript output for a missing text:\n%s", out)
	}
}

func TestPrintEffectWithoutScriptIsUnchanged(t *testing.T) {
	effect := map[string]interface{}{
		"safety_level": "dangerous",
		"preview":      map[string]interface{}{"path": "/etc/app.conf", "content_bytes": float64(10)},
	}
	var buf bytes.Buffer
	printEffect(&buf, "", "Effect:", effect)
	out := buf.String()
	if strings.Contains(out, "Script to run") {
		t.Errorf("a preview without a script printed a script block:\n%s", out)
	}
	if !strings.HasPrefix(out, "Effect:\n  {\n") || !strings.Contains(out, `"path": "/etc/app.conf"`) {
		t.Errorf("unexpected effect output:\n%s", out)
	}
}
