// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"fmt"
	"io"
	"regexp"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// Check results. A check that passes is "ok"; "warn" means it works but
// something looks off; "fail" means this layer is broken; "skip" means
// the check could not run (for example the plugin is not installed).
const (
	doctorOK   = "ok"
	doctorWarn = "warn"
	doctorFail = "fail"
	doctorSkip = "skip"
)

// The layers the doctor checks. The server layer appears only on a
// Linux server of install form 2, where meho-host is installed.
const (
	layerOffline = "offline"
	layerAuthed  = "authenticated"
	layerServer  = "server"
)

// Failure classes for the authenticated layer. The network classes
// (dns, tls, unreachable) come from internal/netdiag.
const (
	classNotLoggedIn     = "not_logged_in"
	classRefreshRejected = "refresh_rejected"
	classNoRefreshToken  = "no_refresh_token"
	classUnauthorized    = "unauthorized"
	classSurfaceGap      = "surface_gap"
	classUnexpected      = "unexpected"
)

// doctorCheck is one line of the doctor report.
type doctorCheck struct {
	Layer  string `json:"layer"`
	Name   string `json:"name"`
	Status string `json:"status"`
	Class  string `json:"class,omitempty"`
	Detail string `json:"detail"`
	Hint   string `json:"hint,omitempty"`
}

// doctorSurface is the expected-vs-actual working-surface summary.
type doctorSurface struct {
	// Listed is the number of tools the client saw in tools/list.
	Listed int `json:"listed"`
	// Expected are the working-surface tools the server says this
	// session should list (from meho_status).
	Expected []string `json:"expected"`
	// Gap are tools the server expects but the client did not see.
	Gap []string `json:"gap"`
	// HiddenByServer are working-surface tools the server hides from
	// this session (role, capability, or add-on not paired).
	HiddenByServer []string `json:"hidden_by_server,omitempty"`
	// OperatorSurface is true when the session also sees the
	// operator planes (mcp:admin).
	OperatorSurface bool `json:"operator_surface"`
}

// doctorReport is the whole doctor result. It never carries a token.
type doctorReport struct {
	Checks        []doctorCheck  `json:"checks"`
	GrantedScopes []string       `json:"granted_scopes,omitempty"`
	Surface       *doctorSurface `json:"surface,omitempty"`
	// MCPSessionID is the Mcp-Session-Id the server gave the doctor's
	// own handshake. It is a correlation id for the audit log, not a
	// credential.
	MCPSessionID string `json:"mcp_session_id,omitempty"`
}

func (r *doctorReport) add(c doctorCheck) { r.Checks = append(r.Checks, c) }

// doctorOptions carries the flag values.
type doctorOptions struct {
	jsonOut           bool
	backplaneOverride string
	pluginRoot        string
	noNetwork         bool
}

// newDoctorCmd returns the `meho doctor` subcommand.
func newDoctorCmd() *cobra.Command {
	var opts doctorOptions
	cmd := &cobra.Command{
		Use:   "doctor",
		Short: "Check this machine's MEHO client setup and say which layer is broken",
		Long: "doctor checks the MEHO client setup on this machine in layers " +
			"and tells you which layer fails. It only reports. It never logs you in " +
			"again, re-installs anything, or changes a file.\n\n" +
			"Offline layer (no network): the meho binary path, the Claude Code " +
			"plugin shim, the vendored mcp-remote tree against the committed lock " +
			"file, the mcp-remote cache, where the plugin reads its settings from, " +
			"the CA bundle, which credential store holds your login (OS keyring or " +
			"the 0600 file), and whether the saved backplane URL parses.\n\n" +
			"Authenticated layer: resolves the backplane name, checks your token " +
			"(and refreshes it the normal way), then runs the MCP handshake " +
			"(initialize, resources/list, tools/list, meho_status). A failure is " +
			"sorted into one bucket: dns, tls, unreachable, refresh_rejected, " +
			"no_refresh_token, not_logged_in, unauthorized, surface_gap, or " +
			"unexpected. It prints the OAuth scopes your CLI session holds and " +
			"compares the working-surface tools the server expects with the ones " +
			"the handshake listed.\n\n" +
			"Note: the scopes shown are the CLI login's scopes. A Claude client " +
			"logs in on its own (through mcp-remote) and can hold other scopes; " +
			"call meho_status from that client to see them. For a deeper look at " +
			"a Claude client login, run mcp-remote with --debug.\n\n" +
			"Server layer (only on a Linux server of install form 2, where " +
			mehoHostPath + " exists): as root, doctor runs `meho-host check --json` " +
			"with a 60-second limit and shows its lines after its own: the node, the " +
			"pods, the disk and the certificates of the built-in Kubernetes (RKE2). " +
			"As another user it shows one warning and runs nothing; run " +
			"`sudo meho doctor`. With --no-network this layer is skipped.\n\n" +
			"The output never contains a token, an OAuth code, a cookie, or a " +
			"redirect URL. Guides: https://evoila.github.io/meho/clients/troubleshooting/\n\n" +
			"Exit codes: 0 no failure, 1 an offline or server check failed, 2 login problem " +
			"(not logged in, refresh rejected, token refused), 3 network problem " +
			"(dns, tls, unreachable), 4 unexpected answer or a surface gap.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			report := runDoctor(cmd, opts, defaultDoctorDeps())
			return renderDoctor(cmd.OutOrStdout(), report, opts.jsonOut)
		},
	}
	cmd.Flags().BoolVar(&opts.jsonOut, "json", false,
		"emit one JSON document on stdout instead of the human report")
	cmd.Flags().StringVar(&opts.backplaneOverride, "backplane", "",
		"backplane URL to check (defaults to the URL saved by the last `meho login`)")
	cmd.Flags().StringVar(&opts.pluginRoot, "plugin-root", "",
		"path to the installed Claude Code plugin (defaults to $CLAUDE_PLUGIN_ROOT, then a search under ~/.claude/plugins)")
	cmd.Flags().BoolVar(&opts.noNetwork, "no-network", false,
		"run only the offline layer; make no network call")
	return cmd
}

