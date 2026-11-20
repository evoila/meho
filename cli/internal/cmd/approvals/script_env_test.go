// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"strings"
	"testing"
)

// The approval card of linux.script.run carries each env value. "meho
// approvals show" prints it, with control characters as visible escapes.
func TestPrintDetailPrintsTheEnvValuesEscaped(t *testing.T) {
	d := scriptRunView(t, map[string]interface{}{
		"script":        "tar -cf /tmp/out.tar /etc/app\n",
		"script_bytes":  float64(30),
		"script_sha256": testScriptSHA,
		"env_var_names": []interface{}{"APP_MODE", "TAR_OPTIONS"},
		"env": map[string]interface{}{
			"TAR_OPTIONS": "x\x1b[2Ky\x7f",
			"APP_MODE":    "on",
		},
		"env_note": "The approver sees every env value. Put secrets in secret_env, not in env.",
	})
	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	out := stdout.String()

	assertNoRawControls(t, out)
	for _, want := range []string{
		`"TAR_OPTIONS": "x\u001b[2Ky\x7f"`,
		`"APP_MODE": "on"`,
		`"env_note": "The approver sees every env value. Put secrets in secret_env, not in env."`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printDetail output misses %q in:\n%s", want, out)
		}
	}
}
