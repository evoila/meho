package dashboard

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestPrintDashboardSummaryShowsFailedMail(t *testing.T) {
	d := fakeDashboardDetail(t)
	at := time.Date(2026, 11, 7, 9, 30, 0, 0, time.UTC)
	sent := false
	reason := "smtp_connect_error"
	d.LastMailAt = &at
	d.LastMailSent = &sent
	d.LastMailReason = &reason
	var buf bytes.Buffer
	printDashboardSummary(&buf, &d)
	out := buf.String()
	for _, want := range []string{"last_mail:", "not sent (smtp_connect_error)"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in summary; got %q", want, out)
		}
	}
}

func TestPrintDashboardSummaryShowsSentMail(t *testing.T) {
	d := fakeDashboardDetail(t)
	at := time.Date(2026, 11, 7, 9, 30, 0, 0, time.UTC)
	sent := true
	d.LastMailAt = &at
	d.LastMailSent = &sent
	var buf bytes.Buffer
	printDashboardSummary(&buf, &d)
	out := buf.String()
	if !strings.Contains(out, "last_mail:") || strings.Contains(out, "not sent") {
		t.Errorf("expected a sent result; got %q", out)
	}
}

func TestPrintDashboardSummaryOmitsMailBeforeFirstAttempt(t *testing.T) {
	d := fakeDashboardDetail(t)
	var buf bytes.Buffer
	printDashboardSummary(&buf, &d)
	if strings.Contains(buf.String(), "last_mail") {
		t.Errorf("no attempt yet, so no line; got %q", buf.String())
	}
}
