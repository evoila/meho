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

func TestValidWorkRef(t *testing.T) {
	for ref, want := range map[string]bool{
		"gh:evoila/meho#13":     true,
		"gh:owner/repo.name#1":  true,
		"":                      false,
		"JIRA-12":               false,
		"gh:evoila/meho":        false,
		"gh:evoila/meho#0":      false,
		"gh:evoila meho/x#1":    false,
		"gh:evoila/meho#13 x":   false,
		"github:evoila/meho#13": false,
	} {
		if got := ValidWorkRef(ref); got != want {
			t.Errorf("ValidWorkRef(%q) = %v; want %v", ref, got, want)
		}
	}
}

// TestAuthedClient_SendsWorkRefHeaderFromContext checks the request
// editor: a context carrying a work-ref adds Meho-Work-Ref, a plain
// context adds nothing.
func TestAuthedClient_SendsWorkRefHeaderFromContext(t *testing.T) {
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get(WorkRefHeader))
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

	if _, err := client.GetHealth(WithWorkRef(context.Background(), "gh:evoila/meho#7")); err != nil {
		t.Fatalf("GetHealth with work-ref: %v", err)
	}
	if _, err := client.GetHealth(context.Background()); err != nil {
		t.Fatalf("GetHealth without work-ref: %v", err)
	}
	if len(seen) != 2 || seen[0] != "gh:evoila/meho#7" || seen[1] != "" {
		t.Errorf("headers seen = %q; want [gh:evoila/meho#7, \"\"]", seen)
	}
}

func TestSetWorkRefHeader(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/targets", nil)
	SetWorkRefHeader(req)
	if got := req.Header.Get(WorkRefHeader); got != "" {
		t.Errorf("no work-ref in context: header = %q; want none", got)
	}
	req = req.WithContext(WithWorkRef(req.Context(), "gh:evoila/meho#9"))
	SetWorkRefHeader(req)
	if got := req.Header.Get(WorkRefHeader); got != "gh:evoila/meho#9" {
		t.Errorf("header = %q; want gh:evoila/meho#9", got)
	}
}
