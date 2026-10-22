// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Command meho is the operator-facing CLI for the MEHO governance
// backplane. v0.1 ships three subcommands — login, status, version —
// per Goal #11. This entry point only wires the cobra tree and
// surfaces its exit code; everything else lives under internal/cmd.
package main

import (
	"errors"
	"os"

	"github.com/evoila/meho/cli/internal/cmd"
	"github.com/evoila/meho/cli/internal/output"
)

func main() {
	err := cmd.Execute()
	if err == nil {
		return
	}
	// Honor structured exit codes (auth_expired → 2, unreachable →
	// 3, unexpected → 4) when the subcommand returned an
	// ExitCoder. The error message is already on stderr: cobra's
	// printer, the command's own RenderError, or the usage_error
	// path for an unknown flag or a wrong argument count. Our only
	// remaining job is to propagate the right process exit code so
	// consumers (install.sh smoke tests, CI gates) can branch on it.
	var coder output.ExitCoder
	if errors.As(err, &coder) {
		os.Exit(coder.ExitCode())
	}
	os.Exit(1)
}
