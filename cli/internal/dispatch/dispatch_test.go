// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// capturingConn returns a Connector whose Request records the last
// marshalled body and replies with a fixed response.
func capturingConn(id string, reply []byte, replyErr error) (*Connector, *[]byte) {
	var last []byte
	c := Connector{
		ID: id,
		Request: func(_ context.Context, _, _, _ string, body []byte) ([]byte, error) {
			last = body
			return reply, replyErr
		},
	}
	return &c, &last
}

func TestCallBakesConnectorIDAndTarget(t *testing.T) {
	c, last := capturingConn("vault-1.x", []byte(`{"status":"ok","op_id":"x","duration_ms":1}`), nil)
	if _, err := c.Call(context.Background(), "https://bp", "vault.kv.read", "rdc-vault", map[string]any{"path": "p"}); err != nil {
		t.Fatalf("Call: %v", err)
	}
	var body CallRequestBody
	if err := json.Unmarshal(*last, &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body.ConnectorID != "vault-1.x" || body.OpID != "vault.kv.read" {
		t.Fatalf("connector/op not baked: %+v", body)
	}
	if body.Target["name"] != "rdc-vault" {
		t.Fatalf("target name = %v, want rdc-vault", body.Target["name"])
	}
	if body.Params["path"] != "p" {
		t.Fatalf("params not threaded: %+v", body.Params)
	}
}

func TestCallEmptyTargetSerialisesNull(t *testing.T) {
	c, last := capturingConn("c", []byte(`{"status":"ok"}`), nil)
	if _, err := c.Call(context.Background(), "https://bp", "op", "", nil); err != nil {
		t.Fatalf("Call: %v", err)
	}
	if got := string(*last); !strings.Contains(got, `"target":null`) {
		t.Fatalf("empty target should serialise null, got %s", got)
	}
}

func TestCallWithTargetThreadsExtraKeys(t *testing.T) {
	c, last := capturingConn("vcfa-rest-9.0", []byte(`{"status":"ok"}`), nil)
	target := map[string]any{"name": "vcfa", "fqdn": "vra.lab"}
	if _, err := c.CallWithTarget(context.Background(), "https://bp", "op", target, nil); err != nil {
		t.Fatalf("CallWithTarget: %v", err)
	}
	var body CallRequestBody
	_ = json.Unmarshal(*last, &body)
	if body.Target["fqdn"] != "vra.lab" {
		t.Fatalf("fqdn not threaded into target: %+v", body.Target)
	}
}

func TestRenderStatusMapping(t *testing.T) {
	c := Connector{ID: "c"}
	cases := []struct {
		status  string
		wantErr error
	}{
		{"ok", nil},
		{"error", ErrOpError},
		{"denied", ErrOpError},
	}
	for _, tc := range cases {
		cmd := &cobra.Command{}
		cmd.SetOut(&bytes.Buffer{})
		cmd.SetErr(&bytes.Buffer{})
		r := &CallResult{Status: tc.status, OpID: "op"}
		err := c.Render(cmd, "op", r, false, nil)
		if !errors.Is(err, tc.wantErr) {
			t.Fatalf("status %q: err = %v, want %v", tc.status, err, tc.wantErr)
		}
	}
}

func TestRenderInvalidStatusReturnsRenderError(t *testing.T) {
	c := Connector{ID: "c"}
	cmd := &cobra.Command{}
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	err := c.Render(cmd, "op", &CallResult{Status: "weird"}, false, nil)
	if err == nil || errors.Is(err, ErrOpError) {
		t.Fatalf("invalid status should yield a non-ErrOpError render error, got %v", err)
	}
}

func TestPrintGenericSuccessRendersResult(t *testing.T) {
	c := Connector{ID: "vault-1.x"}
	var buf bytes.Buffer
	c.PrintGeneric(&buf, "vault.kv.read", &CallResult{
		Status: "ok", Result: json.RawMessage(`{"k":"v"}`), DurationMs: 12,
	})
	out := buf.String()
	if !strings.Contains(out, "vault-1.x vault.kv.read") || !strings.Contains(out, `"k": "v"`) {
		t.Fatalf("unexpected generic render:\n%s", out)
	}
}

func TestPrettyJSON(t *testing.T) {
	got, err := PrettyJSON(json.RawMessage(`{"b":1,"a":2}`))
	if err != nil {
		t.Fatalf("PrettyJSON: %v", err)
	}
	if !strings.Contains(got, "\n  ") {
		t.Fatalf("expected 2-space indent, got %q", got)
	}
	if _, err := PrettyJSON(json.RawMessage(`{bad`)); err == nil {
		t.Fatalf("expected error on malformed JSON")
	}
}
