// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResolveProfile(t *testing.T) {
	cases := []struct {
		name        string
		flag        string
		flagChanged bool
		env         string
		want        string
		wantErr     string
	}{
		{name: "neither is default", want: DefaultProfile},
		{name: "env only", env: "approver", want: "approver"},
		{name: "flag only", flag: "initiator", flagChanged: true, want: "initiator"},
		{name: "flag beats env", flag: "initiator", flagChanged: true, env: "approver", want: "initiator"},
		{name: "explicit default", flag: "default", flagChanged: true, want: DefaultProfile},
		{name: "empty flag value is an error", flagChanged: true, wantErr: "--profile"},
		{name: "bad flag even with good env", flag: "a/b", flagChanged: true, env: "ok", wantErr: "--profile"},
		{name: "bad env", env: "../x", wantErr: ProfileEnv},
		{name: "path dots", env: "..", wantErr: ProfileEnv},
		{name: "too long", env: strings.Repeat("a", 65), wantErr: ProfileEnv},
		{name: "space", env: "a b", wantErr: ProfileEnv},
		{name: "hash", env: "a#b", wantErr: ProfileEnv},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ResolveProfile(tc.flag, tc.flagChanged, tc.env)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v; want it to mention %q", err, tc.wantErr)
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("got %q, %v; want %q", got, err, tc.want)
			}
		})
	}
}

func TestKeyForProfile(t *testing.T) {
	const url = "https://meho.example/"
	defSvc, defUser := KeyForBackplane(url)
	for _, p := range []string{"", DefaultProfile} {
		if svc, user := KeyForProfile(url, p); svc != defSvc || user != defUser {
			t.Errorf("profile %q key = (%q, %q); want the pre-profile key (%q, %q)", p, svc, user, defSvc, defUser)
		}
	}
	a, _ := KeyForProfile(url, "initiator")
	b, bUser := KeyForProfile(url, "approver")
	if a == b || a == defSvc || b == defSvc {
		t.Errorf("keys must differ: default=%q initiator=%q approver=%q", defSvc, a, b)
	}
	if ctxSvc, ctxUser := KeyForContext(WithProfile(context.Background(), "approver"), url); ctxSvc != b || ctxUser != bUser {
		t.Errorf("KeyForContext = (%q, %q); want (%q, %q)", ctxSvc, ctxUser, b, bUser)
	}
	if _, u := KeyForContext(context.Background(), url); u != defUser {
		t.Errorf("a context without a profile must use the default key; got %q", u)
	}
}

// A backplane URL can hold a query and a fragment. No (URL, profile)
// pair may share a slot with another pair, also when the URL itself
// ends in "#profile=<name>".
func TestKeyForProfileWithQueryOrFragmentInTheURL(t *testing.T) {
	pairs := [][2][2]string{
		{{"https://h?x#profile=a", DefaultProfile}, {"https://h?x", "a"}},
		{{"https://h#profile=a", DefaultProfile}, {"https://h", "a"}},
		{{"https://h?x#profile=a", "b"}, {"https://h?x", "a"}},
	}
	for _, p := range pairs {
		s1, u1 := KeyForProfile(p[0][0], p[0][1])
		s2, u2 := KeyForProfile(p[1][0], p[1][1])
		if s1 == s2 && u1 == u2 {
			t.Errorf("%v and %v share the slot (%q, %q)", p[0], p[1], s1, u1)
		}
	}
}

func TestProfilesHaveSeparateSlotsInOneFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "meho", "credentials.json")
	store := NewFileStoreAt(path)
	const url = "https://meho.example"

	for profile, token := range map[string]string{"default": "tok-default", "initiator": "tok-init", "approver": "tok-appr"} {
		svc, user := KeyForProfile(url, profile)
		if err := store.Save(svc, user, StoredToken{AccessToken: token}); err != nil {
			t.Fatalf("save %s: %v", profile, err)
		}
	}
	for profile, want := range map[string]string{"default": "tok-default", "initiator": "tok-init", "approver": "tok-appr"} {
		svc, user := KeyForProfile(url, profile)
		got, err := store.Load(svc, user)
		if err != nil || got.AccessToken != want {
			t.Errorf("profile %s: got %q, %v; want %q", profile, got.AccessToken, err, want)
		}
	}

	// A profile that never logged in is not found: it must not fall
	// back to another profile's token.
	svc, user := KeyForProfile(url, "other")
	if _, err := store.Load(svc, user); !errors.Is(err, ErrTokenNotFound) {
		t.Errorf("unknown profile err = %v; want ErrTokenNotFound", err)
	}

	// Deleting one slot leaves the others.
	svc, user = KeyForProfile(url, "initiator")
	if err := store.Delete(svc, user); err != nil {
		t.Fatal(err)
	}
	svc, user = KeyForProfile(url, "approver")
	if _, err := store.Load(svc, user); err != nil {
		t.Errorf("approver lost after deleting initiator: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Errorf("credentials file mode = %o; want 0600", info.Mode().Perm())
	}
}
