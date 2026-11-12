// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

const (
	opsFolder    = "/work/ops-runbooks"
	codingFolder = "/work/some-app"
)

// fakeUpload is what the fake server got in one upload.
type fakeUpload struct {
	Fields map[string]string
	Files  map[string]string
	Tenant string
}

type fakeServer struct {
	mu       sync.Mutex
	uploads  []fakeUpload
	statuses []int // answers to give, in order; then 200
	// tokenTenants names the one tenant of a login, by access token.
	tokenTenants map[string]string
}

// fakeTenantIDs are the tenant IDs the fake server knows, by tenant name.
var fakeTenantIDs = map[string]string{
	"":         "00000000-0000-4000-8000-000000000001",
	"tenant-a": "00000000-0000-4000-8000-00000000000a",
	"tenant-b": "00000000-0000-4000-8000-00000000000b",
}

// tenantID is the tenant a request works in: the tenant header, else the
// one tenant of the login.
func (f *fakeServer) tenantID(r *http.Request) string {
	name := r.Header.Get(api.TenantHeader)
	if name == "" {
		name = f.tokenTenants[strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")]
	}
	return fakeTenantIDs[name]
}

func (f *fakeServer) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/api/v1/health/live" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"operator": map[string]any{"sub": "op", "tenant_id": f.tenantID(r), "tenant_role": "operator"},
			})
			return
		}
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		f.mu.Lock()
		defer f.mu.Unlock()
		if len(f.statuses) > 0 {
			status := f.statuses[0]
			f.statuses = f.statuses[1:]
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"detail":"session_store_unavailable"}`))
			return
		}
		if err := r.ParseMultipartForm(32 << 20); err != nil {
			t.Errorf("parse form: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		up := fakeUpload{Fields: map[string]string{}, Files: map[string]string{}}
		for k, v := range r.MultipartForm.Value {
			up.Fields[k] = v[0]
		}
		up.Tenant = r.Header.Get(api.TenantHeader)
		file, _, err := r.FormFile("archive")
		if err != nil {
			t.Errorf("archive: %v", err)
			return
		}
		up.Files = untar(t, file)
		f.uploads = append(f.uploads, up)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"session_id": up.Fields["session_id"], "state": up.Fields["state"], "outcome": "created",
			"seq": len(f.uploads), "new_bytes": 10, "quarantined": false, "redactions": 0,
		})
	}
}

func (f *fakeServer) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.uploads)
}

func untar(t *testing.T, r io.Reader) map[string]string {
	t.Helper()
	gz, err := gzip.NewReader(r)
	if err != nil {
		t.Fatalf("gzip: %v", err)
	}
	tr := tar.NewReader(gz)
	files := map[string]string{}
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return files
		}
		if err != nil {
			t.Fatalf("tar: %v", err)
		}
		data, _ := io.ReadAll(tr)
		files[hdr.Name] = string(data)
	}
}

// setup starts the fake server, logs in, writes the settings and returns the outbox.
func setup(t *testing.T, projects ...string) (*fakeServer, *httptest.Server, string) {
	t.Helper()
	fake := &fakeServer{}
	srv := httptest.NewServer(fake.handler(t))
	t.Cleanup(srv.Close)
	seedToken(t, srv.URL)
	cfg := os.Getenv("XDG_CONFIG_HOME")
	outbox := filepath.Join(t.TempDir(), "outbox")
	raw, _ := json.Marshal(pushSettings{Outbox: outbox, Projects: projects})
	if err := os.WriteFile(filepath.Join(cfg, "meho", settingsFile), raw, 0o600); err != nil {
		t.Fatalf("write settings: %v", err)
	}
	return fake, srv, outbox
}

func userLine(folder, text string) string {
	line, _ := json.Marshal(map[string]any{
		"type": "user", "cwd": folder, "version": "2.0.1", "timestamp": "2026-11-01T10:00:00Z",
		"message": map[string]any{"role": "user", "content": text},
	})
	return string(line) + "\n"
}

func writeSession(t *testing.T, outbox, id, transcript string, markers ...string) string {
	t.Helper()
	dir := filepath.Join(outbox, id)
	if err := os.MkdirAll(filepath.Join(dir, id, "subagents"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, id+".jsonl"), []byte(transcript), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, id, "subagents", "agent-1.jsonl"),
		[]byte(`{"type":"assistant"}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, m := range markers {
		if err := os.WriteFile(filepath.Join(dir, m), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func push(t *testing.T, srv *httptest.Server, dryRun bool) (pushReport, error) {
	t.Helper()
	cmd, stdout, stderr := newCmd(t, "")
	err := runPush(cmd, pushOptions{JSONOut: true, DryRun: dryRun, BackplaneOverride: srv.URL})
	var report pushReport
	if derr := json.Unmarshal(stdout.Bytes(), &report); derr != nil {
		t.Fatalf("decode report: %v; stdout=%s stderr=%s", derr, stdout.String(), stderr.String())
	}
	return report, err
}

func TestPushUploadsAnEndedSessionOnceAndKeepsAReceipt(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	dir := writeSession(t, outbox, "s1", userLine(opsFolder, "restart the web tier"), endedMarkerFile)

	report, err := push(t, srv, false)
	if err != nil {
		t.Fatalf("push: %v", err)
	}
	if fake.count() != 1 || report.Sessions[0].Result != resultSent {
		t.Fatalf("want one upload, got %d; report=%+v", fake.count(), report)
	}
	up := fake.uploads[0]
	if up.Fields["session_id"] != "s1" || up.Fields["project"] != "ops-runbooks" ||
		up.Fields["state"] != "ended" || up.Fields["app"] != "claude-code" || up.Fields["app_version"] != "2.0.1" {
		t.Fatalf("fields = %+v", up.Fields)
	}
	if _, ok := up.Files["s1.jsonl"]; !ok {
		t.Fatalf("no transcript in %v", up.Files)
	}
	if _, ok := up.Files["s1/subagents/agent-1.jsonl"]; !ok {
		t.Fatalf("no session folder file in %v", up.Files)
	}
	for name := range up.Files {
		if strings.HasPrefix(name, "meho-") {
			t.Fatalf("a marker file was uploaded: %s", name)
		}
	}
	r := loadReceipt(dir)
	if r == nil || r.SessionID != "s1" || r.State != "ended" || r.Files["s1.jsonl"].Offset == 0 {
		t.Fatalf("receipt = %+v", r)
	}

	report, err = push(t, srv, false)
	if err != nil {
		t.Fatalf("second push: %v", err)
	}
	if fake.count() != 1 || report.Sessions[0].Result != resultAlreadySent {
		t.Fatalf("second run sent again: %d uploads; report=%+v", fake.count(), report)
	}
}

func TestPushKeepsASessionOfAProjectNotOnTheAllowlist(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	writeSession(t, outbox, "s2", userLine(codingFolder, "fix the test"), endedMarkerFile)
	// A sub-folder of an allowed folder is another project folder.
	writeSession(t, outbox, "s3", userLine(opsFolder+"/sub", "look"), endedMarkerFile)

	report, err := push(t, srv, false)
	if err != nil {
		t.Fatalf("push: %v", err)
	}
	if fake.count() != 0 {
		t.Fatalf("sent %d sessions", fake.count())
	}
	for _, row := range report.Sessions {
		if row.Result != resultNotAllowed || row.Reason != reasonNotAllowed {
			t.Fatalf("row = %+v", row)
		}
	}
}

func TestPushWithAnEmptyAllowlistSendsNothing(t *testing.T) {
	fake, srv, outbox := setup(t)
	writeSession(t, outbox, "s4", userLine(opsFolder, "hello"), endedMarkerFile)
	if _, err := push(t, srv, false); err != nil {
		t.Fatalf("push: %v", err)
	}
	if fake.count() != 0 {
		t.Fatal("an empty allowlist sent a session")
	}
}

func TestPushSendsASessionMarkedByHand(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	writeSession(t, outbox, "s5", userLine(codingFolder, "hello"), endedMarkerFile, sendMarkerFile)
	if _, err := push(t, srv, false); err != nil {
		t.Fatalf("push: %v", err)
	}
	if fake.count() != 1 || fake.uploads[0].Fields["project"] != "some-app" {
		t.Fatalf("uploads = %+v", fake.uploads)
	}
}

func TestPushKeepsASessionWithNoProjectFolder(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	writeSession(t, outbox, "s6", `{"type":"summary"}`+"\n", endedMarkerFile, sendMarkerFile)
	report, _ := push(t, srv, false)
	if fake.count() != 0 || report.Sessions[0].Reason != reasonNoFolder {
		t.Fatalf("report = %+v", report)
	}
}

func TestPushReplacesAPlantedSecretBeforeTheUpload(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	secret := "ghp_" + strings.Repeat("A1b2", 9)
	writeSession(t, outbox, "s7",
		userLine(opsFolder, "send header Authorization: "+"Bearer madeUpToken123456 and "+secret),
		endedMarkerFile)
	if _, err := push(t, srv, false); err != nil {
		t.Fatalf("push: %v", err)
	}
	sent := fake.uploads[0].Files["s7.jsonl"]
	if strings.Contains(sent, "madeUpToken123456") || strings.Contains(sent, secret) {
		t.Fatalf("a secret left the laptop: %s", sent)
	}
	if !strings.Contains(sent, "[REDACTED:") {
		t.Fatalf("no marker: %s", sent)
	}
	var line map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(sent)), &line); err != nil {
		t.Fatalf("the checked line is no JSON: %v", err)
	}
}

