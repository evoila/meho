// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"errors"
	"testing"
)

// fakeHome returns a HomeDirFunc that always yields the given path.
func fakeHome(path string) HomeDirFunc {
	return func() (string, error) { return path, nil }
}

// fakeHomeErr returns a HomeDirFunc that always returns an error.
func fakeHomeErr() HomeDirFunc {
	return func() (string, error) { return "", errors.New("no home") }
}

// hasCategory reports whether any match in r has the given category.
func hasCategory(r MachineLocalResult, cat string) bool {
	for _, m := range r.Matches {
		if m.category() == cat {
			return true
		}
	}
	return false
}

// Helper so tests can read Category without exporting more types.
func (m MachineLocalMatch) category() string { return m.Category }

// ── unix-home ─────────────────────────────────────────────────────────────────

func TestUnixHome(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		body  string
		match bool
	}{
		{name: "users_alice_hit", body: "scripts live at /Users/alice/x", match: true},
		{name: "home_bob_hit", body: "config at /home/bob/config.yml", match: true},
		{name: "no_slash_miss", body: "/Usersalice/foo", match: false},
		{name: "users_no_trailing_miss", body: "/Users/alice", match: false},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := DetectMachineLocal(tc.body, fakeHome("/unrelated/path"))
			got := hasCategory(r, "unix-home")
			if got != tc.match {
				t.Errorf("unix-home: body=%q got=%v want=%v", tc.body, got, tc.match)
			}
		})
	}
}

// ── windows-home ──────────────────────────────────────────────────────────────

func TestWindowsHome(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		body  string
		match bool
	}{
		{name: "c_users_bob_hit", body: `path is C:\Users\bob\`, match: true},
		{name: "d_drive_hit", body: `D:\Users\carol\documents`, match: true},
		{name: "no_trailing_miss", body: `C:\Users\bob`, match: false},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := DetectMachineLocal(tc.body, fakeHome("/unrelated/path"))
			got := hasCategory(r, "windows-home")
			if got != tc.match {
				t.Errorf("windows-home: body=%q got=%v want=%v", tc.body, got, tc.match)
			}
		})
	}
}

// ── tilde-home ────────────────────────────────────────────────────────────────

func TestTildeHome(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		body  string
		match bool
	}{
		{name: "line_start_hit", body: "~/foo is the path", match: true},
		{name: "newline_preceded_hit", body: "path:\n~/bar/x", match: true},
		{name: "quote_preceded_hit", body: `open "~/baz"`, match: true},
		// "approx ~/3 weeks" mid-prose should not flag — space before ~/
		// is intentionally excluded to avoid false positives from
		// proximity markers.
		{name: "prose_approx_miss", body: "done in approx ~/3 weeks", match: false},
		// Plain space before ~/ mid-sentence: not a path context.
		{name: "space_preceded_miss", body: "check ~/bar should not flag", match: false},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := DetectMachineLocal(tc.body, fakeHome("/unrelated/path"))
			got := hasCategory(r, "tilde-home")
			if got != tc.match {
				t.Errorf("tilde-home: body=%q got=%v want=%v", tc.body, got, tc.match)
			}
		})
	}
}

// ── local-hostname ────────────────────────────────────────────────────────────

func TestLocalHostname(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		body  string
		match bool
	}{
		{name: "dot_local_hit", body: "connect to db.local:5432", match: true},
		{name: "dot_lan_hit", body: "server at box.lan", match: true},
		{name: "localhost_hit", body: "redis://localhost:6379", match: true},
		{name: "host_docker_internal_hit", body: "api at host.docker.internal:8080", match: true},
		// "mylocaldb" does not match because .local/.lan require a dot before the suffix.
		{name: "mylocaldb_miss", body: "database name is mylocaldb", match: false},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := DetectMachineLocal(tc.body, fakeHome("/unrelated/path"))
			got := hasCategory(r, "local-hostname")
			if got != tc.match {
				t.Errorf("local-hostname: body=%q got=%v want=%v", tc.body, got, tc.match)
			}
		})
	}
}

// ── operator-username ─────────────────────────────────────────────────────────

func TestOperatorUsername(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		body   string
		homeFn HomeDirFunc
		match  bool
	}{
		{
			name:   "three_occurrences_hit",
			body:   "alice ran alice then alice again",
			homeFn: fakeHome("/Users/alice"),
			match:  true,
		},
		{
			name:   "two_occurrences_miss",
			body:   "alice ran alice",
			homeFn: fakeHome("/Users/alice"),
			match:  false,
		},
		{
			name:   "home_dir_error_miss",
			body:   "alice alice alice",
			homeFn: fakeHomeErr(),
			match:  false,
		},
		{
			name:   "nil_homefn_uses_real_os_no_panic",
			body:   "no username repeated here at all",
			homeFn: nil, // nil triggers os.UserHomeDir — must not panic
			match:  false,
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := DetectMachineLocal(tc.body, tc.homeFn)
			got := hasCategory(r, "operator-username")
			if got != tc.match {
				t.Errorf("operator-username: body=%q got=%v want=%v", tc.body, got, tc.match)
			}
		})
	}
}

// ── sample truncation ─────────────────────────────────────────────────────────

func TestSampleTruncation(t *testing.T) {
	t.Parallel()
	// Build a body containing a unix-home hit with a long path segment.
	longSeg := "aaaaabbbbbaaaaabbbbbaaaaaббббббббаааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааааа"
	body := "/Users/" + longSeg + "/"
	r := DetectMachineLocal(body, fakeHome("/unrelated/path"))
	if !r.Flagged {
		t.Fatal("expected Flagged=true for long unix-home path")
	}
	for _, m := range r.Matches {
		if len([]rune(m.Sample)) > 80 {
			t.Errorf("Sample exceeds 80 runes: %d runes in %q", len([]rune(m.Sample)), m.Sample)
		}
		if m.Sample == "" {
			t.Error("Sample must be non-empty for a hit")
		}
	}
}

// ── flagged iff matches ────────────────────────────────────────────────────────

func TestFlaggedConsistency(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		body string
	}{
		{name: "clean_body", body: "this is a clean memory entry about architecture"},
		{name: "hit_body", body: "stores results in /Users/alice/data/"},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := DetectMachineLocal(tc.body, fakeHome("/unrelated/path"))
			if r.Flagged != (len(r.Matches) > 0) {
				t.Errorf("Flagged=%v but len(Matches)=%d", r.Flagged, len(r.Matches))
			}
		})
	}
}
