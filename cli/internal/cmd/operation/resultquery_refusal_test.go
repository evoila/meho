// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// realClient is the generated client plus a no-op Refresh, so the test
// runs postResultQuery over real HTTP through the generated code.
type realClient struct {
	*api.ClientWithResponses
}

func (realClient) Refresh(context.Context) error { return nil }

// A 422 whose `detail` is an object (not the validation-error list the
// OpenAPI spec names) must reach the operator with its reason and message,
// not as a JSON decode error from the generated parser.
func TestPostResultQueryObjectDetail422IsReadable(t *testing.T) {
	const refusal = `{"detail":{"message":"result for handle h is 300000 serialized bytes, ` +
		`over the 262144-byte output budget. The first 12 rows fit: retry with limit=12.",` +
		`"reason":"output_too_large","delivery":"unavailable"}}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(refusal))
	}))
	defer srv.Close()
	gen, err := api.NewClientWithResponses(srv.URL)
	if err != nil {
		t.Fatalf("new client: %v", err)
	}

	_, err = postResultQuery(context.Background(), realClient{gen},
		makeHandleUUID(t, "11111111-1111-1111-1111-111111111111"), 0, 50, nil)

	var apiErr *apiResponseError
	if !errors.As(err, &apiErr) {
		t.Fatalf("want *apiResponseError, got %T: %v", err, err)
	}
	if apiErr.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", apiErr.StatusCode)
	}
	msg, ok := outputTooLargeMessage(err)
	if !ok || !strings.Contains(msg, "retry with limit=12") {
		t.Fatalf("output_too_large message not decoded: ok=%v msg=%q", ok, msg)
	}
}