func TestPushHoldsBackASessionWhenTheCheckIsNotSure(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	unsure := `{"cwd":"` + opsFolder + `","input":{"password":"madeup7"}}` + "\n"
	dir := writeSession(t, outbox, "s8", unsure, endedMarkerFile)
	report, err := push(t, srv, false)
	if err != nil {
		t.Fatalf("push: %v", err)
	}
	row := report.Sessions[0]
	if fake.count() != 0 || row.Result != resultHeldBack || !strings.Contains(row.Reason, reasonPossible) {
		t.Fatalf("row = %+v", row)
	}
	if strings.Contains(row.Reason, "madeup7") {
		t.Fatal("the reason shows the value")
	}
	if loadReceipt(dir) != nil {
		t.Fatal("a held-back session got a receipt")
	}
}

func TestPushSendsTheNewTailOfAnOpenSession(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	first := userLine(opsFolder, "step one")
	dir := writeSession(t, outbox, "s9", first+`{"type":"assistant","partial`)
	if _, err := push(t, srv, false); err != nil {
		t.Fatalf("push: %v", err)
	}
	if fake.count() != 1 || fake.uploads[0].Fields["state"] != "open" {
		t.Fatalf("uploads = %+v", fake.uploads)
	}
	if got := fake.uploads[0].Files["s9.jsonl"]; got != first {
		t.Fatalf("an open session sent a line still in writing: %q", got)
	}
	if r := loadReceipt(dir); r == nil || r.Files["s9.jsonl"].Offset != len(first) {
		t.Fatalf("receipt = %+v", r)
	}

	// No growth: nothing new.
	if _, err := push(t, srv, false); err != nil || fake.count() != 1 {
		t.Fatalf("sent again without growth: %v, %d", err, fake.count())
	}

	// The session grew: it is sent again with the same id, still open.
	second := userLine(opsFolder, "step two")
	grown := first + second
	if err := os.WriteFile(filepath.Join(dir, "s9.jsonl"), []byte(grown), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := push(t, srv, false); err != nil {
		t.Fatalf("push: %v", err)
	}
	if fake.count() != 2 {
		t.Fatalf("want a second upload, got %d", fake.count())
	}
	up := fake.uploads[1]
	if up.Fields["session_id"] != "s9" || up.Fields["state"] != "open" {
		t.Fatalf("fields = %+v", up.Fields)
	}
	if !strings.HasPrefix(up.Files["s9.jsonl"], fake.uploads[0].Files["s9.jsonl"]) {
		t.Fatal("the second upload does not start with the first: the server could not add the tail")
	}
	if r := loadReceipt(dir); r == nil || r.Files["s9.jsonl"].Offset != len(grown) {
		t.Fatalf("receipt offset = %+v", r)
	}
}

func TestPushRetriesAFailedUploadOnTheNextRun(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	fake.statuses = []int{http.StatusBadGateway}
	dir := writeSession(t, outbox, "s10", userLine(opsFolder, "hello"), endedMarkerFile)

	report, err := push(t, srv, false)
	if err == nil {
		t.Fatal("a failed upload must end with an error")
	}
	if report.Sessions[0].Result != resultFailed || !strings.Contains(report.Sessions[0].Reason, "502") {
		t.Fatalf("row = %+v", report.Sessions[0])
	}
	if loadReceipt(dir) != nil {
		t.Fatal("a failed upload wrote a receipt")
	}

	if _, err := push(t, srv, false); err != nil {
		t.Fatalf("retry: %v", err)
	}
	if fake.count() != 1 || loadReceipt(dir) == nil {
		t.Fatalf("the retry did not finish the upload: %d", fake.count())
	}
}

func TestPushNetworkErrorWritesNoReceipt(t *testing.T) {
	_, srv, outbox := setup(t, opsFolder)
	dir := writeSession(t, outbox, "s11", userLine(opsFolder, "hello"), endedMarkerFile)
	srv.Close()
	cmd, _, stderr := newCmd(t, "")
	err := runPush(cmd, pushOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err == nil || !strings.Contains(stderr.String(), "unreachable") {
		t.Fatalf("want an unreachable error, got %v; stderr=%s", err, stderr.String())
	}
	if loadReceipt(dir) != nil {
		t.Fatal("a network error wrote a receipt")
	}
}

func TestPushDryRunSendsNothing(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	dir := writeSession(t, outbox, "s12", userLine(opsFolder, "hello"), endedMarkerFile)
	report, err := push(t, srv, true)
	if err != nil {
		t.Fatalf("push: %v", err)
	}
	if fake.count() != 0 || report.Sessions[0].Result != resultWouldSend || report.Sessions[0].Bytes == 0 {
		t.Fatalf("report = %+v", report)
	}
	if loadReceipt(dir) != nil {
		t.Fatal("a dry run wrote a receipt")
	}
}

func TestPushHumanOutputNamesEachSession(t *testing.T) {
	_, srv, outbox := setup(t, opsFolder)
	writeSession(t, outbox, "s13", userLine(opsFolder, "hello"), endedMarkerFile)
	writeSession(t, outbox, "s14", userLine(codingFolder, "hello"), endedMarkerFile)
	cmd, stdout, stderr := newCmd(t, "")
	if err := runPush(cmd, pushOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("push: %v; %s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"s13", "ops-runbooks", "sent", "created", "s14", "not on allowlist"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output misses %q:\n%s", want, out)
		}
	}
}

func TestPushWithAMissingOutboxSaysSo(t *testing.T) {
	_, srv, _ := setup(t, opsFolder)
	cmd, stdout, _ := newCmd(t, "")
	if err := runPush(cmd, pushOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("push: %v", err)
	}
	if !strings.Contains(stdout.String(), "no sessions in the outbox") {
		t.Fatalf("stdout = %q", stdout.String())
	}
}

func TestGzipTarIsReadable(t *testing.T) {
	data, err := gzipTar(map[string][]byte{"a.jsonl": []byte("x\n"), "a/b.txt": nil})
	if err != nil {
		t.Fatal(err)
	}
	files := untar(t, bytes.NewReader(data))
	if files["a.jsonl"] != "x\n" || len(files) != 2 {
		t.Fatalf("files = %v", files)
	}
}

// pushTo runs push against url, with tenant as the chosen tenant ("" for none).
func pushTo(t *testing.T, url, tenant string) (pushReport, error) {
	t.Helper()
	cmd, stdout, stderr := newCmd(t, "")
	cmd.SetContext(api.WithTenant(cmd.Context(), tenant))
	err := runPush(cmd, pushOptions{JSONOut: true, BackplaneOverride: url})
	var report pushReport
	if derr := json.Unmarshal(stdout.Bytes(), &report); derr != nil {
		t.Fatalf("decode report: %v; stdout=%s stderr=%s", derr, stdout.String(), stderr.String())
	}
	return report, err
}

func TestPushSendsAgainToAnotherBackplane(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	other := &fakeServer{}
	otherSrv := httptest.NewServer(other.handler(t))
	t.Cleanup(otherSrv.Close)
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatal(err)
	}
	service, user := auth.KeyForBackplane(otherSrv.URL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: otherSrv.URL, AccessToken: "eyJ.test.token", TokenType: "Bearer",
		Expiry: time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatal(err)
	}
	dir := writeSession(t, outbox, "s20", userLine(opsFolder, "hello"), endedMarkerFile)
	if _, err := pushTo(t, srv.URL, ""); err != nil {
		t.Fatalf("first push: %v", err)
	}
	report, err := pushTo(t, otherSrv.URL, "")
	if err != nil {
		t.Fatalf("push to the other backplane: %v", err)
	}
	if fake.count() != 1 || other.count() != 1 || report.Sessions[0].Result != resultSent {
		t.Fatalf("uploads %d / %d; report=%+v", fake.count(), other.count(), report)
	}
	if r := loadReceipt(dir); r == nil || r.Backplane != otherSrv.URL {
		t.Fatalf("receipt = %+v", r)
	}
}

