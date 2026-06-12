// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"errors"
	"fmt"
	"os"
	"regexp"

	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/api"
)

// slugPattern mirrors `backend/src/meho_backplane/kb/schemas.py`'s
// `SLUG_PATTERN` verbatim (reused by the runbooks schemas module per
// the backend module docstring). The CLI re-implements the regex
// inline so the pre-flight catches a bad slug without paying a network
// round-trip; the backend re-validates authoritatively at the wire so
// this duplication is a UX layer, not a security boundary. If the
// backend ever tightens the pattern, this regex must be updated in
// lock-step — there is no live config exchange.
var slugPattern = regexp.MustCompile(`^[a-z](?:[a-z0-9.\-]*[a-z0-9])?$`)

// stepIDPattern mirrors `backend/src/meho_backplane/runbooks/schemas.py`'s
// `STEP_ID_PATTERN` — tighter than the slug pattern (no dots) because
// step ids are short procedure handles authors type by hand
// (`revoke-old-cert`, `verify-cluster-quorum`).
var stepIDPattern = regexp.MustCompile(`^[a-z][a-z0-9\-]{0,63}$`)

// substitutionPattern matches every `${...}` occurrence. The inner
// group is captured non-greedily so adjacent substitutions in one
// string are matched independently. Mirrors `_SUBSTITUTION_PATTERN`
// in the backend's runbooks/schemas.py.
var substitutionPattern = regexp.MustCompile(`\$\{([^}]*)\}`)

// paramNamePattern mirrors `_PARAM_NAME_PATTERN` in the backend's
// runbooks/schemas.py — `run.params.X` where `X` is a single flat
// level (no dots inside), matching `[a-z_][a-z0-9_]*`. Nested paths
// like `${run.params.X.Y}` are rejected.
var paramNamePattern = regexp.MustCompile(`^run\.params\.[a-z_][a-z0-9_]*$`)

// yamlTemplateBody is the on-disk shape the CLI parses out of a
// `--from <file.yaml>` argument. Mirrors the backend's
// `RunbookTemplateBody` (and per-step models) one-to-one. The wire
// shape going to the backend is the generated `api.RunbookTemplateBody`
// (built by buildRunbookTemplateBody below); this intermediate shape
// exists so YAML field tags can drive `yaml.v3`'s decoder without
// disturbing the generated client's JSON tags.
//
// `yaml.v3` carries line / column info on every decode error and on
// every node (Node.Line, Node.Column); we don't introspect that here,
// the decoder's default error string ("yaml: line N: ...") is enough
// to point operators at the offending YAML line.
type yamlTemplateBody struct {
	Title       string     `yaml:"title"`
	Description string     `yaml:"description"`
	TargetKind  *string    `yaml:"target_kind,omitempty"`
	Steps       []yamlStep `yaml:"steps"`
}

// yamlStep is the per-step shape — a discriminated union on `type`
// matching the backend's `Step = OperationCallStep | ManualStep`.
// Fields irrelevant to the chosen `type` are silently allowed to be
// absent (the CLI's pre-flight cross-checks them) — yaml.v3 doesn't
// natively support discriminated unions, so the decode is permissive
// and the validator narrows.
//
// `OpID` and `Params` are only meaningful for `type=operation_call`
// steps. Pre-flight rejects `op_id`/`params` on a `type=manual` step
// rather than silently dropping them.
type yamlStep struct {
	ID     string                 `yaml:"id"`
	Title  string                 `yaml:"title"`
	Body   string                 `yaml:"body"`
	Type   string                 `yaml:"type"`
	OpID   string                 `yaml:"op_id,omitempty"`
	Params map[string]interface{} `yaml:"params,omitempty"`
	Verify yamlVerify             `yaml:"verify"`
}

// yamlVerify is the per-verify shape — a discriminated union on
// `type` matching the backend's `Verify = ConfirmVerify |
// OperationCallVerify`. Fields irrelevant to the chosen `type` are
// pre-flighted in validateYAMLTemplate.
type yamlVerify struct {
	Type   string                 `yaml:"type"`
	Prompt string                 `yaml:"prompt,omitempty"`
	OpID   string                 `yaml:"op_id,omitempty"`
	Params map[string]interface{} `yaml:"params,omitempty"`
	Expect map[string]interface{} `yaml:"expect,omitempty"`
}

