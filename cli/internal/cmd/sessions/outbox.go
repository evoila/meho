// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// The outbox is a local folder with one sub-folder per session:
//
//	<outbox>/<session-id>/<session-id>.jsonl   the transcript (needed)
//	<outbox>/<session-id>/<session-id>/...     the session's folder (optional)
//	<outbox>/<session-id>/meho-ended           the session ended (written by the hook)
//	<outbox>/<session-id>/meho-send            a person marked the session by hand
//	<outbox>/<session-id>/meho-receipt.json    what push sent last (written by push)
//
// Only the transcript and the session's folder are uploaded.

const (
	endedMarkerFile = "meho-ended"
	sendMarkerFile  = "meho-send"
	receiptFile     = "meho-receipt.json"
	settingsFile    = "sessions.json"
	appClaudeCode   = "claude-code"
)

// sessionIDPattern is the server's rule for a session id.
var sessionIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// pushSettings is the file <meho config dir>/sessions.json.
type pushSettings struct {
	// Outbox is the outbox folder. Empty means <meho config dir>/session-outbox.
	Outbox string `json:"outbox"`
	// Projects is the allowlist: the folders a session may be started in.
	// Only operations projects belong here. An empty list sends nothing.
	Projects []string `json:"projects"`
}

func mehoConfigDir() (string, error) {
	if xdg := os.Getenv("XDG_CONFIG_HOME"); xdg != "" {
		return filepath.Join(xdg, "meho"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("locate home folder: %w", err)
	}
	return filepath.Join(home, ".config", "meho"), nil
}

// loadPushSettings reads sessions.json. A missing file gives the defaults.
func loadPushSettings() (pushSettings, string, error) {
	dir, err := mehoConfigDir()
	if err != nil {
		return pushSettings{}, "", err
	}
	path := filepath.Join(dir, settingsFile)
	var s pushSettings
	raw, err := os.ReadFile(path)
	switch {
	case errors.Is(err, fs.ErrNotExist):
	case err != nil:
		return pushSettings{}, path, fmt.Errorf("read %s: %w", path, err)
	default:
		if err := json.Unmarshal(raw, &s); err != nil {
			return pushSettings{}, path, fmt.Errorf("read %s: %w", path, err)
		}
	}
	if s.Outbox == "" {
		s.Outbox = filepath.Join(dir, "session-outbox")
	}
	s.Outbox = expandHome(s.Outbox)
	return s, path, nil
}

func expandHome(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, strings.TrimPrefix(path, "~"))
		}
	}
	return path
}

// allowed says whether folder is on the allowlist. A folder matches only
// itself, not its sub-folders.
func (s pushSettings) allowed(folder string) bool {
	if folder == "" {
		return false
	}
	clean := filepath.Clean(folder)
	for _, p := range s.Projects {
		if p != "" && filepath.Clean(expandHome(p)) == clean {
			return true
		}
	}
	return false
}

// outboxSession is one session folder in the outbox.
type outboxSession struct {
	ID     string
	Dir    string
	Ended  bool
	Marked bool
	// Folder is the folder the session was started in (from the transcript).
	Folder     string
	AppVersion string
	StartedAt  *time.Time
	LastAt     *time.Time
}

// Project is the project name sent to MEHO: the last part of Folder.
func (s outboxSession) Project() string {
	if s.Folder == "" {
		return ""
	}
	return filepath.Base(filepath.Clean(s.Folder))
}

// State is the session state sent to MEHO.
func (s outboxSession) State() string {
	if s.Ended {
		return "ended"
	}
	return "open"
}

// listOutbox returns the sessions of the outbox in id order. A missing
// outbox has no sessions. Folders that are no session are left out.
func listOutbox(outbox string) ([]outboxSession, error) {
	entries, err := os.ReadDir(outbox)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read outbox %s: %w", outbox, err)
	}
	var sessions []outboxSession
	for _, e := range entries {
		if !e.IsDir() || !sessionIDPattern.MatchString(e.Name()) || e.Name() == "retention" {
			continue
		}
		dir := filepath.Join(outbox, e.Name())
		if !isRegular(filepath.Join(dir, e.Name()+".jsonl")) {
			continue
		}
		sessions = append(sessions, outboxSession{
			ID:     e.Name(),
			Dir:    dir,
			Ended:  exists(filepath.Join(dir, endedMarkerFile)),
			Marked: exists(filepath.Join(dir, sendMarkerFile)),
		})
	}
	sort.Slice(sessions, func(i, j int) bool { return sessions[i].ID < sessions[j].ID })
	return sessions, nil
}

func exists(path string) bool {
	_, err := os.Lstat(path)
	return err == nil
}