func TestPushSendsAgainToAnotherTenant(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	dir := writeSession(t, outbox, "s21", userLine(opsFolder, "hello"), endedMarkerFile)
	if _, err := pushTo(t, srv.URL, "tenant-a"); err != nil {
		t.Fatalf("first push: %v", err)
	}
	report, err := pushTo(t, srv.URL, "tenant-a")
	if err != nil || report.Sessions[0].Result != resultAlreadySent {
		t.Fatalf("same tenant: %+v, %v", report, err)
	}
	report, err = pushTo(t, srv.URL, "tenant-b")
	if err != nil {
		t.Fatalf("push to another tenant: %v", err)
	}
	if fake.count() != 2 || report.Sessions[0].Result != resultSent || fake.uploads[1].Tenant != "tenant-b" {
		t.Fatalf("uploads %d; report=%+v", fake.count(), report)
	}
	if r := loadReceipt(dir); r == nil || r.Tenant != fakeTenantIDs["tenant-b"] {
		t.Fatalf("receipt = %+v", r)
	}
}

// loginAs stores a login for url whose token belongs to one tenant only.
func loginAs(t *testing.T, fake *fakeServer, url, token, tenant string) {
	t.Helper()
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatal(err)
	}
	service, user := auth.KeyForBackplane(url)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: url, AccessToken: token, TokenType: "Bearer", Expiry: time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.tokenTenants == nil {
		fake.tokenTenants = map[string]string{}
	}
	fake.tokenTenants[token] = tenant
}

