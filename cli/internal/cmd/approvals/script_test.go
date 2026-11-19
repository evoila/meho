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

func TestPrintDetailNamesTheFlagWhenTheScriptHasMorePages(t *testing.T) {
	d := scriptRunView(t, map[string]interface{}{
		"script":           "uname -a\n",
		"script_bytes":     float64(70000),
		"script_sha256":    testScriptSHA,
		"script_truncated": true,
		"script_pages":     float64(2),
	})
	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	want := "The script has 2 pages. Read page N with: meho approvals show " + d.Id.String() + " --script-page N\n"
	if !strings.Contains(stdout.String(), want) {
		t.Errorf("printDetail output misses %q in:\n%s", want, stdout.String())
	}

	one := scriptRunView(t, map[string]interface{}{
		"script":       "uname -a\n",
		"script_bytes": float64(9),
		"script_pages": float64(1),
	})
	cmd, stdout, _ = newCapturingCmd(t)
	printDetail(cmd, &one)
	if strings.Contains(stdout.String(), "--script-page") {
		t.Errorf("a one-page script names the flag:\n%s", stdout.String())
	}
}

func TestWithScriptPagePrintsPageNOfM(t *testing.T) {
	d := scriptRunView(t, map[string]interface{}{
		"interpreter":      "/bin/bash",
		"script":           "page one\n",
		"script_bytes":     float64(70000),
		"script_sha256":    testScriptSHA,
		"script_truncated": true,
		"script_pages":     float64(2),
		"script_ref":       "app-repo@abc123:check.sh",
	})
	text := "page two\n"
	sha := testScriptSHA
	page := &api.ApprovalScriptPageView{
		Page: 2, Pages: 2, Script: &text, ScriptBytes: 70000, ScriptSha256: &sha,
	}
	shown := withScriptPage(d.ProposedEffect, page)
	var buf bytes.Buffer
	printScript(&buf, "", shown)
	out := buf.String()
	for _, want := range []string{
		"  Page:             2 of 2\n",
		"  SHA-256:          " + testScriptSHA + "\n",
		"  Reviewed copy:    app-repo@abc123:check.sh\n",
		"  | page two\n",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printScript output misses %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, "page one") || strings.Contains(out, "longer than the text") {
		t.Errorf("page 2 shows page 1 or the cut note:\n%s", out)
	}
	// The caller's effect is not changed, and the flag hint is not printed for a page.
	if d.ProposedEffect["preview"].(map[string]interface{})["script"] != "page one\n" {
		t.Errorf("withScriptPage changed the proposed effect")
	}
	var hint bytes.Buffer
	printScriptPagesHint(&hint, "id", shown)
	if hint.Len() != 0 {
		t.Errorf("hint printed for a page: %q", hint.String())
	}
}

func TestWithScriptPageHiddenValuesShowNoChecksum(t *testing.T) {
	d := scriptRunView(t, map[string]interface{}{
		"script":       "page one\n",
		"script_bytes": float64(70000),
		"script_pages": float64(2),
	})
	text := "export DB_[REDACTED:api_key]\n"
	hidden := true
	page := &api.ApprovalScriptPageView{
		Page: 2, Pages: 2, Script: &text, ScriptBytes: 70000, ScriptValuesHidden: &hidden,
	}
	var buf bytes.Buffer
	printScript(&buf, "", withScriptPage(d.ProposedEffect, page))
	out := buf.String()
	if strings.Contains(out, "SHA-256:  ") || !strings.Contains(out, "MEHO shows no SHA-256") {
		t.Errorf("hidden values must show no checksum:\n%s", out)
	}
}

func TestRenderScriptPageErrorPrintsTheSentence(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
		want   string
	}{
		{410, `{"detail":"MEHO no longer keeps this script. It keeps the script only while the request waits for approval."}`, "MEHO no longer keeps this script"},
		{404, `{"detail":"The script has 2 page(s). Page 3 does not exist."}`, "Page 3 does not exist"},
		{404, `{"detail":"approval_request_not_found"}`, "approval_request_not_found"},
	} {
		cmd, _, stderr := newCapturingCmd(t)
		err := renderScriptPageError(cmd, "https://meho.example", &httpResponseError{statusCode: tc.status, body: []byte(tc.body)}, false)
		if err == nil || !strings.Contains(stderr.String(), tc.want) {
			t.Errorf("status %d: err=%v stderr=%q, want %q", tc.status, err, stderr.String(), tc.want)
		}
	}
}
