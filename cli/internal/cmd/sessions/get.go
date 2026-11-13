// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

func newGetCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get <session-id> <folder>",
		Short: "Download one stored agent session as whole files",
		Long: "get downloads every upload of one session (GET /api/v1/sessions/{session_id}/uploads/{seq}), " +
			"joins the parts of each file in upload order, and writes the whole (checked) " +
			"files under <folder>/<session-id>/, with the paths of the archive. " +
			"Every archive is checked first: an unsafe path stops the command before any file is written. " +
			"An upload in quarantine is skipped when you may not open it (only a tenant_admin may); " +
			"a file that misses such an upload is written up to that upload and marked incomplete. " +
			"Needs the operator role. Every download is audited.",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runGet(cmd, args[0], args[1], jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "print what was written as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by `meho login`)")
	return cmd
}

// getResult is what --json prints.
type getResult struct {
	SessionID string          `json:"session_id"`
	Folder    string          `json:"folder"`
	Files     []writtenFile   `json:"files"`
	Skipped   []skippedUpload `json:"skipped_uploads"`
}

type writtenFile struct {
	Path     string `json:"path"`
	Bytes    int    `json:"bytes"`
	Written  bool   `json:"written"`
	Complete bool   `json:"complete"`
	Missing  []int  `json:"missing_uploads"`
}

type skippedUpload struct {
	Seq    int    `json:"seq"`
	Reason string `json:"reason"`
}

// getError is a failure of `get` that is not an HTTP answer.
func getError(cmd *cobra.Command, jsonOut bool, format string, args ...any) error {
	return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf(format, args...)), jsonOut)
}

func runGet(cmd *cobra.Command, sessionID, folder string, jsonOut bool, backplaneOverride string) error {
	if !sessionIDPattern.MatchString(sessionID) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Usage("the session id may hold only letters, digits, '.', '_' and '-'"), jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	shown, err := retryOn401(cmd.Context(), authed,
		func(ctx context.Context) (*api.ShowAgentSessionApiV1SessionsSessionIdGetResponse, error) {
			return authed.ShowAgentSessionApiV1SessionsSessionIdGetWithResponse(ctx, sessionID, nil)
		},
		func(r *api.ShowAgentSessionApiV1SessionsSessionIdGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if shown.StatusCode() != http.StatusOK || shown.JSON200 == nil {
		return renderHTTPStatus(cmd, backplaneURL, shown.StatusCode(), shown.Body, jsonOut)
	}

	result := getResult{SessionID: sessionID, Files: []writtenFile{}, Skipped: []skippedUpload{}}
	parts := make([]storedPart, 0, len(shown.JSON200.Uploads))
	for _, upload := range shown.JSON200.Uploads {
		resp, err := retryOn401(cmd.Context(), authed,
			func(ctx context.Context) (*api.DownloadAgentSessionUploadApiV1SessionsSessionIdUploadsSeqGetResponse, error) {
				return authed.DownloadAgentSessionUploadApiV1SessionsSessionIdUploadsSeqGetWithResponse(
					ctx, sessionID, upload.Seq, nil)
			},
			func(r *api.DownloadAgentSessionUploadApiV1SessionsSessionIdUploadsSeqGetResponse) int {
				return r.StatusCode()
			},
		)
		if err != nil {
			return renderRequestError(cmd, backplaneURL, err, jsonOut)
		}
		if resp.StatusCode() == http.StatusForbidden && decodeDetail(resp.Body) == "session_upload_quarantined" {
			result.Skipped = append(result.Skipped, skippedUpload{Seq: upload.Seq, Reason: "quarantined"})
			parts = append(parts, storedPart{Upload: upload})
			continue
		}
		if resp.StatusCode() != http.StatusOK {
			return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
		}
		sum := sha256.Sum256(resp.Body)
		if hex.EncodeToString(sum[:]) != upload.ArchiveSha256 {
			return getError(cmd, jsonOut, "upload #%d: the downloaded archive does not have the stored hash", upload.Seq)
		}
		files, err := readStoredArchive(resp.Body)
		if err == nil {
			err = checkPartFiles(upload, files)
		}
		if err != nil {
			return getError(cmd, jsonOut, "upload #%d: %v; no file was written", upload.Seq, err)
		}
		parts = append(parts, storedPart{Upload: upload, Files: files})
	}
	whole, err := joinParts(parts)
	if err != nil {
		return getError(cmd, jsonOut, "%v; no file was written", err)
	}
	dir := filepath.Join(folder, sessionID)
	result.Folder = dir
	if result.Files, err = writeWholeFiles(dir, whole); err != nil {
		return getError(cmd, jsonOut, "write the files: %v", err)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printGet(cmd, result)
	return nil
}

// writeWholeFiles writes each file under dir. os.Root keeps every write
// inside dir, also through a link that is already on the disk. A file
// that misses an upload and has no bytes before it is not written.
func writeWholeFiles(dir string, whole map[string]*wholeFile) ([]writtenFile, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, err
	}
	defer func() { _ = root.Close() }()
	paths := make([]string, 0, len(whole))
	for path := range whole {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	written := make([]writtenFile, 0, len(paths))
	for _, path := range paths {
		file := whole[path]
		entry := writtenFile{
			Path: path, Bytes: len(file.Data), Complete: len(file.Missing) == 0, Missing: file.Missing,
		}
		if entry.Missing == nil {
			entry.Missing = []int{}
		}
		if entry.Complete || len(file.Data) > 0 {
			name := filepath.FromSlash(path)
			if err := root.MkdirAll(filepath.Dir(name), 0o700); err != nil {
				return nil, err
			}
			if err := root.WriteFile(name, file.Data, 0o600); err != nil {
				return nil, err
			}
			entry.Written = true
		}
		written = append(written, entry)
	}
	return written, nil
}

func printGet(cmd *cobra.Command, result getResult) {
	out := cmd.OutOrStdout()
	for _, s := range result.Skipped {
		fmt.Fprintf(out, "skipped upload #%d: it is in quarantine, and only a tenant_admin may open it\n", s.Seq)
	}
	count := 0
	for _, f := range result.Files {
		switch {
		case !f.Written:
			fmt.Fprintf(out, "not written: %s (its first part is in skipped upload #%d)\n", f.Path, f.Missing[0])
		case !f.Complete:
			count++
			fmt.Fprintf(out, "incomplete: %s (%d bytes, written up to skipped upload #%d)\n", f.Path, f.Bytes, f.Missing[0])
		default:
			count++
		}
	}
	fmt.Fprintf(out, "wrote %d files to %s\n", count, result.Folder)
}