func TestPushSendsAgainAfterALoginInAnotherTenant(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	loginAs(t, fake, srv.URL, "eyJ.tenant-a.token", "tenant-a")
	dir := writeSession(t, outbox, "s24", userLine(opsFolder, "hello"), endedMarkerFile)
	if _, err := pushTo(t, srv.URL, ""); err != nil {
		t.Fatalf("first push: %v", err)
	}
	report, err := pushTo(t, srv.URL, "")
	if err != nil || report.Sessions[0].Result != resultAlreadySent {
		t.Fatalf("same login: %+v, %v", report, err)
	}
	loginAs(t, fake, srv.URL, "eyJ.tenant-b.token", "tenant-b")
	report, err = pushTo(t, srv.URL, "")
	if err != nil {
		t.Fatalf("push after the new login: %v", err)
	}
	if fake.count() != 2 || report.Sessions[0].Result != resultSent || fake.uploads[1].Tenant != "" {
		t.Fatalf("uploads %d; report=%+v", fake.count(), report)
	}
	if r := loadReceipt(dir); r == nil || r.Tenant != fakeTenantIDs["tenant-b"] {
		t.Fatalf("receipt = %+v", r)
	}
}

func TestPushFailsWhenASessionCannotBeRead(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	dir := writeSession(t, outbox, "s22", userLine(opsFolder, "hello"), endedMarkerFile)
	transcript := filepath.Join(dir, "s22.jsonl")
	if err := os.Chmod(transcript, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(transcript, 0o600) })
	if f, err := os.Open(transcript); err == nil {
		_ = f.Close()
		t.Skip("this user can read a file with no permissions")
	}
	for _, dryRun := range []bool{false, true} {
		report, err := push(t, srv, dryRun)
		if err == nil {
			t.Fatalf("dry run %v: an unreadable session must end with an error", dryRun)
		}
		if report.Sessions[0].Result != resultFailed || fake.count() != 0 {
			t.Fatalf("dry run %v: row = %+v", dryRun, report.Sessions[0])
		}
	}
}

