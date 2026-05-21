// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package auth implements the operator authentication surface for the
// meho CLI: OAuth 2.0 device authorization (RFC 8628) against the
// backplane's Keycloak realm, plus cross-platform token persistence.
//
// Persistence is split behind a small TokenStore interface so the
// device-code flow and the cobra command never see the difference
// between an OS keyring backend (zalando/go-keyring) and the file
// fallback used on headless / CI hosts. ADR 0004 locks the keyring
// choice and the file-fallback location.
package auth

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/zalando/go-keyring"
)

// StoredToken is the persisted shape of a successful login. The CLI
// re-reads this in subsequent commands (meho status, future ops) so
// every field the operator depends on for follow-up work is captured
// here — not just the access token.
//
// JSON tags are stable: the file-fallback backend serialises this
// shape verbatim, so a rename here is a wire-compat break for hosts
// that persist tokens between CLI upgrades. Treat the field names as
// part of the public contract.
type StoredToken struct {
	// BackplaneURL is the canonical address of the backplane this
	// token authenticates against. Stored alongside the token so
	// future commands don't have to be re-told which backplane the
	// operator targets.
	BackplaneURL string `json:"backplane_url"`
	// Issuer is the Keycloak realm URL the token was issued by.
	// Persisted so subsequent commands can refresh / introspect
	// without re-running discovery.
	Issuer string `json:"issuer"`
	// ClientID is the OAuth client identifier the device flow used.
	// Persisted so a future `meho refresh` (v0.2) can rebuild the
	// oauth2.Config without another discovery roundtrip.
	ClientID string `json:"client_id"`
	// AccessToken is the bearer token operators pass to the backplane.
	AccessToken string `json:"access_token"`
	// RefreshToken is captured for v0.2's refresh path; v0.1 never
	// uses it, but storing it now means v0.2 can light up without a
	// token-store schema migration. Empty when the IdP didn't issue
	// one (rare for confidential clients, common for public clients
	// configured without offline_access).
	RefreshToken string `json:"refresh_token,omitempty"`
	// IDToken is the OIDC id_token; persisted for future identity
	// claims the backplane might require beyond the access token's
	// surface.
	IDToken string `json:"id_token,omitempty"`
	// TokenType is almost always "Bearer". Captured verbatim so the
	// CLI replays whatever the IdP set on every backplane request.
	TokenType string `json:"token_type,omitempty"`
	// Expiry is the access token's expiration moment in UTC. Zero
	// when the IdP omitted expires_in (treat as "unknown — try and
	// see"). Compared via time.Now().After() at request time.
	Expiry time.Time `json:"expiry,omitempty"`
}

// TokenStore is the storage backend for credentials persisted across
// meho invocations. The interface is the seam between the OS keyring
// (preferred — leans on Keychain / Secret Service / Wincred) and the
// file fallback used on headless hosts where no keyring service is
// available.
//
// Service / user terminology matches zalando/go-keyring's API surface
// so the keyring backend can pass values through verbatim. The file
// backend interprets (service, user) as a logical addressing pair
// even though only one credential is stored per host in v0.1.
type TokenStore interface {
	// Save persists tok under the (service, user) key. Overwrites any
	// existing entry. Returns the underlying backend error unwrapped
	// so callers can distinguish per-platform failures (e.g.
	// keyring.ErrUnsupportedPlatform) when they need to fall back.
	Save(service, user string, tok StoredToken) error
	// Load reads the token persisted under (service, user). Returns
	// ErrTokenNotFound when no entry exists; this sentinel lets the
	// caller distinguish "operator never logged in" from "storage is
	// broken".
	Load(service, user string) (StoredToken, error)
	// Delete removes the entry under (service, user). Returns nil if
	// the entry was absent — delete is idempotent, matching the
	// posture of every other secret-store delete in the
	// cobra-CLI ecosystem (gh, argocd, flux).
	Delete(service, user string) error
	// Describe returns a short human-readable label for the backend
	// (e.g. "OS keyring", "credentials file at ~/.config/meho/...").
	// Used by the login command's success message so the operator
	// knows which storage backend received their token. Carries no
	// secret content — safe to log.
	Describe() string
}

// ErrTokenNotFound is returned by Load when no token exists under the
// given (service, user). Callers (meho status, future auth checks)
// use errors.Is to distinguish "first-time user" from a real backend
// failure.
var ErrTokenNotFound = errors.New("meho: no stored token")

// DefaultService is the canonical service name the CLI registers
// under in the OS keyring. One entry per (service, user) pair — v0.1
// uses ("meho", <backplane-url>) so a single host can hold tokens
// for several backplanes once a hypothetical v0.2 multi-tenant CLI
// lands without a key-shape migration.
const DefaultService = "meho"

