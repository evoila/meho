// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// firstStartTimeout bounds the GET /first-start call. The backplane runs
// its own checks (each with a short limit) before it answers.
const firstStartTimeout = 30 * time.Second

// firstStartLine is one line of the backplane's first-start list.
type firstStartLine struct {
	Name    string `json:"name"`
	Status  string `json:"status"`
	Message string `json:"message"`
}

// firstStartList is the answer of GET /first-start.
type firstStartList struct {
	OK     bool             `json:"ok"`
	Checks []firstStartLine `json:"checks"`
}

// runFirstStart implements `meho status --first-start`. The route needs
// no login, so a plain HTTP client works. With --json it prints the
// backplane's answer as it is. A red line makes the exit code 6.
func runFirstStart(cmd *cobra.Command, backplaneURL string, jsonOut bool) error {
	ctx, cancel := context.WithTimeout(cmd.Context(), firstStartTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, backplaneURL+"/first-start", http.NoBody)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	req.Header.Set("Accept", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, redactedError(err))), jsonOut)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d from %s/first-start (an older backplane has no first-start checks)", resp.StatusCode, backplaneURL)),
			jsonOut)
	}
	var list firstStartList
	if err := json.NewDecoder(io.LimitReader(resp.Body, 256*1024)).Decode(&list); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode /first-start: %v", err)), jsonOut)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), list); err != nil {
			return err
		}
	} else {
		printFirstStart(cmd.OutOrStdout(), list)
	}
	if !list.OK {
		return output.RenderError(cmd.ErrOrStderr(), &output.StructuredError{
			Code:   output.ErrCodeFirstStartFailed,
			Detail: "at least one first-start check is red",
			Exit:   output.ExitFirstStartFailed,
		}, jsonOut)
	}
	return nil
}

// printFirstStart writes one line per check: GREEN, RED or SKIPPED, the
// check name and its sentence.
func printFirstStart(w io.Writer, list firstStartList) {
	for _, line := range list.Checks {
		label := "GREEN"
		switch line.Status {
		case "fail":
			label = "RED"
		case "skipped":
			label = "SKIPPED"
		}
		fmt.Fprintf(w, "%-7s %-13s %s\n", label, line.Name, line.Message)
	}
}
