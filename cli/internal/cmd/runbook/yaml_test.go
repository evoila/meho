// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// validYAML is the canonical valid template body — exercises both
// step types, both verify types, and a `${run.target}` substitution.
// Tests that need a specific failure mode mutate this baseline.
const validYAML = `title: Cert rotation — vCenter
description: Rotate the cert on a managed vCenter target.
target_kind: vmware-rest
steps:
  - id: revoke-old-cert
    title: Revoke the existing cert
    body: |
      Run revoke-cert ${run.target} against the vault PKI.
    type: manual
    verify:
      type: confirm
      prompt: Did the revoke succeed?
  - id: issue-new-cert
    title: Issue a new cert
    body: |
      Dispatching to the PKI engine on ${run.target}.
    type: operation_call
    op_id: vault.pki.issue
    params:
      common_name: ${run.params.cn}
    verify:
      type: operation_call
      op_id: vault.pki.cert
      params:
        serial: ${run.params.cn}
      expect:
        revoked: false
`

func writeYAML(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "template.yaml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write yaml: %v", err)
	}
	return path
}

// TestLoadYAMLTemplateHappyPath — parsing the canonical template
// returns every field intact and routes the step / verify types
// correctly.
func TestLoadYAMLTemplateHappyPath(t *testing.T) {
	body, err := loadYAMLTemplate(writeYAML(t, validYAML))
	if err != nil {
		t.Fatalf("loadYAMLTemplate: %v", err)
	}
	if body.Title != "Cert rotation — vCenter" {
		t.Errorf("title: %q", body.Title)
	}
	if body.TargetKind == nil || *body.TargetKind != "vmware-rest" {
		t.Errorf("target_kind: %+v", body.TargetKind)
	}
	if len(body.Steps) != 2 {
		t.Fatalf("expected 2 steps; got %d", len(body.Steps))
	}
	if body.Steps[0].Type != "manual" {
		t.Errorf("steps[0].type: %q", body.Steps[0].Type)
	}
	if body.Steps[1].Type != "operation_call" {
		t.Errorf("steps[1].type: %q", body.Steps[1].Type)
	}
	if body.Steps[0].Verify.Type != "confirm" {
		t.Errorf("steps[0].verify.type: %q", body.Steps[0].Verify.Type)
	}
	if body.Steps[1].Verify.Type != "operation_call" {
		t.Errorf("steps[1].verify.type: %q", body.Steps[1].Verify.Type)
	}
}

// TestLoadYAMLTemplateParseErrorIncludesLine — malformed YAML
// surfaces yaml.v3's `line N:` error format wrapped with the path.
// Test 9 (AC): YAML parse error → exit 1 with line:col of the
// offending field.
func TestLoadYAMLTemplateParseErrorIncludesLine(t *testing.T) {
	bad := "title: ok\ndescription: missing-colon-here\nsteps\n  - id: x\n"
	_, err := loadYAMLTemplate(writeYAML(t, bad))
	if err == nil {
		t.Fatal("expected YAML parse error")
	}
	if !strings.Contains(err.Error(), "line") {
		t.Errorf("expected line: hint in error; got %q", err.Error())
	}
}

// TestLoadYAMLTemplateMissingFile — missing path surfaces a clear
// filesystem error rather than a panic.
func TestLoadYAMLTemplateMissingFile(t *testing.T) {
	_, err := loadYAMLTemplate("/no/such/path/template.yaml")
	if err == nil {
		t.Fatal("expected missing-file error")
	}
}

// TestValidateYAMLTemplateHappyPath — the canonical template passes
// pre-flight without errors.
func TestValidateYAMLTemplateHappyPath(t *testing.T) {
	body, err := loadYAMLTemplate(writeYAML(t, validYAML))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if err := validateYAMLTemplate("vcenter-cert-rotation", body); err != nil {
		t.Errorf("validateYAMLTemplate: %v", err)
	}
}

// TestValidateYAMLTemplateRejectsBadSlug — slug regex pre-flight
// (AC test 6).
func TestValidateYAMLTemplateRejectsBadSlug(t *testing.T) {
	body, _ := loadYAMLTemplate(writeYAML(t, validYAML))
	err := validateYAMLTemplate("BAD_SLUG", body)
	if err == nil {
		t.Fatal("expected slug-regex error")
	}
	if !strings.Contains(err.Error(), "does not match") {
		t.Errorf("expected regex hint; got %q", err.Error())
	}
}