// keyringStore is the OS-keychain-backed TokenStore. Set on macOS via
// Keychain, on Linux via Secret Service (D-Bus), on Windows via
// Wincred. The library returns keyring.ErrUnsupportedPlatform when
// none of those are available (most CI containers, sshed hosts
// without a D-Bus session) — the constructor below tests for that
// condition up front and falls back to fileStore.
type keyringStore struct{}

// Save serialises tok as JSON and stores it as a single keyring
// secret. The serialisation keeps every field round-trippable, which
// matters because the file fallback writes the identical JSON shape
// and we want the two backends to be transparent to callers.
func (keyringStore) Save(service, user string, tok StoredToken) error {
	blob, err := json.Marshal(tok)
	if err != nil {
		return fmt.Errorf("meho: marshal token: %w", err)
	}
	if err := keyring.Set(service, user, string(blob)); err != nil {
		return fmt.Errorf("meho: keyring set: %w", err)
	}
	return nil
}

// Load fetches the JSON-serialised token from the keyring and
// deserialises it. keyring.ErrNotFound is translated to the package
// ErrTokenNotFound sentinel so callers don't have to import the
// backend's error symbols.
func (keyringStore) Load(service, user string) (StoredToken, error) {
	raw, err := keyring.Get(service, user)
	if err != nil {
		if errors.Is(err, keyring.ErrNotFound) {
			return StoredToken{}, ErrTokenNotFound
		}
		return StoredToken{}, fmt.Errorf("meho: keyring get: %w", err)
	}
	var tok StoredToken
	if err := json.Unmarshal([]byte(raw), &tok); err != nil {
		return StoredToken{}, fmt.Errorf("meho: unmarshal token: %w", err)
	}
	return tok, nil
}

// Delete removes the keyring entry; absence is not an error.
func (keyringStore) Delete(service, user string) error {
	if err := keyring.Delete(service, user); err != nil {
		if errors.Is(err, keyring.ErrNotFound) {
			return nil
		}
		return fmt.Errorf("meho: keyring delete: %w", err)
	}
	return nil
}

// Describe identifies this backend in operator-facing messages.
func (keyringStore) Describe() string { return "OS keyring" }

// fileStore is the headless-host fallback. Persistence is a single
// JSON file at <credsDir>/credentials.json with mode 0600 — strict
// enough that an operator who accidentally `cat`s it knows it's a
// secret, lax enough that the same UID can read it back without sudo.
// One file holds the entire map of (service, user) → StoredToken so
// hosts that target several backplanes still survive a single chmod.
type fileStore struct {
	// path is the absolute file location. Set by newFileStore via
	// resolveCredsPath so the resolution discipline (XDG_CONFIG_HOME
	// then $HOME/.config, with a 0700 parent dir) lives in one
	// place and is straightforward to unit-test by passing a tmpdir.
	path string
}

// fileBlob is the on-disk schema. Wrapped in a struct rather than
// stored as a bare map so future fields (schema version, last-used
// timestamp) can be added without breaking older readers that ignore
// unknown JSON keys by default.
type fileBlob struct {
	// Entries is keyed by "<service>\x00<user>" — the NUL separator
	// is illegal in both service names and URLs so it can never
	// collide with a legitimate component. Concatenation keeps the
	// JSON shape flat for human inspection.
	Entries map[string]StoredToken `json:"entries"`
}

// fileKey concatenates service and user with a NUL separator so the
// composed string is unambiguously decomposable. Used only as the
// in-memory map key; the on-disk JSON still serialises the
// concatenated form because rendering "user\x00service" in JSON
// (via base64 or escaping) buys nothing operators ever read by hand.
func fileKey(service, user string) string {
	return service + "\x00" + user
}

func (s *fileStore) Save(service, user string, tok StoredToken) error {
	blob, err := s.read()
	if err != nil {
		return err
	}
	if blob.Entries == nil {
		blob.Entries = make(map[string]StoredToken)
	}
	blob.Entries[fileKey(service, user)] = tok
	return s.write(blob)
}

func (s *fileStore) Load(service, user string) (StoredToken, error) {
	blob, err := s.read()
	if err != nil {
		return StoredToken{}, err
	}
	tok, ok := blob.Entries[fileKey(service, user)]
	if !ok {
		return StoredToken{}, ErrTokenNotFound
	}
	return tok, nil
}

func (s *fileStore) Delete(service, user string) error {
	blob, err := s.read()
	if err != nil {
		return err
	}
	if _, ok := blob.Entries[fileKey(service, user)]; !ok {
		return nil
	}
	delete(blob.Entries, fileKey(service, user))
	return s.write(blob)
}

// Describe identifies the file backend and its on-disk location.
// The path is part of the message so an operator with no shell
// history knows exactly which file to inspect / chmod / delete.
func (s *fileStore) Describe() string {
	return fmt.Sprintf("credentials file at %s", s.path)
}