// loadYAMLTemplate reads the file at path and unmarshals it as a
// runbook template body. The error message embeds the path so the
// operator sees which file failed when piping several `meho runbook
// draft-template` invocations through a shell loop.
//
// yaml.v3's decoder surfaces parse errors with a `line N: ` prefix;
// the path-on-disk is prepended so operators see both the file and
// the offending line. The default decoder is non-strict (unknown
// keys are silently ignored) — pre-flight is the strict layer, the
// decoder just turns bytes into the intermediate struct.
func loadYAMLTemplate(path string) (*yamlTemplateBody, error) {
	blob, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read --from file %q: %w", path, err)
	}
	var body yamlTemplateBody
	if err := yaml.Unmarshal(blob, &body); err != nil {
		// yaml.v3's error format already includes "line N:".
		return nil, fmt.Errorf("parse --from file %q: %w", path, err)
	}
	return &body, nil
}

// validateSlug applies the slug regex pre-flight. Returns an error
// suitable for surfacing under output.Unexpected (the operator-visible
// hint matches the backend's 422 detail string format).
func validateSlug(slug string) error {
	if !slugPattern.MatchString(slug) {
		return fmt.Errorf(
			"slug %q does not match %q -- must start with [a-z], "+
				"end with [a-z0-9], and contain only [a-z0-9.-] in between",
			slug, slugPattern.String(),
		)
	}
	return nil
}

// validateSubstitutions walks an arbitrary value recursively and
// rejects every `${...}` pattern except the two allowlisted forms
// (`${run.target}` and `${run.params.X}` with X = `[a-z_][a-z0-9_]*`).
// Pure 1:1 port of the backend's `validate_substitutions` (
// `backend/src/meho_backplane/runbooks/schemas.py:105`). Walks
// strings, dict keys + values, and list items; scalars carry no
// substitution surface so nothing else is traversed.
//
// The pre-flight runs over every step body, op-call params, verify
// params, and verify expect — same fields the backend's
// `_validate_step_ids_unique_and_substitutions_allowlisted` walks.
// Defense in depth: the backend re-walks at publish + advance time.
func validateSubstitutions(value interface{}) error {
	switch v := value.(type) {
	case string:
		for _, match := range substitutionPattern.FindAllStringSubmatch(v, -1) {
			inner := match[1]
			if inner == "run.target" {
				continue
			}
			if paramNamePattern.MatchString(inner) {
				continue
			}
			return fmt.Errorf("disallowed substitution pattern: %s", match[0])
		}
	case map[string]interface{}:
		for key, item := range v {
			if err := validateSubstitutions(key); err != nil {
				return err
			}
			if err := validateSubstitutions(item); err != nil {
				return err
			}
		}
	case []interface{}:
		for _, item := range v {
			if err := validateSubstitutions(item); err != nil {
				return err
			}
		}
	}
	// Other scalar types (int, float, bool, nil) carry no
	// substitution surface; nothing to walk.
	return nil
}

