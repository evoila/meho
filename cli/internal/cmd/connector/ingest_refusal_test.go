// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// The answer the backplane sends when an ingest label is refused: a 422
// whose `detail` is an object (#4728). It is a saved copy of the real shape.
const uncoveredVersionAnswer = `{"detail":{"kind":"uncovered_version_label","product":"vmware",` +
	`"version":"7.0","impl_id":"vmware-rest","registered_classes":[{"class_name":"VmwareRestConnector",` +
	`"version":"9.0","impl_id":"vmware-rest","supported_version_range":">=8.5,<10.0"}],` +
	`"message":"version='7.0' is not covered by VmwareRestConnector (>=8.5,<10.0)"}}`

func TestIngestObjectDetail422IsReadAndPrinted(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(uncoveredVersionAnswer))
	}))
	defer srv.Close()

	client, err := api.NewClientWithResponses(srv.URL)
	if err != nil {
		t.Fatalf("client: %v", err)
	}
	resp, err := client.IngestEndpointApiV1ConnectorsIngestPostWithResponse(
		context.Background(),
		&api.IngestEndpointApiV1ConnectorsIngestPostParams{},
		api.IngestRequest{},
	)
	if err != nil {
		t.Fatalf("the generated parser must read an object detail; got %v", err)
	}
	if resp.StatusCode() != http.StatusUnprocessableEntity {
		t.Fatalf("status: got %d", resp.StatusCode())
	}

	cmd, _, errBuf := newPreferTestCmd()
	if err := renderHTTPStatus(cmd, srv.URL, resp.StatusCode(), resp.Body, false); err == nil {
		t.Fatal("expected a non-nil error")
	}
	stderr := errBuf.String()
	if !strings.Contains(stderr, "uncovered_version_label: version='7.0' is not covered") {
		t.Errorf("stderr misses the error name and message: %q", stderr)
	}
	if strings.Contains(stderr, "registered_classes") {
		t.Errorf("stderr should show the message, not the raw body: %q", stderr)
	}
}

func TestRenderHTTPStatus422ListKeepsRawBody(t *testing.T) {
	cmd, _, errBuf := newPreferTestCmd()
	body := []byte(`{"detail":[{"loc":["body","version"],"msg":"field required","type":"missing"}]}`)
	_ = renderHTTPStatus(cmd, "https://backplane.example", http.StatusUnprocessableEntity, body, false)
	if !strings.Contains(errBuf.String(), `"loc":["body","version"]`) {
		t.Errorf("a field-level list should keep the field names: %q", errBuf.String())
	}
}