// read returns an empty fileBlob when the credentials file is absent;
// any other read error (permissions, malformed JSON) propagates.
func (s *fileStore) read() (fileBlob, error) {
	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return fileBlob{Entries: map[string]StoredToken{}}, nil
		}
		return fileBlob{}, fmt.Errorf("meho: read credentials: %w", err)
	}
	var blob fileBlob
	if err := json.Unmarshal(data, &blob); err != nil {
		return fileBlob{}, fmt.Errorf("meho: parse credentials: %w", err)
	}
	return blob, nil
}

// write serialises the blob with 0600 file permissions and 0700 dir
// permissions. Uses an atomic write (tmpfile + rename) so a partial
// flush can't truncate the existing file — token storage that
// silently empties on a bad flush is the worst-of-both outcome.
func (s *fileStore) write(blob fileBlob) error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return fmt.Errorf("meho: create credentials dir: %w", err)
	}
	data, err := json.MarshalIndent(blob, "", "  ")
	if err != nil {
		return fmt.Errorf("meho: marshal credentials: %w", err)
	}
	tmp := s.path + ".tmp"
	// O_EXCL prevents clobbering a tmpfile left over from a crashed
	// previous run — that file might belong to a different process
	// still mid-write. The cost is one stale-file cleanup on the
	// operator's side when it happens; the benefit is no silent
	// data race.
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		// If a stale tmpfile blocks us, surface a specific message so
		// the operator knows what to do (manually rm it).
		if errors.Is(err, os.ErrExist) {
			return fmt.Errorf("meho: stale credentials tmpfile at %s — remove and retry", tmp)
		}
		return fmt.Errorf("meho: open credentials tmpfile: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("meho: write credentials: %w", err)
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("meho: close credentials: %w", err)
	}
	if err := os.Rename(tmp, s.path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("meho: install credentials: %w", err)
	}
	return nil
}

