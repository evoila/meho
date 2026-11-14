// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

type workRefCase struct {
	Name               string `json:"name"`
	Raw                string `json:"raw"`
	Repeat             int    `json:"repeat"`
	Clean              string `json:"clean"`
	CleanRepeat        int    `json:"clean_repeat"`
	Rule               string `json:"rule"`
	LengthDependsOnNFC bool   `json:"length_depends_on_nfc"`
	CLIRefuses         bool   `json:"cli_refuses"`
}

func (c workRefCase) raw() string {
	if c.Repeat > 0 {
		return strings.Repeat(c.Raw, c.Repeat)
	}
	return c.Raw
}

func (c workRefCase) clean() string {
	if c.CleanRepeat > 0 {
		return strings.Repeat(c.Clean, c.CleanRepeat)
	}
	return c.Clean
}

// loadWorkRefCases reads the case list shared with the backend test.
func loadWorkRefCases(t *testing.T) (valid, invalid []workRefCase) {
	t.Helper()
	data, err := os.ReadFile("../../../testdata/work_ref_cases.json")
	if err != nil {
		t.Fatalf("read shared cases: %v", err)
	}
	var doc struct {
		Valid   []workRefCase `json:"valid"`
		Invalid []workRefCase `json:"invalid"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatalf("parse shared cases: %v", err)
	}
	if len(doc.Valid) == 0 || len(doc.Invalid) == 0 {
		t.Fatal("the shared case list is empty")
	}
	return doc.Valid, doc.Invalid
}

// TestValidWorkRef_SharedCases runs the list the backend test runs too.
// The CLI does not make the text NFC, so a case whose length depends on
// that step is left to the backend.
func TestValidWorkRef_SharedCases(t *testing.T) {
	valid, invalid := loadWorkRefCases(t)
	for _, c := range valid {
		if c.LengthDependsOnNFC {
			continue
		}
		if got := ValidWorkRef(c.raw()); got == c.CLIRefuses {
			t.Errorf("%s: ValidWorkRef(%q) = %v; want %v", c.Name, c.raw(), got, !c.CLIRefuses)
		}
		// Only the case that needs the NFC step differs from CleanWorkRef.
		if c.Name != "decomposed text is made composed" && CleanWorkRef(c.raw()) != c.clean() {
			t.Errorf("%s: CleanWorkRef(%q) = %q; want %q", c.Name, c.raw(), CleanWorkRef(c.raw()), c.clean())
		}
	}
	for _, c := range invalid {
		if ValidWorkRef(c.raw()) {
			t.Errorf("%s: ValidWorkRef(%q) = true; want false", c.Name, c.raw())
		}
	}
}

// TestValidWorkRef_InvalidUTF8 covers what a JSON file cannot hold: bytes
// that are not UTF-8 text, which the header cannot carry.
func TestValidWorkRef_InvalidUTF8(t *testing.T) {
	for _, ref := range []string{"CHG\xff1", "\xc3", "\xed\xa0\x80CHG"} {
		if ValidWorkRef(ref) {
			t.Errorf("ValidWorkRef(%q) = true; want false", ref)
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
