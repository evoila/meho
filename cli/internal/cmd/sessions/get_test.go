// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type tarEntry struct {
	name     string
	body     string
	typeflag byte
}

func gzipTar(t *testing.T, entries ...tarEntry) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	tw := tar.NewWriter(zw)
	for _, e := range entries {
		flag := e.typeflag
		if flag == 0 {
			flag = tar.TypeReg
		}
		hdr := &tar.Header{Name: e.name, Mode: 0o600, Size: int64(len(e.body)), Typeflag: flag}
		if flag == tar.TypeSymlink {
			hdr.Size, hdr.Linkname = 0, "/etc/passwd"
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatalf("WriteHeader: %v", err)
		}
		if flag == tar.TypeReg {
			if _, err := tw.Write([]byte(e.body)); err != nil {
				t.Fatalf("Write: %v", err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// fakeUpload is one stored upload: the archive the server holds, the file
// rows of the upload (offset and added count the uploaded bytes), and
// whether only an admin may open it.
type fakeUpload struct {
	archive     []byte
	rows        map[string][2]int
	quarantined bool
}

// fakeSessionServer serves `show` and the upload downloads of session abc.
// admin decides whether a quarantined upload is sent or refused.
func fakeSessionServer(t *testing.T, uploads []fakeUpload, admin bool) (*httptest.Server, map[int]int) {
	t.Helper()
	downloads := map[int]int{}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sessions/abc", func(w http.ResponseWriter, _ *http.Request) {
		body := summary("abc")
		body["files"] = map[string]any{}
		parts := []any{}
		for i, u := range uploads {
			files := map[string]any{}
			for path, row := range u.rows {
				files[path] = map[string]any{"offset": row[0], "added": row[1], "size": row[0] + row[1], "sha256": "00"}
			}
			sum := sha256.Sum256(u.archive)
			parts = append(parts, map[string]any{
				"seq": i + 1, "uploaded_at": "2026-11-03T10:00:00Z", "archive_bytes": len(u.archive),
				"archive_sha256": hex.EncodeToString(sum[:]), "quarantined": u.quarantined,
				"redactions": 0, "files": files,
			})
		}
		body["uploads"] = parts
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(body)
	})
	for i, u := range uploads {
		seq, upload := i+1, u
		mux.HandleFunc(fmt.Sprintf("/api/v1/sessions/abc/uploads/%d", seq), func(w http.ResponseWriter, _ *http.Request) {
			downloads[seq]++
			if upload.quarantined && !admin {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"detail":"session_upload_quarantined"}`))
				return
			}
			w.Header().Set("Content-Type", "application/gzip")
			_, _ = w.Write(upload.archive)
		})
	}
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedToken(t, srv.URL)
	return srv, downloads
}

// threeUploads is a session uploaded three times. The second tail of the
// transcript is shorter after the check than it was uploaded: a secret was
// replaced, so offsets count uploaded bytes and the tails are joined as stored.
func threeUploads(t *testing.T, quarantineSecond bool) []fakeUpload {
	return []fakeUpload{
		{
			archive: gzipTar(t, tarEntry{name: "abc.jsonl", body: "line one\n"}),
			rows:    map[string][2]int{"abc.jsonl": {0, 9}},
		},
		{
			archive: gzipTar(t,
				tarEntry{name: "abc", typeflag: tar.TypeDir},
				tarEntry{name: "abc.jsonl", body: "[REDACTED:x]\n"},
				tarEntry{name: "abc/subagents/agent-1.jsonl", body: "sub one\n"}),
			rows:        map[string][2]int{"abc.jsonl": {9, 40}, "abc/subagents/agent-1.jsonl": {0, 8}},
			quarantined: quarantineSecond,
		},
		{
			archive: gzipTar(t, tarEntry{name: "abc.jsonl", body: "line three\n"}),
			rows:    map[string][2]int{"abc.jsonl": {49, 11}},
		},
	}
}

func readOut(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(data)
}

func TestGetJoinsThreeUploadsIntoTheStoredFiles(t *testing.T) {
	srv, downloads := fakeSessionServer(t, threeUploads(t, false), false)
	folder := t.TempDir()

	cmd, stdout, stderr := newCmd(t, "")
	if err := runGet(cmd, "abc", folder, true, srv.URL); err != nil {
		t.Fatalf("runGet: %v; stderr=%s", err, stderr.String())
	}
	if got := readOut(t, filepath.Join(folder, "abc", "abc.jsonl")); got != "line one\n[REDACTED:x]\nline three\n" {
		t.Fatalf("transcript = %q", got)
	}
	if got := readOut(t, filepath.Join(folder, "abc", "abc", "subagents", "agent-1.jsonl")); got != "sub one\n" {
		t.Fatalf("subagent = %q", got)
	}
	// One download per upload: the server writes one audit row for each.
	if downloads[1] != 1 || downloads[2] != 1 || downloads[3] != 1 {
		t.Fatalf("downloads = %v", downloads)
	}
	var got getResult
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(got.Files) != 2 || !got.Files[0].Complete || got.Files[0].Bytes != 33 || len(got.Skipped) != 0 {
		t.Fatalf("result = %+v", got)
	}
}

func TestGetSkipsAQuarantinedUploadForAnOperator(t *testing.T) {
	srv, _ := fakeSessionServer(t, threeUploads(t, true), false)
	folder := t.TempDir()

	cmd, stdout, stderr := newCmd(t, "")
	if err := runGet(cmd, "abc", folder, false, srv.URL); err != nil {
		t.Fatalf("runGet: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"skipped upload #2: it is in quarantine",
		"incomplete: abc.jsonl (9 bytes, written up to skipped upload #2)",
		"not written: abc/subagents/agent-1.jsonl",
		"wrote 1 files",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in %q", want, out)
		}
	}
	// The part after the hole is never joined on: the file stops before it.
	if got := readOut(t, filepath.Join(folder, "abc", "abc.jsonl")); got != "line one\n" {
		t.Fatalf("transcript = %q", got)
	}
	if _, err := os.Stat(filepath.Join(folder, "abc", "abc", "subagents", "agent-1.jsonl")); !os.IsNotExist(err) {
		t.Fatalf("the subagent file was written: %v", err)
	}
}

func TestGetWritesAQuarantinedUploadForATenantAdmin(t *testing.T) {
	srv, _ := fakeSessionServer(t, threeUploads(t, true), true)
	folder := t.TempDir()

	cmd, stdout, stderr := newCmd(t, "")
	if err := runGet(cmd, "abc", folder, false, srv.URL); err != nil {
		t.Fatalf("runGet: %v; stderr=%s", err, stderr.String())
	}
	if strings.Contains(stdout.String(), "skipped") {
		t.Fatalf("out = %q", stdout.String())
	}
	if got := readOut(t, filepath.Join(folder, "abc", "abc.jsonl")); got != "line one\n[REDACTED:x]\nline three\n" {
		t.Fatalf("transcript = %q", got)
	}
}

func TestGetStopsOnAnUnsafeArchiveBeforeAnyFileIsWritten(t *testing.T) {
	for name, entry := range map[string]tarEntry{
		"dot-dot":       {name: "abc/../../evil.jsonl", body: "x\n"},
		"leading slash": {name: "/tmp/evil.jsonl", body: "x\n"},
		"link":          {name: "evil.jsonl", typeflag: tar.TypeSymlink},
	} {
		t.Run(name, func(t *testing.T) {
			uploads := threeUploads(t, false)
			uploads[2] = fakeUpload{archive: gzipTar(t, entry), rows: map[string][2]int{entry.name: {0, 2}}}
			srv, _ := fakeSessionServer(t, uploads, false)
			folder := t.TempDir()

			cmd, _, stderr := newCmd(t, "")
			if err := runGet(cmd, "abc", folder, false, srv.URL); err == nil {
				t.Fatal("want an error")
			}
			if !strings.Contains(stderr.String(), "no file was written") {
				t.Fatalf("stderr = %q", stderr.String())
			}
			if entries, _ := os.ReadDir(folder); len(entries) != 0 {
				t.Fatalf("files were written: %v", entries)
			}
		})
	}
}

func TestGetRefusesAHoleOrAChangedArchive(t *testing.T) {
	hole := threeUploads(t, false)
	hole[2].rows = map[string][2]int{"abc.jsonl": {50, 11}}
	extra := threeUploads(t, false)
	extra[0].archive = gzipTar(t, tarEntry{name: "abc.jsonl", body: "line one\n"}, tarEntry{name: "other", body: "x"})
	for name, uploads := range map[string][]fakeUpload{"hole": hole, "file not in the row": extra} {
		t.Run(name, func(t *testing.T) {
			srv, _ := fakeSessionServer(t, uploads, false)
			folder := t.TempDir()
			cmd, _, stderr := newCmd(t, "")
			if err := runGet(cmd, "abc", folder, false, srv.URL); err == nil {
				t.Fatal("want an error")
			}
			if entries, _ := os.ReadDir(folder); len(entries) != 0 {
				t.Fatalf("files were written: %v; stderr=%s", entries, stderr.String())
			}
		})
	}
}

func TestGetRefusesAnUnsafeSessionID(t *testing.T) {
	cmd, _, stderr := newCmd(t, "")
	if err := runGet(cmd, "../abc", t.TempDir(), false, "https://meho.example.com"); err == nil {
		t.Fatal("want an error")
	}
	if !strings.Contains(stderr.String(), "session id") {
		t.Fatalf("stderr = %q", stderr.String())
	}
}