// TestValidateYAMLTemplateRejectsDuplicateStepID — duplicate step
// ids fail pre-flight (AC test 7).
func TestValidateYAMLTemplateRejectsDuplicateStepID(t *testing.T) {
	dup := strings.Replace(validYAML, "issue-new-cert", "revoke-old-cert", 1)
	body, _ := loadYAMLTemplate(writeYAML(t, dup))
	err := validateYAMLTemplate("vcenter-cert-rotation", body)
	if err == nil {
		t.Fatal("expected duplicate step id error")
	}
	if !strings.Contains(err.Error(), "duplicate step id") {
		t.Errorf("expected dup-id message; got %q", err.Error())
	}
}

// TestValidateYAMLTemplateRejectsBadStepID — step id grammar
// (lowercase + digit + hyphen, no dots, ≤64 chars).
func TestValidateYAMLTemplateRejectsBadStepID(t *testing.T) {
	bad := strings.Replace(validYAML, "id: revoke-old-cert", "id: Revoke_old.Cert", 1)
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("vcenter-cert-rotation", body)
	if err == nil {
		t.Fatal("expected step-id grammar error")
	}
}

// TestValidateYAMLTemplateRejectsBadStepType — `type` outside the
// allowlist fails pre-flight.
func TestValidateYAMLTemplateRejectsBadStepType(t *testing.T) {
	bad := strings.Replace(validYAML, "type: manual", "type: scripted", 1)
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("vcenter-cert-rotation", body)
	if err == nil {
		t.Fatal("expected step-type error")
	}
	if !strings.Contains(err.Error(), "unknown type") {
		t.Errorf("expected unknown-type message; got %q", err.Error())
	}
}

// TestValidateYAMLTemplateRejectsBadVerifyType — verify.type outside
// the allowlist fails pre-flight.
func TestValidateYAMLTemplateRejectsBadVerifyType(t *testing.T) {
	bad := strings.Replace(validYAML, "type: confirm", "type: gut-feeling", 1)
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("vcenter-cert-rotation", body)
	if err == nil {
		t.Fatal("expected verify-type error")
	}
	if !strings.Contains(err.Error(), "unknown verify.type") {
		t.Errorf("expected unknown verify-type message; got %q", err.Error())
	}
}

// TestValidateYAMLTemplateRejectsBadSubstitution — disallowed
// `${...}` patterns fail pre-flight (AC test 8). The substitution
// allowlist mirrors the backend's `validate_substitutions`.
func TestValidateYAMLTemplateRejectsBadSubstitution(t *testing.T) {
	bad := strings.Replace(validYAML,
		"Run revoke-cert ${run.target}",
		"Run revoke-cert ${run.bad.path}", 1)
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("vcenter-cert-rotation", body)
	if err == nil {
		t.Fatal("expected disallowed-substitution error")
	}
	if !strings.Contains(err.Error(), "disallowed substitution") {
		t.Errorf("expected substitution hint; got %q", err.Error())
	}
}

// TestValidateYAMLTemplateAllowsRunTarget — `${run.target}` is in
// the allowlist; the canonical template uses it and must pass.
func TestValidateYAMLTemplateAllowsRunTarget(t *testing.T) {
	body, _ := loadYAMLTemplate(writeYAML(t, validYAML))
	if err := validateYAMLTemplate("vcenter-cert-rotation", body); err != nil {
		t.Errorf("validateYAMLTemplate (allow run.target): %v", err)
	}
}

// TestValidateYAMLTemplateAllowsRunParams — `${run.params.X}` with
// `X = [a-z_][a-z0-9_]*` is in the allowlist.
func TestValidateYAMLTemplateAllowsRunParams(t *testing.T) {
	body, _ := loadYAMLTemplate(writeYAML(t, validYAML))
	if err := validateYAMLTemplate("vcenter-cert-rotation", body); err != nil {
		t.Errorf("validateYAMLTemplate (allow run.params.X): %v", err)
	}
}

// TestValidateYAMLTemplateRejectsNestedRunParams — `${run.params.X.Y}`
// is rejected (nested paths are not in the allowlist).
func TestValidateYAMLTemplateRejectsNestedRunParams(t *testing.T) {
	bad := strings.Replace(validYAML,
		"${run.params.cn}",
		"${run.params.cn.nested}", -1) //nolint:gocritic // exhaustive replacement intended
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("vcenter-cert-rotation", body)
	if err == nil {
		t.Fatal("expected nested-param error")
	}
}

