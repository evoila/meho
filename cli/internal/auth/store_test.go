// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/zalando/go-keyring"
)

// TestFileStoreRoundTrip is the load-bearing happy-path for the
// file-fallback backend. It exercises Save → Load → Delete in a
// tmpdir so the test never touches the operator's real
// $XDG_CONFIG_HOME. Failures here mean a regression in the
// serialisation layer the CLI depends on when the OS keyring is
// unavailable (every CI run + every headless host).
func TestFileStoreRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "credentials.json")
	store := NewFileStoreAt(path)

	want := StoredToken{
		BackplaneURL: "https://meho.example.com",
		Issuer:       "https://kc.example.com/realms/meho",
		ClientID:     "meho-cli",
		AccessToken:  "access-token-value",
		RefreshToken: "refresh-token-value",
		IDToken:      "id-token-value",
		TokenType:    "Bearer",
		// Truncate to seconds so JSON RFC3339 round-trip is exact —
		// time.Time keeps monotonic clock data that serialisation
		// strips, which produces a spurious mismatch on Equal.
		Expiry: time.Now().UTC().Truncate(time.Second),
	}

	if err := store.Save(DefaultService, want.BackplaneURL, want); err != nil {
		t.Fatalf("save: %v", err)
	}

	got, err := store.Load(DefaultService, want.BackplaneURL)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.AccessToken != want.AccessToken {
		t.Errorf("access token: got %q, want %q", got.AccessToken, want.AccessToken)
	}
	if got.RefreshToken != want.RefreshToken {
		t.Errorf("refresh token: got %q, want %q", got.RefreshToken, want.RefreshToken)
	}
	if got.IDToken != want.IDToken {
		t.Errorf("id token: got %q, want %q", got.IDToken, want.IDToken)
	}
	if got.Issuer != want.Issuer {
		t.Errorf("issuer: got %q, want %q", got.Issuer, want.Issuer)
	}
	if got.ClientID != want.ClientID {
		t.Errorf("client id: got %q, want %q", got.ClientID, want.ClientID)
	}
	if !got.Expiry.Equal(want.Expiry) {
		t.Errorf("expiry: got %v, want %v", got.Expiry, want.Expiry)
	}

	if err := store.Delete(DefaultService, want.BackplaneURL); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := store.Load(DefaultService, want.BackplaneURL); !errors.Is(err, ErrTokenNotFound) {
		t.Fatalf("post-delete load: got %v, want ErrTokenNotFound", err)
	}
}