// validateYAMLTemplate enforces the template-level invariants the
// backend's model validator enforces:
//
//  1. Slug matches SLUG_PATTERN.
//  2. Step ids are unique within the template.
//  3. Each step id matches STEP_ID_PATTERN.
//  4. Each step `type` is `manual` or `operation_call`.
//  5. Each verify `type` is `confirm` or `operation_call`.
//  6. `op_id` is set when (and only when) `type=operation_call`
//     (mirrors the discriminated-union shape on both step + verify).
//  7. Every string in every step body / op-call params / verify
//     params / verify expect satisfies the substitution allowlist.
//
// The pre-flight is not a security boundary — the backend
// re-validates at the wire so any drift between this code and the
// backend's schemas.py is caught by a 422. The pre-flight value is
// strictly UX: a fast-fail loop on the operator's workstation
// instead of a network round-trip per typo.
//
// Returns nil on success; a wrapped error on the first failure so
// the operator gets one actionable problem at a time. The error
// strings mirror the backend's wording wherever possible so an
// operator reading both the CLI error and the backend's 422 detail
// sees consistent terminology.
func validateYAMLTemplate(slug string, body *yamlTemplateBody) error {
	if err := validateSlug(slug); err != nil {
		return err
	}
	if body == nil {
		return errors.New("template body is empty")
	}
	if body.Title == "" {
		return errors.New("template title is required")
	}
	if body.Description == "" {
		return errors.New("template description is required")
	}
	if len(body.Steps) == 0 {
		return errors.New("template must declare at least one step")
	}
	seen := make(map[string]bool, len(body.Steps))
	for i, step := range body.Steps {
		// Step id presence + grammar.
		if step.ID == "" {
			return fmt.Errorf("step %d: id is required", i+1)
		}
		if !stepIDPattern.MatchString(step.ID) {
			return fmt.Errorf(
				"step %d: id %q does not match %q -- "+
					"start with [a-z], remainder [a-z0-9-], max 64 chars",
				i+1, step.ID, stepIDPattern.String(),
			)
		}
		if seen[step.ID] {
			return fmt.Errorf("duplicate step id: %q", step.ID)
		}
		seen[step.ID] = true

		// Step-level required fields.
		if step.Title == "" {
			return fmt.Errorf("step %q: title is required", step.ID)
		}
		if step.Body == "" {
			return fmt.Errorf("step %q: body is required", step.ID)
		}

		// Step type discriminator + per-type fields.
		switch step.Type {
		case "manual":
			if step.OpID != "" || step.Params != nil {
				return fmt.Errorf(
					"step %q: type=manual must not carry op_id or params; "+
						"set type=operation_call to dispatch via the registry",
					step.ID,
				)
			}
		case "operation_call":
			if step.OpID == "" {
				return fmt.Errorf("step %q: type=operation_call requires op_id", step.ID)
			}
			if err := validateSubstitutions(step.Params); err != nil {
				return fmt.Errorf("step %q params: %w", step.ID, err)
			}
		case "":
			return fmt.Errorf("step %q: type is required (manual or operation_call)", step.ID)
		default:
			return fmt.Errorf(
				"step %q: unknown type %q (allowed: manual, operation_call)",
				step.ID, step.Type,
			)
		}

		// Verify discriminator + per-type fields.
		switch step.Verify.Type {
		case "confirm":
			if step.Verify.Prompt == "" {
				return fmt.Errorf("step %q: verify.type=confirm requires verify.prompt", step.ID)
			}
			if step.Verify.OpID != "" || step.Verify.Params != nil || step.Verify.Expect != nil {
				return fmt.Errorf(
					"step %q: verify.type=confirm must not carry op_id / params / expect",
					step.ID,
				)
			}
		case "operation_call":
			if step.Verify.OpID == "" {
				return fmt.Errorf("step %q: verify.type=operation_call requires verify.op_id", step.ID)
			}
			if step.Verify.Prompt != "" {
				return fmt.Errorf(
					"step %q: verify.type=operation_call must not carry verify.prompt",
					step.ID,
				)
			}
			if err := validateSubstitutions(step.Verify.Params); err != nil {
				return fmt.Errorf("step %q verify.params: %w", step.ID, err)
			}
			if err := validateSubstitutions(step.Verify.Expect); err != nil {
				return fmt.Errorf("step %q verify.expect: %w", step.ID, err)
			}
		case "":
			return fmt.Errorf("step %q: verify.type is required (confirm or operation_call)", step.ID)
		default:
			return fmt.Errorf(
				"step %q: unknown verify.type %q (allowed: confirm, operation_call)",
				step.ID, step.Verify.Type,
			)
		}

		// Substitution allowlist over the step body (manual + op-call
		// both carry a body that may include `${run.target}` /
		// `${run.params.X}` substitutions).
		if err := validateSubstitutions(step.Body); err != nil {
			return fmt.Errorf("step %q body: %w", step.ID, err)
		}
	}
	return nil
}

