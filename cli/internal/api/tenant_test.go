// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

// TestAuthedClient_SendsTenantHeaderFromContext checks the request
// editor (#5241): a context carrying a tenant adds Meho-Tenant; a plain
// context, or one where WithTenant cleared the tenant, adds nothing.
func TestAuthedClient_SendsTenantHeaderFromContext(t *testing.T) {
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get(TenantHeader))
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	store := &inMemoryStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "test-bearer-marker",
		Expiry:       time.Now().Add(time.Hour),
	})
	client, err := NewAuthedClient(context.Background(), srv.URL,
		AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}

	withB := WithTenant(context.Background(), "lab-b")
	for _, ctx := range []context.Context{withB, context.Background(), WithTenant(withB, "")} {
		if _, err := client.GetHealth(ctx); err != nil {
			t.Fatalf("GetHealth: %v", err)
		}
	}
	if len(seen) != 3 || seen[0] != "lab-b" || seen[1] != "" || seen[2] != "" {
		t.Errorf("headers seen = %q; want [lab-b, \"\", \"\"]", seen)
	}
}

func TestSetTenantHeader(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/feed", nil)
	SetTenantHeader(req)
	if got := req.Header.Get(TenantHeader); got != "" {
		t.Errorf("no tenant in context: header = %q; want none", got)
	}
	req = req.WithContext(WithTenant(req.Context(), "3f0c5a52-0000-4000-8000-000000000001"))
	SetTenantHeader(req)
	if got := req.Header.Get(TenantHeader); got != "3f0c5a52-0000-4000-8000-000000000001" {
		t.Errorf("header = %q; want the tenant ID", got)
	}
}