// resolveCredsPath returns the XDG-correct credentials path:
// $XDG_CONFIG_HOME/meho/credentials.json when XDG_CONFIG_HOME is set,
// otherwise $HOME/.config/meho/credentials.json. Mirrors the
// convention every modern Linux CLI follows (gh, argocd, flux).
// Exported indirectly via NewFileStore so tests can pass in a tmpdir
// without having to set environment variables.
func resolveCredsPath() (string, error) {
	if xdg := os.Getenv("XDG_CONFIG_HOME"); xdg != "" {
		return filepath.Join(xdg, "meho", "credentials.json"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("meho: locate home dir: %w", err)
	}
	return filepath.Join(home, ".config", "meho", "credentials.json"), nil
}

// NewFileStore constructs a fileStore at the XDG-resolved location.
// Exposed for the rare case where a caller wants to force the file
// backend explicitly (testing, headless CI runs that pre-set the
// MEHO_KEYRING_DISABLE escape hatch).
func NewFileStore() (TokenStore, error) {
	path, err := resolveCredsPath()
	if err != nil {
		return nil, err
	}
	return &fileStore{path: path}, nil
}

// NewFileStoreAt is the test-friendly constructor: takes the file
// path verbatim, skips XDG resolution. Used by unit tests and by the
// disable-keyring escape hatch test.
func NewFileStoreAt(path string) TokenStore {
	return &fileStore{path: path}
}

// NewTokenStore picks the right backend:
//
//  1. If MEHO_KEYRING_DISABLE is set to a non-empty value, use the
//     file backend unconditionally. This is the documented escape
//     hatch for CI hosts and operators who explicitly want the file
//     backend even when a keyring is present (e.g. shared dev hosts
//     where the keyring is somebody else's session).
//  2. Probe the OS keyring with a no-op delete on a sentinel key.
//     Any error indicating "no keyring available" (the package
//     ErrUnsupportedPlatform on platforms without a backend, or a
//     keyring.Get failure on Linux hosts where Secret Service is
//     unreachable) routes us to the file backend.
//  3. Otherwise, return the keyring backend **wrapped in a
//     fallbackStore** that transparently retries against the file
//     backend on runtime size errors. macOS's legacy Keychain
//     `kSecValueData` path caps a single value at ~4 KiB and
//     go-keyring's add-generic-password shell-out enforces a
//     4096-byte command-line limit; an OIDC token bundle
//     (access_token + refresh_token + id_token JSON-wrapped, plus
//     go-keyring's `go-keyring-base64:` chunk marker) regularly
//     exceeds that and surfaces as keyring.ErrSetDataTooBig. The
//     wrapper catches that specific sentinel and writes to the file
//     backend instead, leaving every other keyring failure (D-Bus
//     down, locked Keychain, etc.) to propagate unchanged.
//
// Probe-then-fallback is necessary because the zalando/go-keyring
// library doesn't expose an "is the keyring available?" function —
// the documented pattern (per its README and per upstream issue #95
// of github.com/99designs/keyring which has the same shape) is to
// call Set/Get on a sentinel and inspect the error.
func NewTokenStore() (TokenStore, error) {
	if disabled := os.Getenv("MEHO_KEYRING_DISABLE"); disabled != "" {
		return NewFileStore()
	}
	if keyringAvailable() {
		file, err := NewFileStore()
		if err != nil {
			return nil, err
		}
		return newFallbackStore(keyringStore{}, file), nil
	}
	return NewFileStore()
}

// fallbackStore wraps a primary TokenStore with a secondary that
// catches a narrowly-defined runtime failure. The intended pairing is
// keyringStore (primary) + fileStore (secondary): if `keyring.Set`
// rejects an oversized token bundle with keyring.ErrSetDataTooBig the
// wrapper transparently re-saves to the file store and remembers that
// fact so `Describe()` (which the login command surfaces in its
// success message) names the backend that actually received the
// token.
//
// Other failure modes from the primary — D-Bus unreachable, Keychain
// locked, etc. — are left to surface unchanged. We deliberately match
// on the typed sentinel rather than a substring of the error message
// so a future go-keyring release that rewords the error string can't
// silently change which failures route to the file fallback.
//
// Load / Delete go to the primary only. The fallback never reads from
// the secondary opportunistically, because the secondary is the file
// store and reading from it would mask a keyring outage (an operator
// would see a stale token instead of the expected "please log in"
// error). The narrow contract — fall back only on a save-time size
// rejection — keeps the wrapper's behaviour predictable.
type fallbackStore struct {
	primary   TokenStore
	secondary TokenStore

	// mu guards lastBackend. Save can be racy if a future caller
	// drives Save concurrently from multiple goroutines (today
	// `meho login` is single-threaded, but the v0.2 refresh path
	// may run a background renew while the foreground is also
	// touching the store).
	mu          sync.Mutex
	lastBackend TokenStore
}

// newFallbackStore is the package-internal constructor so tests can
// build a fallback over any pair of TokenStore implementations.
func newFallbackStore(primary, secondary TokenStore) *fallbackStore {
	return &fallbackStore{
		primary:     primary,
		secondary:   secondary,
		lastBackend: primary,
	}
}

// Save tries the primary store first. On a size-rejection sentinel —
// keyring.ErrSetDataTooBig, raised by go-keyring's macOS and Windows
// backends when the password payload exceeds the platform's hard cap —
// we transparently retry against the secondary and record that the
// secondary now holds the token. Every other primary-side error
// propagates unchanged so unrelated keyring failures (locked Keychain,
// D-Bus down) still surface to the operator.
func (s *fallbackStore) Save(service, user string, tok StoredToken) error {
	err := s.primary.Save(service, user, tok)
	if err == nil {
		s.mu.Lock()
		s.lastBackend = s.primary
		s.mu.Unlock()
		return nil
	}
	if !errors.Is(err, keyring.ErrSetDataTooBig) {
		return err
	}
	if ferr := s.secondary.Save(service, user, tok); ferr != nil {
		// Both backends failed. Surface the file-store error wrapped
		// so the operator sees the real disk-side problem; mention
		// the keyring rejection too so they understand why we tried
		// the file store at all.
		return fmt.Errorf("meho: keyring rejected token by size (%v) and file fallback also failed: %w", err, ferr)
	}
	s.mu.Lock()
	s.lastBackend = s.secondary
	s.mu.Unlock()
	return nil
}

// Load reads from the primary store only — see the type comment for
// why we don't opportunistically check the secondary.
func (s *fallbackStore) Load(service, user string) (StoredToken, error) {
	return s.primary.Load(service, user)
}

// Delete removes the entry from the primary store only. The secondary
// is only ever written to after a Save fell back; if a previous run
// produced a file-store entry, the operator should clean it up
// explicitly (or re-run `meho login`, which will overwrite it).
func (s *fallbackStore) Delete(service, user string) error {
	return s.primary.Delete(service, user)
}

// Describe names the backend that received the most recent Save. The
// login command prints this in its success message so the operator
// knows where the token landed — critical when the fallback fired,
// because the on-disk file path is the recovery breadcrumb if anything
// in the rest of the system misbehaves.
func (s *fallbackStore) Describe() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastBackend.Describe()
}

// keyringAvailable probes whether the OS keyring is usable. We use
// Get against a sentinel key rather than Set so we never leave
// state behind; ErrNotFound is the success signal (keyring is
// reachable, the sentinel just isn't there). Any other error means
// the keyring is unreachable on this host.
func keyringAvailable() bool {
	_, err := keyring.Get(DefaultService+"-probe", "meho-availability-check")
	if err == nil {
		// Sentinel exists from a prior run — keyring is reachable.
		return true
	}
	return errors.Is(err, keyring.ErrNotFound)
}