func isRegular(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular()
}

// transcriptLine holds the fields push reads from a Claude Code transcript line.
type transcriptLine struct {
	Cwd       string `json:"cwd"`
	Version   string `json:"version"`
	Timestamp string `json:"timestamp"`
}

// readTranscriptMeta fills Folder (the first cwd), AppVersion, StartedAt
// and LastAt from the transcript lines.
func readTranscriptMeta(s *outboxSession, data []byte) {
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	scanner.Buffer(make([]byte, 0, 64*1024), len(data)+1)
	for scanner.Scan() {
		var line transcriptLine
		if json.Unmarshal(scanner.Bytes(), &line) != nil {
			continue
		}
		if s.Folder == "" && line.Cwd != "" {
			s.Folder = line.Cwd
		}
		if s.AppVersion == "" && line.Version != "" && len(line.Version) <= 64 {
			s.AppVersion = line.Version
		}
		if t, err := time.Parse(time.RFC3339Nano, line.Timestamp); err == nil {
			if s.StartedAt == nil {
				start := t
				s.StartedAt = &start
			}
			last := t
			s.LastAt = &last
		}
	}
}

// readSessionFiles returns the files to upload, by their path in the
// archive: the transcript and every regular file in the session's folder.
// Links and other special files are left out.
func readSessionFiles(s outboxSession) (map[string][]byte, error) {
	files := map[string][]byte{}
	transcript := s.ID + ".jsonl"
	data, err := os.ReadFile(filepath.Join(s.Dir, transcript))
	if err != nil {
		return nil, err
	}
	files[transcript] = data
	root := filepath.Join(s.Dir, s.ID)
	info, err := os.Lstat(root)
	if errors.Is(err, fs.ErrNotExist) || (err == nil && !info.IsDir()) {
		return files, nil
	}
	if err != nil {
		return nil, err
	}
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, werr error) error {
		if werr != nil {
			return werr
		}
		if !d.Type().IsRegular() {
			return nil
		}
		rel, rerr := filepath.Rel(s.Dir, path)
		if rerr != nil {
			return rerr
		}
		content, rerr := os.ReadFile(path)
		if rerr != nil {
			return rerr
		}
		files[filepath.ToSlash(rel)] = content
		return nil
	})
	if err != nil {
		return nil, err
	}
	return files, nil
}

// wholeLines cuts data after its last newline: the last line of an open
// session may still be in writing. An ended session sends all bytes.
func wholeLines(data []byte, ended bool) []byte {
	if ended {
		return data
	}
	cut := strings.LastIndexByte(string(data), '\n')
	return data[:cut+1]
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// receiptFileState is one file of a receipt: how many bytes of the local
// file were sent (the offset of the next tail) and their SHA-256 hash.
type receiptFileState struct {
	Offset int    `json:"offset"`
	SHA256 string `json:"sha256"`
}

// receipt is the local record of the last upload of one session.
type receipt struct {
	SessionID string                      `json:"session_id"`
	Project   string                      `json:"project"`
	State     string                      `json:"state"`
	Backplane string                      `json:"backplane"`
	Tenant    string                      `json:"tenant,omitempty"`
	SentAt    time.Time                   `json:"sent_at"`
	Files     map[string]receiptFileState `json:"files"`
	Server    json.RawMessage             `json:"server"`
}

// loadReceipt returns the session's receipt, or nil when it has none or
// it cannot be read (then the session is sent again; a retry is safe).
func loadReceipt(dir string) *receipt {
	raw, err := os.ReadFile(filepath.Join(dir, receiptFile))
	if err != nil {
		return nil
	}
	var r receipt
	if json.Unmarshal(raw, &r) != nil {
		return nil
	}
	return &r
}

// writeReceipt writes the receipt to a temporary file and renames it, so a
// cut-off write never leaves half a receipt.
func writeReceipt(dir string, r receipt) error {
	raw, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, receiptFile+".*")
	if err != nil {
		return err
	}
	if _, err := tmp.Write(append(raw, '\n')); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmp.Name())
		return err
	}
	return os.Rename(tmp.Name(), filepath.Join(dir, receiptFile))
}

// sameAsReceipt says whether the receipt already covers these file states
// and this state at this destination: then there is nothing new to send.
func sameAsReceipt(r *receipt, dest destination, states map[string]receiptFileState, state string) bool {
	if r == nil || dest.Backplane == "" || r.Backplane != dest.Backplane || r.Tenant != dest.Tenant ||
		r.State != state || len(r.Files) != len(states) {
		return false
	}
	for path, st := range states {
		if r.Files[path] != st {
			return false
		}
	}
	return true
}