// buildRunbookTemplateBody converts the local YAML struct into the
// generated `api.RunbookTemplateBody` wire shape. The step union is
// built via the generated `FromManualStep` / `FromOperationCallStep`
// helpers; the verify union via `FromConfirmVerify` /
// `FromOperationCallVerify`.
//
// The pre-flight guarantees this function is called only on a body
// that passed validateYAMLTemplate, so the discriminator switch never
// needs a default branch error; the helper returns an error only when
// the generated union helpers themselves fail (a JSON marshal
// failure on a deeply weird params/expect blob would be the only
// trigger — practically unreachable for a YAML-parsed body, but
// surfaced cleanly rather than panicking).
func buildRunbookTemplateBody(body *yamlTemplateBody) (api.RunbookTemplateBody, error) {
	out := api.RunbookTemplateBody{
		Title:       body.Title,
		Description: body.Description,
		Steps:       make([]api.RunbookTemplateBody_Steps_Item, 0, len(body.Steps)),
	}
	if body.TargetKind != nil && *body.TargetKind != "" {
		tk := *body.TargetKind
		out.TargetKind = &tk
	}
	for i, step := range body.Steps {
		verifyUnion, err := buildVerifyUnion(step.Verify)
		if err != nil {
			return out, fmt.Errorf("step %q: %w", step.ID, err)
		}
		var item api.RunbookTemplateBody_Steps_Item
		switch step.Type {
		case "manual":
			manual := api.ManualStep{
				Id:     step.ID,
				Title:  step.Title,
				Body:   step.Body,
				Type:   "manual",
				Verify: verifyUnion.manual,
			}
			if err := item.FromManualStep(manual); err != nil {
				return out, fmt.Errorf("step %q: marshal manual step: %w", step.ID, err)
			}
		case "operation_call":
			// Default empty map when the YAML omitted params on an
			// operation_call step; the backend's OperationCallStep
			// requires `params: dict[str, object]` to be present
			// (empty dict is acceptable).
			params := step.Params
			if params == nil {
				params = map[string]interface{}{}
			}
			opCall := api.OperationCallStep{
				Id:     step.ID,
				Title:  step.Title,
				Body:   step.Body,
				Type:   "operation_call",
				OpId:   step.OpID,
				Params: params,
				Verify: verifyUnion.opCall,
			}
			if err := item.FromOperationCallStep(opCall); err != nil {
				return out, fmt.Errorf("step %q: marshal operation_call step: %w", step.ID, err)
			}
		default:
			return out, fmt.Errorf("step %d (id %q): unreachable step type %q after pre-flight",
				i+1, step.ID, step.Type)
		}
		out.Steps = append(out.Steps, item)
	}
	return out, nil
}

// verifyUnions carries both the manual-step and op-call-step verify
// union flavours. The generated client defines `ManualStep_Verify`
// and `OperationCallStep_Verify` as distinct types (even though they
// hold structurally identical unions), so buildVerifyUnion populates
// both flavours from one yamlVerify and the caller picks the one
// matching its step type.
type verifyUnions struct {
	manual api.ManualStep_Verify
	opCall api.OperationCallStep_Verify
}

// buildVerifyUnion converts a yamlVerify into both step-flavour
// verify unions. The discriminator is the yamlVerify.Type field,
// which has already been validated against the allowlist by
// validateYAMLTemplate.
func buildVerifyUnion(v yamlVerify) (verifyUnions, error) {
	out := verifyUnions{}
	switch v.Type {
	case "confirm":
		c := api.ConfirmVerify{Type: "confirm", Prompt: v.Prompt}
		if err := out.manual.FromConfirmVerify(c); err != nil {
			return out, fmt.Errorf("marshal confirm verify (manual): %w", err)
		}
		if err := out.opCall.FromConfirmVerify(c); err != nil {
			return out, fmt.Errorf("marshal confirm verify (operation_call): %w", err)
		}
	case "operation_call":
		params := v.Params
		if params == nil {
			params = map[string]interface{}{}
		}
		expect := v.Expect
		if expect == nil {
			expect = map[string]interface{}{}
		}
		c := api.OperationCallVerify{
			Type:   "operation_call",
			OpId:   v.OpID,
			Params: params,
			Expect: expect,
		}
		if err := out.manual.FromOperationCallVerify(c); err != nil {
			return out, fmt.Errorf("marshal operation_call verify (manual): %w", err)
		}
		if err := out.opCall.FromOperationCallVerify(c); err != nil {
			return out, fmt.Errorf("marshal operation_call verify (operation_call): %w", err)
		}
	default:
		return out, fmt.Errorf("unreachable verify type %q after pre-flight", v.Type)
	}
	return out, nil
}