// TestFileStoreEnforcesZeroSixHundred locks the security property
// that the credentials file is created mode 0600 and the directory
// 0700 — anything looser would leak the access token to other UIDs
// on a shared host.
//
// Skipped on Windows because POSIX file modes don't map cleanly
// there; the file backend on Windows is documented as best-effort
// and operators are expected to rely on the OS keyring backend.
func TestFileStoreEnforcesZeroSixHundred(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX mode bits not enforced on Windows")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "nested", "credentials.json")
	store := NewFileStoreAt(path)

	if err := store.Save(DefaultService, "user", StoredToken{AccessToken: "secret"}); err != nil {
		t.Fatalf("save: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat file: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("file perms: got %o, want 600", perm)
	}

	dirInfo, err := os.Stat(filepath.Dir(path))
	if err != nil {
		t.Fatalf("stat dir: %v", err)
	}
	if perm := dirInfo.Mode().Perm(); perm != 0o700 {
		t.Errorf("dir perms: got %o, want 700", perm)
	}
}

// TestFileStoreLoadMissingReturnsSentinel confirms that "no file
// yet" maps to ErrTokenNotFound rather than a raw "no such file"
// error. This is the path every meho status invocation takes on a
// brand-new host before login has run; misclassifying it as a real
// error would produce confusing "have you tried logging in?" prompts
// that already happened.
func TestFileStoreLoadMissingReturnsSentinel(t *testing.T) {
	dir := t.TempDir()
	store := NewFileStoreAt(filepath.Join(dir, "credentials.json"))
	if _, err := store.Load(DefaultService, "anything"); !errors.Is(err, ErrTokenNotFound) {
		t.Fatalf("expected ErrTokenNotFound, got %v", err)
	}
}

// TestFileStoreLoadMalformedReturnsError defends against a truncated
// or hand-edited credentials file: load must surface a real error
// (not the sentinel) so the operator sees the corruption rather than
// silently logging in again over the rubble.
func TestFileStoreLoadMalformedReturnsError(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "credentials.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	store := NewFileStoreAt(path)
	_, err := store.Load(DefaultService, "user")
	if err == nil {
		t.Fatalf("expected parse error, got nil")
	}
	if errors.Is(err, ErrTokenNotFound) {
		t.Fatalf("malformed file shouldn't surface as ErrTokenNotFound: %v", err)
	}
}

// TestFileStoreSupportsMultipleEntries shows that the on-disk shape
// holds entries for several (service, user) pairs simultaneously —
// the seam every multi-backplane future-CLI release will use.
func TestFileStoreSupportsMultipleEntries(t *testing.T) {
	dir := t.TempDir()
	store := NewFileStoreAt(filepath.Join(dir, "credentials.json"))

	one := StoredToken{BackplaneURL: "https://a.example", AccessToken: "tok-a"}
	two := StoredToken{BackplaneURL: "https://b.example", AccessToken: "tok-b"}

	if err := store.Save(DefaultService, one.BackplaneURL, one); err != nil {
		t.Fatalf("save one: %v", err)
	}
	if err := store.Save(DefaultService, two.BackplaneURL, two); err != nil {
		t.Fatalf("save two: %v", err)
	}

	gotOne, err := store.Load(DefaultService, one.BackplaneURL)
	if err != nil {
		t.Fatalf("load one: %v", err)
	}
	gotTwo, err := store.Load(DefaultService, two.BackplaneURL)
	if err != nil {
		t.Fatalf("load two: %v", err)
	}
	if gotOne.AccessToken != "tok-a" || gotTwo.AccessToken != "tok-b" {
		t.Fatalf("entries cross-talked: one=%q two=%q", gotOne.AccessToken, gotTwo.AccessToken)
	}
}

// TestFileStoreSaveStampsSavedAt pins that a BARE fileStore.Save stamps
// saved_at (#3320). Before the stamp was hoisted into every concrete
// backend it lived only in fallbackStore.Save, so a login performed
// under MEHO_KEYRING_DISABLE=1 (bare fileStore) persisted a zero
// saved_at — which a later, stamped keyring entry could out-rank in the
// reconciliation tie-break even when the file token was the newer one.
func TestFileStoreSaveStampsSavedAt(t *testing.T) {
	dir := t.TempDir()
	store := NewFileStoreAt(filepath.Join(dir, "credentials.json"))

	before := time.Now().UTC().Add(-time.Second)
	// Save an entry that carries NO saved_at — the store must stamp it.
	if err := store.Save(DefaultService, "https://a.example", StoredToken{AccessToken: "tok"}); err != nil {
		t.Fatalf("save: %v", err)
	}
	after := time.Now().UTC().Add(time.Second)

	got, err := store.Load(DefaultService, "https://a.example")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.SavedAt.IsZero() {
		t.Fatal("bare fileStore.Save must stamp saved_at, got zero")
	}
	if got.SavedAt.Before(before) || got.SavedAt.After(after) {
		t.Errorf("saved_at %v not within [%v, %v]", got.SavedAt, before, after)
	}
}

// TestFileStoreDeleteAbsentIsNoop matches the documented
// idempotency contract: deleting a non-existent entry returns nil
// rather than a sentinel — callers don't have to special-case
// first-run.
func TestFileStoreDeleteAbsentIsNoop(t *testing.T) {
	dir := t.TempDir()
	store := NewFileStoreAt(filepath.Join(dir, "credentials.json"))
	if err := store.Delete(DefaultService, "never-saved"); err != nil {
		t.Fatalf("delete absent: %v", err)
	}
}

// TestFileStoreDescribeIncludesPath verifies the operator-facing
// label contains the path so a confused operator can find the file.
func TestFileStoreDescribeIncludesPath(t *testing.T) {
	path := "/tmp/meho-test/credentials.json"
	store := NewFileStoreAt(path)
	desc := store.Describe()
	if want := path; !contains(desc, want) {
		t.Errorf("describe should contain %q, got %q", want, desc)
	}
}

// TestKeyForBackplaneNormalisesTrailingSlash guarantees that
// `meho login https://x/` and `meho login https://x` collide on the
// same store key — otherwise an operator's second invocation would
// silently store a duplicate.
func TestKeyForBackplaneNormalisesTrailingSlash(t *testing.T) {
	_, userA := KeyForBackplane("https://meho.example.com")
	_, userB := KeyForBackplane("https://meho.example.com/")
	if userA != userB {
		t.Errorf("trailing slash should normalise: %q vs %q", userA, userB)
	}
}

// TestStoredTokenJSONShape pins the wire shape — adding or renaming
// JSON fields here is a forward-compat break for tokens persisted
// by earlier CLI versions. The test deliberately writes the exact
// keys; a rename in the struct without updating this test is the
// signal that you've broken the on-disk schema.
func TestStoredTokenJSONShape(t *testing.T) {
	tok := StoredToken{
		BackplaneURL: "https://x",
		Issuer:       "https://kc",
		ClientID:     "id",
		AccessToken:  "at",
		RefreshToken: "rt",
		IDToken:      "idt",
		TokenType:    "Bearer",
	}
	data, err := json.Marshal(tok)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, key := range []string{
		`"backplane_url"`,
		`"issuer"`,
		`"client_id"`,
		`"access_token"`,
		`"refresh_token"`,
		`"id_token"`,
		`"token_type"`,
	} {
		if !contains(string(data), key) {
			t.Errorf("JSON missing %s in %s", key, string(data))
		}
	}
}

// contains is a tiny strings.Contains shim kept inline so this test
// file stays free of the strings import (cuts down on the import
// block, no functional value otherwise).
func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

// fakeStore is a TokenStore double the fallback-store tests inject so
// they can drive Save/Load/Delete behaviour deterministically without
// depending on the real OS keyring. Captures the last Save payload
// and per-method call counts; reports whichever errors the test set.
//
// loadErr is honoured first if set, then a non-zero `last` is
// returned; when loadErr is nil and `last` is the zero StoredToken,
// Load returns ErrTokenNotFound — the sentinel the fallback wrapper
// keys off when bridging to the secondary.
type fakeStore struct {
	label       string
	saveErr     error
	loadErr     error
	deleteErr   error
	saveCalls   int
	loadCalls   int
	deleteCalls int
	last        StoredToken
	hasToken    bool
}

func (f *fakeStore) Save(_, _ string, tok StoredToken) error {
	f.saveCalls++
	if f.saveErr != nil {
		return f.saveErr
	}
	f.last = tok
	f.hasToken = true
	return nil
}

func (f *fakeStore) Load(_, _ string) (StoredToken, error) {
	f.loadCalls++
	if f.loadErr != nil {
		return StoredToken{}, f.loadErr
	}
	if !f.hasToken {
		return StoredToken{}, ErrTokenNotFound
	}
	return f.last, nil
}

func (f *fakeStore) Delete(_, _ string) error {
	f.deleteCalls++
	if f.deleteErr != nil {
		return f.deleteErr
	}
	// Actually evict so a subsequent Load reports ErrTokenNotFound —
	// the reconciliation tests assert an evicted keyring entry can no
	// longer shadow the file token.
	f.last = StoredToken{}
	f.hasToken = false
	return nil
}

func (f *fakeStore) Describe() string { return f.label }

// TestFallbackStoreSavesToPrimaryByDefault is the happy path: the
// primary store accepts the token and the secondary is never touched.
// Describe() must name the primary so the operator's success message
// is honest about which backend the token landed in.
func TestFallbackStoreSavesToPrimaryByDefault(t *testing.T) {
	primary := &fakeStore{label: "OS keyring"}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	store := newFallbackStore(primary, secondary)

	tok := StoredToken{AccessToken: "small"}
	if err := store.Save(DefaultService, "user", tok); err != nil {
		t.Fatalf("save: %v", err)
	}
	if primary.saveCalls != 1 {
		t.Errorf("primary save calls: got %d, want 1", primary.saveCalls)
	}
	if secondary.saveCalls != 0 {
		t.Errorf("secondary should not be touched on primary success; got %d calls", secondary.saveCalls)
	}
	if got := store.Describe(); got != "OS keyring" {
		t.Errorf("describe: got %q, want %q", got, "OS keyring")
	}
}

// TestFallbackStoreFallsBackOnSizeError is the load-bearing test for
// the G0.9.1-T14 fix: when the primary rejects the payload as too big
// (the macOS Keychain ~4 KiB cap surfaced via keyring.ErrSetDataTooBig),
// the wrapper must transparently write to the secondary and have
// Describe() report the secondary so the login command's success
// message names the file backend the operator can actually inspect.
func TestFallbackStoreFallsBackOnSizeError(t *testing.T) {
	primary := &fakeStore{label: "OS keyring", saveErr: keyring.ErrSetDataTooBig}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	store := newFallbackStore(primary, secondary)

	tok := StoredToken{AccessToken: "huge"}
	if err := store.Save(DefaultService, "user", tok); err != nil {
		t.Fatalf("save should succeed via fallback: %v", err)
	}
	if primary.saveCalls != 1 {
		t.Errorf("primary save calls: got %d, want 1", primary.saveCalls)
	}
	if secondary.saveCalls != 1 {
		t.Errorf("secondary save calls: got %d, want 1", secondary.saveCalls)
	}
	if secondary.last.AccessToken != "huge" {
		t.Errorf("secondary did not receive the token: %+v", secondary.last)
	}
	if got := store.Describe(); got != "credentials file at /tmp/x" {
		t.Errorf("describe after fallback should name the file backend: got %q", got)
	}
}

// TestFallbackStoreFallsBackOnWrappedSizeError defends against future
// keyring backends that wrap ErrSetDataTooBig (e.g. with %w via
// fmt.Errorf for additional context). The sentinel match must use
// errors.Is, not equality, so a wrapped sentinel still triggers the
// fallback. Today the macOS and Windows backends return the bare
// sentinel; pinning the wrapped behaviour here means a future
// upstream change won't silently regress the fix.
func TestFallbackStoreFallsBackOnWrappedSizeError(t *testing.T) {
	wrapped := fmt.Errorf("meho: keyring set: %w", keyring.ErrSetDataTooBig)
	primary := &fakeStore{label: "OS keyring", saveErr: wrapped}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	store := newFallbackStore(primary, secondary)

	if err := store.Save(DefaultService, "user", StoredToken{AccessToken: "huge"}); err != nil {
		t.Fatalf("save should succeed via fallback on wrapped sentinel: %v", err)
	}
	if secondary.saveCalls != 1 {
		t.Errorf("secondary save calls: got %d, want 1", secondary.saveCalls)
	}
}

// TestFallbackStoreSurfacesNonSizeErrors confirms the wrapper does NOT
// swallow unrelated keyring failures. A locked Keychain, an
// unreachable D-Bus session, a Wincred ACL denial — all of those must
// continue to surface to the operator so they understand the system
// is broken rather than silently landing tokens in the file backend
// when the keyring was the intended store. The acceptance criterion
// hangs on this: "fallback triggers on a size/too-big keyring error
// specifically [...], not on unrelated keyring failures (which should
// still surface)."
func TestFallbackStoreSurfacesNonSizeErrors(t *testing.T) {
	bespoke := errors.New("dbus: connection refused")
	primary := &fakeStore{label: "OS keyring", saveErr: bespoke}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	store := newFallbackStore(primary, secondary)

	err := store.Save(DefaultService, "user", StoredToken{AccessToken: "x"})
	if err == nil {
		t.Fatalf("expected primary error to propagate, got nil")
	}
	if !errors.Is(err, bespoke) {
		t.Errorf("expected original error to remain unwrappable; got: %v", err)
	}
	if secondary.saveCalls != 0 {
		t.Errorf("secondary must not be touched on non-size errors; got %d calls", secondary.saveCalls)
	}
}

// TestFallbackStoreSurfacesBothFailures covers the failure-of-failures
// case: the keyring rejected by size AND the file backend also
// failed. The operator needs both signals — the wrapper composes
// them so they can see which backend ultimately blocked persistence.
func TestFallbackStoreSurfacesBothFailures(t *testing.T) {
	diskErr := errors.New("permission denied")
	primary := &fakeStore{label: "OS keyring", saveErr: keyring.ErrSetDataTooBig}
	secondary := &fakeStore{label: "credentials file at /tmp/x", saveErr: diskErr}
	store := newFallbackStore(primary, secondary)

	err := store.Save(DefaultService, "user", StoredToken{AccessToken: "x"})
	if err == nil {
		t.Fatalf("expected combined error, got nil")
	}
	if !errors.Is(err, diskErr) {
		t.Errorf("expected file-store error to remain unwrappable; got: %v", err)
	}
}

// TestFallbackStoreLoadReturnsPrimaryWhenOnlyPrimaryHasEntry pins the
// reconciled contract (#3320): Load now reads BOTH backends so a fresher
// file entry can never be shadowed by a stale keyring one. When only the
// primary holds an entry it still wins, but the secondary IS consulted
// (that read is what makes the reconciliation possible). This replaces
// the old "stays on primary / never touches the file" contract, which
// was the exact behaviour that let the split-brain go undetected.
func TestFallbackStoreLoadReturnsPrimaryWhenOnlyPrimaryHasEntry(t *testing.T) {
	primary := &fakeStore{label: "OS keyring"}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	store := newFallbackStore(primary, secondary)

	// Seed the primary so its Load returns a token (not ErrTokenNotFound).
	if err := primary.Save(DefaultService, "user", StoredToken{AccessToken: "from-primary", RefreshToken: "r"}); err != nil {
		t.Fatalf("seed primary: %v", err)
	}
	primary.saveCalls = 0 // ignore seed call in assertions

	got, err := store.Load(DefaultService, "user")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.AccessToken != "from-primary" {
		t.Errorf("load returned wrong token: got %q, want %q", got.AccessToken, "from-primary")
	}
	if primary.loadCalls != 1 {
		t.Errorf("primary load calls: got %d, want 1", primary.loadCalls)
	}
	if secondary.loadCalls != 1 {
		t.Errorf("secondary must be consulted so a fresher file entry can't be shadowed; got %d Load calls, want 1", secondary.loadCalls)
	}
}

// TestFallbackStoreDeleteClearsBothBackends pins the reconciled Delete
// contract (#3320): Delete must clear the entry from BOTH backends so a
// copy left by an earlier size-fallback (or any other divergence) can't
// be stranded to shadow a later login. This replaces the old
// primary-only asymmetry, which was one of the ways the two stores could
// drift out of sync.
func TestFallbackStoreDeleteClearsBothBackends(t *testing.T) {
	primary := &fakeStore{label: "OS keyring"}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	// Seed both so we can assert both entries are actually gone.
	if err := primary.Save(DefaultService, "user", StoredToken{AccessToken: "p"}); err != nil {
		t.Fatalf("seed primary: %v", err)
	}
	if err := secondary.Save(DefaultService, "user", StoredToken{AccessToken: "s"}); err != nil {
		t.Fatalf("seed secondary: %v", err)
	}
	store := newFallbackStore(primary, secondary)

	if err := store.Delete(DefaultService, "user"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if primary.deleteCalls != 1 {
		t.Errorf("primary delete calls: got %d, want 1", primary.deleteCalls)
	}
	if secondary.deleteCalls != 1 {
		t.Errorf("secondary delete calls: got %d, want 1", secondary.deleteCalls)
	}
	if primary.hasToken {
		t.Error("primary entry should be gone after Delete")
	}
	if secondary.hasToken {
		t.Error("secondary entry should be gone after Delete")
	}
	// Idempotent: with nothing left, Load reports the not-found sentinel.
	if _, err := store.Load(DefaultService, "user"); !errors.Is(err, ErrTokenNotFound) {
		t.Errorf("after Delete, Load should report ErrTokenNotFound; got %v", err)
	}
}

// TestFallbackStoreDeleteSurfacesErrorOnlyWhenBothFail confirms the
// best-effort posture: a failure in one backend still lets the other
// removal stand and returns success; only when BOTH backends error does
// Delete surface an error (#3320).
func TestFallbackStoreDeleteSurfacesErrorOnlyWhenBothFail(t *testing.T) {
	// Primary errors, secondary succeeds → overall success.
	primary := &fakeStore{label: "OS keyring", deleteErr: errors.New("keychain locked")}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	if err := newFallbackStore(primary, secondary).Delete(DefaultService, "user"); err != nil {
		t.Errorf("one-backend delete failure must not fail the overall delete; got %v", err)
	}

	// Both error → surface the primary's error.
	pErr := errors.New("keychain locked")
	sErr := errors.New("permission denied")
	primary2 := &fakeStore{label: "OS keyring", deleteErr: pErr}
	secondary2 := &fakeStore{label: "credentials file at /tmp/x", deleteErr: sErr}
	err := newFallbackStore(primary2, secondary2).Delete(DefaultService, "user")
	if !errors.Is(err, pErr) {
		t.Errorf("both-fail delete should surface the primary error; got %v", err)
	}
}

// TestFallbackStoreLoadBridgesToSecondaryOnNotFound is the B1
// regression: when the primary reports ErrTokenNotFound (the state a
// fresh process sees after a previous run hit the size-fallback path
// on Save), Load must surface the token persisted on the secondary so
// AC #1 ("a subsequent `meho status` reads the bearer") holds across
// process boundaries.
func TestFallbackStoreLoadBridgesToSecondaryOnNotFound(t *testing.T) {
	primary := &fakeStore{label: "OS keyring"}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	// Pre-seed the secondary as if a prior invocation had hit the
	// size-fallback path. Reset saveCalls so the assertions below
	// reflect only the Load-path behaviour under test.
	if err := secondary.Save(DefaultService, "user", StoredToken{AccessToken: "from-secondary"}); err != nil {
		t.Fatalf("seed secondary: %v", err)
	}
	secondary.saveCalls = 0

	store := newFallbackStore(primary, secondary)

	got, err := store.Load(DefaultService, "user")
	if err != nil {
		t.Fatalf("load should bridge to secondary on primary not-found: %v", err)
	}
	if got.AccessToken != "from-secondary" {
		t.Errorf("bridged load returned wrong token: got %q, want %q", got.AccessToken, "from-secondary")
	}
	if primary.loadCalls != 1 {
		t.Errorf("primary load calls: got %d, want 1", primary.loadCalls)
	}
	if secondary.loadCalls != 1 {
		t.Errorf("secondary load calls: got %d, want 1 (bridge expected)", secondary.loadCalls)
	}
}

// TestFallbackStoreLoadCrossInvocationAfterSizeFallback is the
// load-bearing AC #1 round-trip: drive a size-rejected Save through a
// fallbackStore (which lands the token in the secondary), construct a
// fresh fallbackStore over the same (primary, secondary) pair, and
// assert that Load returns the persisted token. This is the exact
// shape of two consecutive CLI invocations — `meho login` then `meho
// status` — on a macOS host where the keyring rejects the bundle by
// size.
func TestFallbackStoreLoadCrossInvocationAfterSizeFallback(t *testing.T) {
	primary := &fakeStore{label: "OS keyring", saveErr: keyring.ErrSetDataTooBig}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}

	// Invocation 1: login persists via fallback.
	loginStore := newFallbackStore(primary, secondary)
	want := StoredToken{AccessToken: "huge-bearer", RefreshToken: "huge-refresh"}
	if err := loginStore.Save(DefaultService, "user", want); err != nil {
		t.Fatalf("login save should succeed via fallback: %v", err)
	}
	if secondary.saveCalls != 1 {
		t.Fatalf("size fallback did not write to secondary: saveCalls=%d", secondary.saveCalls)
	}

	// Invocation 2: a fresh process constructs a new fallbackStore
	// over the same primary/secondary pair. The primary still
	// returns ErrTokenNotFound (it never accepted the oversized
	// payload), and Load must bridge to the secondary.
	statusStore := newFallbackStore(primary, secondary)
	got, err := statusStore.Load(DefaultService, "user")
	if err != nil {
		t.Fatalf("status load should surface the persisted token: %v", err)
	}
	if got.AccessToken != want.AccessToken {
		t.Errorf("access token: got %q, want %q", got.AccessToken, want.AccessToken)
	}
	if got.RefreshToken != want.RefreshToken {
		t.Errorf("refresh token: got %q, want %q", got.RefreshToken, want.RefreshToken)
	}
	if secondary.loadCalls != 1 {
		t.Errorf("secondary load calls: got %d, want 1", secondary.loadCalls)
	}
}

// TestFallbackStoreLoadPrefersFileWhenKeyringShadowLacksRefreshToken is
// the core #3320 regression: the keyring holds a stale entry with an
// empty refresh_token (an older/smaller login the size cap accepted)
// while the file holds the fresh, refresh-token-bearing token from a
// later size-rejected login. The old Load returned the keyring shadow —
// which trips the "no refresh_token present" guard on the next refresh.
// The reconciled Load must return the file entry instead.
func TestFallbackStoreLoadPrefersFileWhenKeyringShadowLacksRefreshToken(t *testing.T) {
	primary := &fakeStore{label: "OS keyring"}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	// Stale keyring shadow: has an access token but NO refresh_token.
	if err := primary.Save(DefaultService, "user", StoredToken{AccessToken: "stale-keyring"}); err != nil {
		t.Fatalf("seed keyring: %v", err)
	}
	// Fresh file token: carries the refresh_token that can renew the session.
	if err := secondary.Save(DefaultService, "user", StoredToken{AccessToken: "fresh-file", RefreshToken: "fresh-refresh"}); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	store := newFallbackStore(primary, secondary)

	got, err := store.Load(DefaultService, "user")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.AccessToken != "fresh-file" {
		t.Errorf("Load must return the refresh-token-bearing file entry, not the keyring shadow: got %q", got.AccessToken)
	}
	if got.RefreshToken != "fresh-refresh" {
		t.Errorf("Load must carry the file entry's refresh_token; got %q", got.RefreshToken)
	}
}

// TestFallbackStoreSaveEvictsStaleKeyringEntryOnSizeFallback is the
// #3320 Save-side regression: a size-rejected Save writes the fresh
// token to the file AND evicts the smaller, now-stale keyring entry the
// cap previously accepted, so a later Load can't hand back that shadow.
func TestFallbackStoreSaveEvictsStaleKeyringEntryOnSizeFallback(t *testing.T) {
	primary := &fakeStore{label: "OS keyring"}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	// Pre-seed the keyring with a smaller earlier login (fit under the cap).
	if err := primary.Save(DefaultService, "user", StoredToken{AccessToken: "old-small"}); err != nil {
		t.Fatalf("seed keyring: %v", err)
	}
	primary.saveCalls = 0
	// Now the keyring rejects the next (oversized) bundle by size.
	primary.saveErr = keyring.ErrSetDataTooBig

	store := newFallbackStore(primary, secondary)
	fresh := StoredToken{AccessToken: "new-big", RefreshToken: "new-refresh"}
	if err := store.Save(DefaultService, "user", fresh); err != nil {
		t.Fatalf("size-fallback save should succeed: %v", err)
	}

	if secondary.saveCalls != 1 || secondary.last.AccessToken != "new-big" {
		t.Errorf("fresh token must land in the file backend; saveCalls=%d last=%q", secondary.saveCalls, secondary.last.AccessToken)
	}
	if primary.deleteCalls != 1 {
		t.Errorf("stale keyring entry must be evicted on size fallback; deleteCalls=%d, want 1", primary.deleteCalls)
	}
	if primary.hasToken {
		t.Error("stale keyring entry should be gone after the eviction")
	}
	// End-to-end: a fresh process now Loads the file token, not the shadow.
	got, err := newFallbackStore(primary, secondary).Load(DefaultService, "user")
	if err != nil {
		t.Fatalf("post-fallback load: %v", err)
	}
	if got.AccessToken != "new-big" || got.RefreshToken != "new-refresh" {
		t.Errorf("post-fallback Load returned the wrong token: %+v", got)
	}
}

// TestFallbackStoreLoadPrefersNewerWhenBothHaveRefreshTokens covers the
// recency tie-break (#3320): when both backends carry a usable
// refresh_token, the more recently written entry (by SavedAt) wins.
func TestFallbackStoreLoadPrefersNewerWhenBothHaveRefreshTokens(t *testing.T) {
	older := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	newer := older.Add(24 * time.Hour)

	// Case 1: the file entry is newer → it wins.
	primary := &fakeStore{label: "OS keyring"}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	if err := primary.Save(DefaultService, "user", StoredToken{AccessToken: "keyring", RefreshToken: "r1", SavedAt: older}); err != nil {
		t.Fatalf("seed keyring: %v", err)
	}
	if err := secondary.Save(DefaultService, "user", StoredToken{AccessToken: "file", RefreshToken: "r2", SavedAt: newer}); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	got, err := newFallbackStore(primary, secondary).Load(DefaultService, "user")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.AccessToken != "file" {
		t.Errorf("newer file entry should win; got %q", got.AccessToken)
	}

	// Case 2: the keyring entry is newer → it wins.
	primary2 := &fakeStore{label: "OS keyring"}
	secondary2 := &fakeStore{label: "credentials file at /tmp/x"}
	if err := primary2.Save(DefaultService, "user", StoredToken{AccessToken: "keyring", RefreshToken: "r1", SavedAt: newer}); err != nil {
		t.Fatalf("seed keyring: %v", err)
	}
	if err := secondary2.Save(DefaultService, "user", StoredToken{AccessToken: "file", RefreshToken: "r2", SavedAt: older}); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	got2, err := newFallbackStore(primary2, secondary2).Load(DefaultService, "user")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got2.AccessToken != "keyring" {
		t.Errorf("newer keyring entry should win; got %q", got2.AccessToken)
	}
}

// TestFallbackStoreLoadNewerRealFileWinsOverOlderStampedKeyring is the
// end-to-end proof of the every-Save-stamps fix (#3320): the file entry
// is written through a REAL fileStore.Save (so its saved_at is stamped
// "now", not hand-set), while the keyring holds an OLDER stamped entry.
// Both carry a refresh_token, so the tie-break falls to recency — and
// because the file save is now genuinely stamped, the newer file token
// wins. Before the stamp was hoisted into the leaf stores this file
// entry would have persisted a zero saved_at and LOST to the older
// keyring stamp, which is the exact disable-mode-then-re-enable hazard.
func TestFallbackStoreLoadNewerRealFileWinsOverOlderStampedKeyring(t *testing.T) {
	// Keyring: an older, still-refreshable entry (hand-stamped in the past).
	keyringSide := &fakeStore{label: "OS keyring"}
	if err := keyringSide.Save(DefaultService, "https://x", StoredToken{
		AccessToken:  "old-keyring",
		RefreshToken: "kr",
		SavedAt:      time.Now().UTC().Add(-time.Hour),
	}); err != nil {
		t.Fatalf("seed keyring: %v", err)
	}

	// File: written through the REAL fileStore, which stamps saved_at=now.
	dir := t.TempDir()
	fileSide := NewFileStoreAt(filepath.Join(dir, "credentials.json"))
	if err := fileSide.Save(DefaultService, "https://x", StoredToken{
		AccessToken:  "new-file",
		RefreshToken: "fr",
		// No SavedAt set — the store stamps it (the previously-unstamped
		// disable-mode shape, now correctly timestamped).
	}); err != nil {
		t.Fatalf("seed file: %v", err)
	}

	store := newFallbackStore(keyringSide, fileSide)
	got, err := store.Load(DefaultService, "https://x")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.AccessToken != "new-file" {
		t.Errorf("newer real-stamped file entry should win over older keyring entry; got %q", got.AccessToken)
	}
}

// TestFallbackStoreLoadSurfacesNonNotFoundErrors confirms the bridge
// is narrow: a primary error that is NOT ErrTokenNotFound (locked
// Keychain, D-Bus unreachable, malformed entry) must propagate so a
// real keyring outage isn't masked by a stale file-store entry.
func TestFallbackStoreLoadSurfacesNonNotFoundErrors(t *testing.T) {
	bespoke := errors.New("dbus: connection refused")
	primary := &fakeStore{label: "OS keyring", loadErr: bespoke}
	secondary := &fakeStore{label: "credentials file at /tmp/x"}
	// Seed the secondary with a token that the wrapper must NOT
	// return — if the bridge fires on the wrong error class, this
	// test catches it.
	if err := secondary.Save(DefaultService, "user", StoredToken{AccessToken: "stale"}); err != nil {
		t.Fatalf("seed secondary: %v", err)
	}
	secondary.saveCalls = 0

	store := newFallbackStore(primary, secondary)

	_, err := store.Load(DefaultService, "user")
	if err == nil {
		t.Fatalf("expected primary error to propagate, got nil")
	}
	if !errors.Is(err, bespoke) {
		t.Errorf("expected original error to remain unwrappable; got: %v", err)
	}
	if secondary.loadCalls != 0 {
		t.Errorf("secondary must not be touched on non-not-found primary errors; got %d Load calls", secondary.loadCalls)
	}
}

// TestNewTokenStoreHonorsDisableEnv pins the documented escape hatch
// — `MEHO_KEYRING_DISABLE=1` forces the file backend straight from
// the constructor, no probe, no fallback wrapper. The operator
// success message must name the file backend directly.
func TestNewTokenStoreHonorsDisableEnv(t *testing.T) {
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())

	store, err := NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if _, ok := store.(*fileStore); !ok {
		t.Errorf("disable env should yield raw fileStore, got %T", store)
	}
	if !contains(store.Describe(), "credentials file at") {
		t.Errorf("describe should name file backend: %q", store.Describe())
	}
}