func TestPushKeepsBINDAndURLSecretsOnTheLaptop(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	text := `secret "bWFkZVVwU2VjcmV0VmFsdWU=";` + " https://madeUpUser77@example.com/x " +
		"https://example.com/f?%74oken=madeUpValue78"
	writeSession(t, outbox, "s23", userLine(opsFolder, text), endedMarkerFile)
	if _, err := push(t, srv, false); err != nil {
		t.Fatalf("push: %v", err)
	}
	sent := fake.uploads[0].Files["s23.jsonl"]
	for _, secret := range []string{"bWFkZVVwU2VjcmV0VmFsdWU=", "madeUpUser77", "madeUpValue78"} {
		if strings.Contains(sent, secret) {
			t.Fatalf("%s left the laptop: %s", secret, sent)
		}
	}
}

func TestPushHoldsBackABINDSecretOverLinesInATextFile(t *testing.T) {
	fake, srv, outbox := setup(t, opsFolder)
	dir := writeSession(t, outbox, "s25", userLine(opsFolder, "hello"), endedMarkerFile)
	notes := filepath.Join(dir, "s25", "named.conf")
	if err := os.WriteFile(notes, []byte("secret /* key */ \"bWFkZVVw\nU2VjcmV0VmFsdWU=\";\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	report, err := push(t, srv, false)
	if err != nil {
		t.Fatalf("push: %v", err)
	}
	row := report.Sessions[0]
	if fake.count() != 0 || row.Result != resultHeldBack || !strings.Contains(row.Reason, "s25/named.conf") {
		t.Fatalf("uploads %d; row = %+v", fake.count(), row)
	}
}