// runDoctor runs the layers and returns the report. The server layer
// comes last, after the doctor's own checks.
func runDoctor(cmd *cobra.Command, opts doctorOptions, deps doctorDeps) *doctorReport {
	report := &doctorReport{}
	backplaneURL := runOfflineChecks(report, opts, deps)
	switch {
	case opts.noNetwork:
		report.add(doctorCheck{Layer: layerAuthed, Name: "authenticated layer", Status: doctorSkip,
			Detail: "skipped (--no-network)"})
	case backplaneURL == "":
		report.add(doctorCheck{Layer: layerAuthed, Name: "authenticated layer", Status: doctorSkip,
			Detail: "skipped: no usable backplane URL", Hint: "run `meho login <url>` or pass --backplane <url>"})
	default:
		runAuthenticatedChecks(cmd.Context(), report, backplaneURL, deps)
	}
	runServerChecks(cmd.Context(), report, opts, deps)
	return report
}

// renderDoctor prints the report (redacted) and returns an error that
// carries the exit code when a check failed.
func renderDoctor(w io.Writer, report *doctorReport, jsonOut bool) error {
	safe := report.redacted()
	if jsonOut {
		if err := output.PrintJSON(w, safe); err != nil {
			return err
		}
	} else {
		printDoctorHuman(w, safe)
	}
	if code := doctorExitCode(safe); code != 0 {
		return &doctorExit{code: code}
	}
	return nil
}

// printDoctorHuman prints the checks layer by layer. The server lines
// come last, after the summary of the authenticated layer, so the
// doctor's own output comes first.
func printDoctorHuman(w io.Writer, r *doctorReport) {
	var serverLines []doctorCheck
	layer := ""
	for _, c := range r.Checks {
		if c.Layer == layerServer {
			serverLines = append(serverLines, c)
			continue
		}
		layer = printDoctorLine(w, c, layer)
	}
	printDoctorSummary(w, r)
	for _, c := range serverLines {
		layer = printDoctorLine(w, c, layer)
	}
}

// printDoctorLine prints one check, with a layer heading first when the
// layer changes, and returns the layer of the check.
func printDoctorLine(w io.Writer, c doctorCheck, layer string) string {
	if c.Layer != layer {
		fmt.Fprintf(w, "%s layer\n", strings.ToUpper(c.Layer[:1])+c.Layer[1:])
	}
	line := fmt.Sprintf("  [%-4s] %-18s %s", c.Status, c.Name, c.Detail)
	if c.Class != "" {
		line += " (" + c.Class + ")"
	}
	fmt.Fprintln(w, line)
	if c.Hint != "" {
		fmt.Fprintf(w, "         -> %s\n", c.Hint)
	}
	return c.Layer
}

