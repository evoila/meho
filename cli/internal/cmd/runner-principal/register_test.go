// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runnerprincipal

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

func TestPrintRegisteredSummaryShowsTheEnrollmentCode(t *testing.T) {
	var out bytes.Buffer
	printRegisteredSummary(&out, &api.RunnerPrincipalRegistered{
		Name:                    "site-a",
		KeycloakClientId:        "runner:site-a",
		EnrollmentCode:          "abc.def",
		EnrollmentCodeExpiresAt: time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC),
	})
	got := out.String()
	for _, want := range []string{"enrollment_code:     abc.def", "2026-11-23T12:00:00Z", "MEHO_RUNNER_ENROLLMENT_CODE"} {
		if !strings.Contains(got, want) {
			t.Errorf("output lacks %q:\n%s", want, got)
		}
	}
}
