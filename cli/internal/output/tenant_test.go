// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

// TestRenderError_TenantNotChosen: a backplane tenant_not_chosen answer,
// in whatever detail the command built, becomes the plain message with
// the person's tenants; the exit code stays the command's (#5241).
func TestRenderError_TenantNotChosen(t *testing.T) {
	detail := `call https://meho.example.test: HTTP 400: {"detail":"tenant_not_chosen: this login is in 2 ` +
		`tenants (lab-a, lab-b). Choose one with ` + "`meho tenants use <name>`" + `, or send the Meho-Tenant header ` +
		`with a tenant name or ID."}`

	var human bytes.Buffer
	err := RenderError(&human, Unexpected(detail), false)
	want := "meho: tenant_not_chosen: You are in several tenants. Pick one with `meho tenants use <name>`. " +
		"Your tenants: lab-a, lab-b.\n"
	if human.String() != want {
		t.Errorf("human output = %q; want %q", human.String(), want)
	}
	if coder, ok := err.(ExitCoder); !ok || coder.ExitCode() != ExitUnexpected {
		t.Errorf("exit code changed: %v", err)
	}

	var js bytes.Buffer
	_ = RenderError(&js, InsufficientRole(detail), true)
	var env jsonError
	if jerr := json.Unmarshal(js.Bytes(), &env); jerr != nil {
		t.Fatalf("json: %v (%s)", jerr, js.String())
	}
	if env.Error != ErrCodeTenantNotChosen || env.ExitCode != ExitInsufficientRole ||
		!strings.HasPrefix(env.Detail, TenantNotChosenHint) {
		t.Errorf("json envelope = %+v", env)
	}

	var other bytes.Buffer
	_ = RenderError(&other, Unexpected("HTTP 500: boom"), false)
	if other.String() != "meho: unexpected_response: HTTP 500: boom\n" {
		t.Errorf("unrelated error was rewritten: %q", other.String())
	}
}
