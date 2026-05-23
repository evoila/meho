// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package backplane

import (
	"errors"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
)

func TestNormaliseURLTrimsAndValidates(t *testing.T) {
	got, err := NormaliseURL("https://meho.test/")
	if err != nil || got != "https://meho.test" {
		t.Fatalf("NormaliseURL: got %q, err %v", got, err)
	}
	if _, err := NormaliseURL("   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("blank URL should error 'empty', got %v", err)
	}
	if _, err := NormaliseURL("notaurl"); err == nil {
		t.Fatalf("malformed URL should error")
	}
}

func TestResolveOverrideWins(t *testing.T) {
	got, err := Resolve("https://override.test/")
	if err != nil || got != "https://override.test" {
		t.Fatalf("Resolve(override): got %q, err %v", got, err)
	}
}

func TestClassifyError(t *testing.T) {
	notConfigured := &NotConfiguredError{Inner: auth.ErrConfigNotFound}
	if se := ClassifyError(notConfigured); se == nil || se.Code != "auth_expired" {
		t.Fatalf("not-configured should classify as auth_expired, got %+v", se)
	}
	if se := ClassifyError(errors.New("parse boom")); se == nil || se.Code != "unexpected_response" {
		t.Fatalf("arbitrary error should classify as unexpected_response, got %+v", se)
	}
}

func TestNotConfiguredErrorUnwrapsToConfigNotFound(t *testing.T) {
	err := &NotConfiguredError{Inner: auth.ErrConfigNotFound}
	if !errors.Is(err, auth.ErrConfigNotFound) {
		t.Fatalf("NotConfiguredError should unwrap to auth.ErrConfigNotFound")
	}
	if !strings.Contains(err.Error(), "meho login") {
		t.Fatalf("error message should hint `meho login`, got %q", err.Error())
	}
}
