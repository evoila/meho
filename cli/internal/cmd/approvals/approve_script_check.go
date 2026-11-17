// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"golang.org/x/term"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// scriptQuestion is the question before a one-id approve of a script.
const scriptQuestion = "Type yes, or the script's SHA-256, to approve. Anything else stops: "

// inputIsTerminal reports whether a person is at the keyboard. Tests
// override it.
var inputIsTerminal = func(in io.Reader) bool {
	f, ok := in.(*os.File)
	return ok && term.IsTerminal(int(f.Fd()))
}

// scriptSHA256 returns the SHA-256 that MEHO shows for the script of a
// request, or "" when it shows none.
func scriptSHA256(preview map[string]interface{}) string {
	sha, _ := preview["script_sha256"].(string)
	return strings.TrimSpace(sha)
}

// printScriptBlock prints the short script block of a one-id approve: the
// size, the SHA-256 (or why there is none), the reviewed copy, the check
// result, and where to read the whole script. It does not print the text.
func printScriptBlock(w io.Writer, idArg string, preview map[string]interface{}) {
	fmt.Fprintln(w, "\nThis request runs a script.")
	if size, ok := preview["script_bytes"].(float64); ok {
		fmt.Fprintf(w, "  Size:             %d bytes\n", int64(size))
	}
	if sha := scriptSHA256(preview); sha != "" {
		fmt.Fprintf(w, "  SHA-256:          %s\n", output.SafeText(sha))
	} else if hidden, _ := preview["script_values_hidden"].(bool); hidden {
		fmt.Fprintln(w, "  SHA-256:          none. Values that look like secrets were hidden, so MEHO shows no SHA-256.")
	} else if reason, _ := preview["script_unavailable"].(string); reason != "" {
		fmt.Fprintf(w, "  SHA-256:          none. MEHO could not check the script: %s\n", output.SafeText(reason))
	} else {
		fmt.Fprintln(w, "  SHA-256:          none. MEHO shows no SHA-256 for this script.")
	}
	if ref, _ := preview["script_ref"].(string); ref != "" {
		fmt.Fprintf(w, "  Reviewed copy:    %s\n", output.SafeText(ref))
	}
	if check, _ := preview["script_check"].(string); check != "" {
		fmt.Fprintf(w, "  Expected SHA-256: %s\n", output.SafeText(check))
	}
	fmt.Fprintf(w, "  Read the whole script: `meho approvals show %s`, or the approvals page of the console.\n", idArg)
}

// confirmScript shows the script block of a one-id approve and asks. It
// returns "" when the person may approve, or the plain reason why not.
// From a terminal, "yes" approves. The script's SHA-256 approves from a
// terminal or a pipe, when it matches. Anything else stops.
func confirmScript(cmd *cobra.Command, idArg string, detail *api.ApprovalRequestView) string {
	preview := scriptPreview(detail.ProposedEffect)
	if preview == nil {
		return ""
	}
	w := cmd.ErrOrStderr()
	printScriptBlock(w, idArg, preview)
	in := cmd.InOrStdin()
	fmt.Fprint(w, scriptQuestion)
	line, _ := bufio.NewReader(in).ReadString('\n')
	answer := strings.TrimSpace(line)
	fmt.Fprintln(w)
	sha := scriptSHA256(preview)
	switch {
	case sha != "" && strings.EqualFold(answer, sha):
		return ""
	case sha != "" && len(answer) == len(sha) && isHex(answer):
		return "That SHA-256 does not match the script of this request. Nothing was approved."
	case strings.EqualFold(answer, "yes") && inputIsTerminal(in):
		return ""
	case strings.EqualFold(answer, "yes"):
		if sha == "" {
			return "Nothing was approved: this script has no SHA-256, and yes is only taken from a terminal."
		}
		return "Nothing was approved: yes is only taken from a terminal. On a pipe, give the script's SHA-256."
	default:
		return "Nothing was approved: you did not type yes or the script's SHA-256."
	}
}

func isHex(s string) bool {
	for _, c := range s {
		if !strings.ContainsRune("0123456789abcdefABCDEF", c) {
			return false
		}
	}
	return true
}
