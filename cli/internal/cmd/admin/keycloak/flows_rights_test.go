// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func flowClient(t *testing.T, status int) *adminClient {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
	}))
	t.Cleanup(srv.Close)
	return &adminClient{httpClient: srv.Client(), baseURL: srv.URL, realm: "example", token: "t"}
}

// Keycloak answers 403 to a flow call: the error names the missing role.
func TestFlowCall_ForbiddenNamesTheMissingRole(t *testing.T) {
	c := flowClient(t, http.StatusForbidden)
	calls := map[string]func() error{
		"flowCall": func() error {
			_, err := c.flowCall(context.Background(), http.MethodPost, "authentication/flows", nil, http.StatusCreated)
			return err
		},
		"listFlows": func() error {
			_, err := c.listFlows(context.Background())
			return err
		},
	}
	for name, call := range calls {
		err := call()
		if err == nil || !strings.Contains(err.Error(), "manage-realm") {
			t.Fatalf("%s: error %v does not name manage-realm", name, err)
		}
		var apiErr *errKeycloakAPI
		if !errors.As(err, &apiErr) || apiErr.statusCode != http.StatusForbidden {
			t.Errorf("%s: the Keycloak error is lost: %v", name, err)
		}
	}
}

// Another failure keeps its own text and does not name a role.
func TestFlowCall_OtherStatusNamesNoRole(t *testing.T) {
	c := flowClient(t, http.StatusInternalServerError)
	_, err := c.flowCall(context.Background(), http.MethodGet, "authentication/flows", nil, http.StatusOK)
	if err == nil || strings.Contains(err.Error(), "manage-realm") {
		t.Fatalf("unexpected error: %v", err)
	}
}