// TestValidateYAMLTemplateRejectsEmptyTitle — title is required.
func TestValidateYAMLTemplateRejectsEmptyTitle(t *testing.T) {
	bad := strings.Replace(validYAML,
		"title: Cert rotation — vCenter",
		"title: \"\"", 1)
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("vcenter-cert-rotation", body)
	if err == nil {
		t.Fatal("expected title-required error")
	}
}

// TestValidateYAMLTemplateRejectsNoSteps — at least one step
// required.
func TestValidateYAMLTemplateRejectsNoSteps(t *testing.T) {
	body := &yamlTemplateBody{
		Title:       "Title",
		Description: "Desc",
		Steps:       nil,
	}
	err := validateYAMLTemplate("ok-slug", body)
	if err == nil {
		t.Fatal("expected no-steps error")
	}
}

// TestValidateYAMLTemplateManualStepMustNotCarryOpID — fail-fast on a
// manual step that smuggles op_id.
func TestValidateYAMLTemplateManualStepMustNotCarryOpID(t *testing.T) {
	bad := `title: T
description: D
steps:
  - id: x
    title: t
    body: b
    type: manual
    op_id: should.not.be.here
    verify:
      type: confirm
      prompt: ok?
`
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("ok-slug", body)
	if err == nil {
		t.Fatal("expected manual+op_id error")
	}
}

// TestValidateYAMLTemplateOpCallStepRequiresOpID — an operation_call
// step must carry op_id.
func TestValidateYAMLTemplateOpCallStepRequiresOpID(t *testing.T) {
	bad := `title: T
description: D
steps:
  - id: x
    title: t
    body: b
    type: operation_call
    verify:
      type: confirm
      prompt: ok?
`
	body, _ := loadYAMLTemplate(writeYAML(t, bad))
	err := validateYAMLTemplate("ok-slug", body)
	if err == nil {
		t.Fatal("expected op_id required error")
	}
}

// TestBuildRunbookTemplateBodyRoundtripsSteps — the local YAML body
// round-trips through buildRunbookTemplateBody into the generated
// wire shape preserving the discriminator + per-step fields.
func TestBuildRunbookTemplateBodyRoundtripsSteps(t *testing.T) {
	body, _ := loadYAMLTemplate(writeYAML(t, validYAML))
	if err := validateYAMLTemplate("vcenter-cert-rotation", body); err != nil {
		t.Fatalf("validate: %v", err)
	}
	wire, err := buildRunbookTemplateBody(body)
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if wire.Title != body.Title || wire.Description != body.Description {
		t.Errorf("title/description not threaded: %+v", wire)
	}
	if len(wire.Steps) != 2 {
		t.Fatalf("expected 2 wire steps; got %d", len(wire.Steps))
	}
	// Step 0 — discriminator round-trip via the generated union helper.
	d0, err := wire.Steps[0].Discriminator()
	if err != nil {
		t.Fatalf("step[0] discriminator: %v", err)
	}
	if d0 != "manual" {
		t.Errorf("step[0] discriminator: got %q; want manual", d0)
	}
	manual, err := wire.Steps[0].AsManualStep()
	if err != nil {
		t.Fatalf("as manual: %v", err)
	}
	if manual.Id != "revoke-old-cert" || manual.Type != "manual" {
		t.Errorf("manual step fields: %+v", manual)
	}
	// Verify step 1's op_id survived.
	opCall, err := wire.Steps[1].AsOperationCallStep()
	if err != nil {
		t.Fatalf("as op_call: %v", err)
	}
	if opCall.OpId != "vault.pki.issue" {
		t.Errorf("op_call.OpId: got %q", opCall.OpId)
	}
}

// TestValidateSubstitutionsAllowsScalarTypes — non-string, non-map,
// non-list values carry no substitution surface; the walker must
// silently accept them.
func TestValidateSubstitutionsAllowsScalarTypes(t *testing.T) {
	for _, v := range []interface{}{42, 3.14, true, nil} {
		if err := validateSubstitutions(v); err != nil {
			t.Errorf("scalar %v should pass: %v", v, err)
		}
	}
}

// TestValidateSubstitutionsWalksMapKeys — a substitution smuggled
// into a dict key is just as dangerous as one in a value (mirrors
// the backend's validate_substitutions).
func TestValidateSubstitutionsWalksMapKeys(t *testing.T) {
	v := map[string]interface{}{"${evil}": "ok"}
	if err := validateSubstitutions(v); err == nil {
		t.Error("expected dict-key substitution to be rejected")
	}
}
