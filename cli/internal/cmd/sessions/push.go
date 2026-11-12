// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"fmt"
	"mime/multipart"
	"net/http"
	"sort"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

type pushOptions struct {
	Outbox            string
	DryRun            bool
	JSONOut           bool
	BackplaneOverride string
}

func newPushCmd() *cobra.Command {
	var opts pushOptions
	cmd := &cobra.Command{
		Use:   "push",
		Short: "Upload the agent sessions in your local outbox (POST /api/v1/sessions)",
		Long: "push reads the local session outbox and uploads each session to MEHO " +
			"with your `meho login`. It sends only sessions that were started in a " +
			"project folder on your allowlist, or that you marked by hand. Before a " +
			"session leaves the laptop, known secrets are replaced with a marker. " +
			"When the check is not sure, the session stays in the outbox and push " +
			"says why. A receipt next to each session records what was sent, so a " +
			"second run sends only what is new. --dry-run shows the list and sends " +
			"nothing. Settings: " + settingsFile + " in the meho config folder.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runPush(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&opts.Outbox, "outbox", "", "outbox folder (defaults to the setting, then <meho config folder>/session-outbox)")
	cmd.Flags().BoolVar(&opts.DryRun, "dry-run", false, "show what would be sent, and send nothing")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false, "print the result as JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by `meho login`)")
	return cmd
}

// Results of one session in the push report.
const (
	resultSent        = "sent"
	resultWouldSend   = "would send"
	resultAlreadySent = "already sent"
	resultNotAllowed  = "not on allowlist"
	resultHeldBack    = "held back"
	resultNothingYet  = "nothing to send yet"
	resultFailed      = "failed"
	reasonNotAllowed  = "the project folder is not on the allowlist; the session stays local"
	reasonNoFolder    = "the transcript names no project folder; the session stays local"
	reasonNoWholeLine = "the open session has no complete line yet"
)

// pushRow is one session in the push report.
type pushRow struct {
	SessionID string                   `json:"session_id"`
	Project   string                   `json:"project"`
	State     string                   `json:"state"`
	Bytes     int                      `json:"bytes"`
	Result    string                   `json:"result"`
	Reason    string                   `json:"reason,omitempty"`
	Server    *api.SessionUploadResult `json:"server,omitempty"`
}

type pushReport struct {
	Outbox   string    `json:"outbox"`
	DryRun   bool      `json:"dry_run"`
	Sessions []pushRow `json:"sessions"`
}

// preparedSession is a session that passed the allowlist and the secret check.
type preparedSession struct {
	session outboxSession
	states  map[string]receiptFileState
	archive []byte
}

func runPush(cmd *cobra.Command, opts pushOptions) error {
	settings, _, err := loadPushSettings()
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Usage(err.Error()), opts.JSONOut)
	}
	if opts.Outbox != "" {
		settings.Outbox = expandHome(opts.Outbox)
	}
	sessions, err := listOutbox(settings.Outbox)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	report := pushReport{Outbox: settings.Outbox, DryRun: opts.DryRun, Sessions: []pushRow{}}
	var toSend []preparedSession
	var toSendRows []int
	for _, s := range sessions {
		row, prepared := prepareSession(s, settings)
		report.Sessions = append(report.Sessions, row)
		if prepared != nil {
			toSend = append(toSend, *prepared)
			toSendRows = append(toSendRows, len(report.Sessions)-1)
		}
	}
	failed := 0
	if len(toSend) > 0 && !opts.DryRun {
		failed, err = uploadAll(cmd, opts, toSend, toSendRows, report.Sessions)
		if err != nil {
			return err
		}
	}
	if opts.JSONOut {
		if perr := output.PrintJSON(cmd.OutOrStdout(), report); perr != nil {
			return perr
		}
	} else {
		printPush(cmd, report)
	}
	if failed > 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("%d session(s) failed to upload; run push again to retry", failed)),
			opts.JSONOut)
	}
	return nil
}

// prepareSession decides what to do with one session. It returns the
// prepared upload when the session has something to send.
func prepareSession(s outboxSession, settings pushSettings) (pushRow, *preparedSession) {
	row := pushRow{SessionID: s.ID, State: s.State()}
	files, err := readSessionFiles(s)
	if err != nil {
		row.Result, row.Reason = resultFailed, fmt.Sprintf("read the session files: %v", err)
		return row, nil
	}
	readTranscriptMeta(&s, files[s.ID+".jsonl"])
	row.Project = s.Project()
	switch {
	case s.Folder == "":
		row.Result, row.Reason = resultNotAllowed, reasonNoFolder
		return row, nil
	case !s.Marked && !settings.allowed(s.Folder):
		row.Result, row.Reason = resultNotAllowed, reasonNotAllowed
		return row, nil
	}
	states := map[string]receiptFileState{}
	checked := map[string][]byte{}
	var unsure []string
	whole := 0
	for _, path := range sortedKeys(files) {
		part := wholeLines(files[path], s.Ended)
		whole += len(part)
		states[path] = receiptFileState{Offset: len(part), SHA256: sha256Hex(part)}
		result := checkFile(part)
		for _, reason := range result.Unsure {
			unsure = append(unsure, path+": "+reason)
		}
		checked[path] = result.Content
	}
	if sameAsReceipt(loadReceipt(s.Dir), states, s.State()) {
		row.Result = resultAlreadySent
		return row, nil
	}
	if whole == 0 && !s.Ended {
		row.Result, row.Reason = resultNothingYet, reasonNoWholeLine
		return row, nil
	}
	if len(unsure) > 0 {
		row.Result = resultHeldBack
		row.Reason = "the secret check is not sure: " + strings.Join(unsure, "; ") +
			". Check the file in the outbox, then run push again"
		return row, nil
	}
	archive, err := gzipTar(checked)
	if err != nil {
		row.Result, row.Reason = resultFailed, fmt.Sprintf("pack the session: %v", err)
		return row, nil
	}
	row.Bytes = len(archive)
	row.Result = resultWouldSend
	return row, &preparedSession{session: s, states: states, archive: archive}
}