// printDoctorSummary prints the scopes, the working surface and the MCP
// session id of the authenticated layer.
func printDoctorSummary(w io.Writer, r *doctorReport) {
	if len(r.GrantedScopes) > 0 {
		fmt.Fprintf(w, "Granted scopes (CLI session): %s\n", strings.Join(r.GrantedScopes, " "))
	}
	if s := r.Surface; s != nil {
		fmt.Fprintf(w, "Working surface: server expects %d tools for this session, handshake listed %d tools\n",
			len(s.Expected), s.Listed)
		if len(s.Gap) > 0 {
			fmt.Fprintf(w, "  missing from tools/list: %s\n", strings.Join(s.Gap, ", "))
		}
		if len(s.HiddenByServer) > 0 {
			fmt.Fprintf(w, "  hidden by the server for this session: %s\n", strings.Join(s.HiddenByServer, ", "))
		}
		if s.OperatorSurface {
			fmt.Fprintln(w, "  operator planes: listed (mcp:admin)")
		}
	}
	if r.MCPSessionID != "" {
		fmt.Fprintf(w, "MCP session id (for the audit log): %s\n", r.MCPSessionID)
	}
}

// doctorExitCode maps the first failed check to an exit code.
func doctorExitCode(r *doctorReport) int {
	for _, c := range r.Checks {
		if c.Status != doctorFail {
			continue
		}
		switch c.Class {
		case classNotLoggedIn, classRefreshRejected, classNoRefreshToken, classUnauthorized:
			return output.ExitAuthExpired
		case "dns", "tls", "unreachable":
			return output.ExitUnreachable
		case classSurfaceGap, classUnexpected, "other":
			return output.ExitUnexpected
		}
		return 1
	}
	return 0
}

// doctorExit carries the exit code without a message: the report was
// already printed.
type doctorExit struct{ code int }

func (e *doctorExit) Error() string { return "" }
func (e *doctorExit) ExitCode() int { return e.code }

// redacted returns a copy of the report with every free-text field
// passed through redactSensitive. This is the single choke point: no
// string reaches the terminal without it.
func (r *doctorReport) redacted() *doctorReport {
	out := &doctorReport{
		GrantedScopes: append([]string(nil), r.GrantedScopes...),
		MCPSessionID:  redactSensitive(r.MCPSessionID),
	}
	for _, c := range r.Checks {
		c.Detail = redactSensitive(c.Detail)
		c.Hint = redactSensitive(c.Hint)
		out.Checks = append(out.Checks, c)
	}
	if r.Surface != nil {
		s := *r.Surface
		out.Surface = &s
	}
	return out
}

// Patterns for secrets that can show up in error text: HTTP headers that
// carry credentials, OAuth parameters (as query parameters and as JSON
// members, because a failed token exchange echoes the response body),
// and redirect targets.
var (
	redactHeaderRe = regexp.MustCompile(`(?i)\b(proxy-authorization|authorization|set-cookie|cookie)\s*[:=]\s*[^\r\n]*`)
	redactLocRe    = regexp.MustCompile(`(?i)\blocation\s*:\s*\S+`)
	redactBearerRe = regexp.MustCompile(`(?i)\bbearer\s+\S+`)
	redactParamRe  = regexp.MustCompile(`(?i)\b(code|device_code|user_code|access_token|refresh_token|id_token|token|state|redirect_uri|client_secret|session_state)=[^&\s"']+`)
	redactJSONRe   = regexp.MustCompile(`(?i)"(code|device_code|user_code|access_token|refresh_token|id_token|token|state|redirect_uri|client_secret|session_state|password|cookie|authorization)"\s*:\s*"(?:[^"\\]|\\.)*"`)
	redactQueryRe  = regexp.MustCompile(`(https?://[^\s?#"']+)[?#][^\s"']*`)
)

// redactSensitive extends redactJWTLike (status.go): besides JWT-shaped
// strings it removes credential headers, bearer values, OAuth parameters
// (code=, state=, redirect_uri=, ... and the same names as JSON members,
// for example {"access_token":"..."}), redirect Location values, and the
// query part of any URL.
func redactSensitive(s string) string {
	if s == "" {
		return s
	}
	s = redactHeaderRe.ReplaceAllString(s, "$1: [redacted]")
	s = redactLocRe.ReplaceAllString(s, "Location: [redacted-url]")
	s = redactBearerRe.ReplaceAllString(s, "Bearer [redacted]")
	s = redactParamRe.ReplaceAllString(s, "$1=[redacted]")
	s = redactJSONRe.ReplaceAllString(s, `"$1":"[redacted]"`)
	s = redactQueryRe.ReplaceAllString(s, "$1?[redacted]")
	return redactJWTLike(s)
}