func sortedKeys(m map[string][]byte) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// gzipTar packs files into one gzip tar archive, in path order.
func gzipTar(files map[string][]byte) ([]byte, error) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, path := range sortedKeys(files) {
		content := files[path]
		hdr := &tar.Header{Name: path, Mode: 0o600, Size: int64(len(content)), Typeflag: tar.TypeReg}
		if err := tw.WriteHeader(hdr); err != nil {
			return nil, err
		}
		if _, err := tw.Write(content); err != nil {
			return nil, err
		}
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	if err := gz.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// uploadAll sends each prepared session and writes its receipt. It returns
// how many uploads failed. A failed upload writes no receipt, so the next
// run sends the session again.
func uploadAll(cmd *cobra.Command, opts pushOptions, toSend []preparedSession, rowIdx []int, rows []pushRow) (int, error) {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return 0, output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return 0, renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	failed := 0
	for i, p := range toSend {
		row := &rows[rowIdx[i]]
		result, raw, uerr := uploadOne(cmd.Context(), authed, p)
		if uerr != nil {
			row.Result, row.Reason = resultFailed, uerr.Error()
			failed++
			continue
		}
		row.Result, row.Server = resultSent, result
		r := receipt{
			SessionID: p.session.ID, Project: p.session.Project(), State: p.session.State(),
			Backplane: backplaneURL, SentAt: time.Now().UTC(), Files: p.states, Server: raw,
		}
		if werr := writeReceipt(p.session.Dir, r); werr != nil {
			// The upload worked; the next run sends the same bytes again,
			// which changes nothing on the server.
			row.Reason = fmt.Sprintf("uploaded, but the receipt was not written: %v", werr)
		}
	}
	return failed, nil
}

func uploadOne(ctx context.Context, authed *api.AuthedClient, p preparedSession) (*api.SessionUploadResult, []byte, error) {
	body, contentType, err := multipartBody(p)
	if err != nil {
		return nil, nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.UploadAgentSessionApiV1SessionsPostResponse, error) {
			return authed.UploadAgentSessionApiV1SessionsPostWithBodyWithResponse(
				ctx, nil, contentType, bytes.NewReader(body))
		},
		func(r *api.UploadAgentSessionApiV1SessionsPostResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, nil, fmt.Errorf("upload: %v", err)
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return nil, nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode(), decodeDetail(resp.Body))
	}
	return resp.JSON200, resp.Body, nil
}

func multipartBody(p preparedSession) ([]byte, string, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	s := p.session
	fields := [][2]string{
		{"session_id", s.ID},
		{"project", s.Project()},
		{"app", appClaudeCode},
		{"state", s.State()},
	}
	if s.AppVersion != "" {
		fields = append(fields, [2]string{"app_version", s.AppVersion})
	}
	if s.StartedAt != nil {
		fields = append(fields, [2]string{"started_at", s.StartedAt.Format(time.RFC3339Nano)})
	}
	if s.Ended && s.LastAt != nil {
		fields = append(fields, [2]string{"ended_at", s.LastAt.Format(time.RFC3339Nano)})
	}
	for _, f := range fields {
		if err := w.WriteField(f[0], f[1]); err != nil {
			return nil, "", err
		}
	}
	part, err := w.CreateFormFile("archive", s.ID+".tar.gz")
	if err != nil {
		return nil, "", err
	}
	if _, err := part.Write(p.archive); err != nil {
		return nil, "", err
	}
	if err := w.Close(); err != nil {
		return nil, "", err
	}
	return buf.Bytes(), w.FormDataContentType(), nil
}

func printPush(cmd *cobra.Command, report pushReport) {
	out := cmd.OutOrStdout()
	if len(report.Sessions) == 0 {
		fmt.Fprintf(out, "no sessions in the outbox %s\n", report.Outbox)
		return
	}
	tw := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "SESSION\tPROJECT\tSTATE\tSIZE\tRESULT\tSERVER")
	for _, r := range report.Sessions {
		server := "-"
		if r.Server != nil {
			server = fmt.Sprintf("%s, %d new bytes", r.Server.Outcome, r.Server.NewBytes)
			if r.Server.Quarantined {
				server += ", in quarantine"
			}
		}
		project := r.Project
		if project == "" {
			project = "-"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%d\t%s\t%s\n",
			output.SafeText(r.SessionID), output.SafeText(project), r.State, r.Bytes, r.Result, server)
	}
	_ = tw.Flush()
	for _, r := range report.Sessions {
		if r.Reason != "" {
			fmt.Fprintf(out, "%s: %s\n", output.SafeText(r.SessionID), output.SafeText(r.Reason))
		}
	}
	if report.DryRun {
		fmt.Fprintln(out, "dry run: nothing was sent")
	}
}
