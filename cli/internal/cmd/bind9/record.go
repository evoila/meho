// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newRecordCmd returns the `meho bind9 record` parent command and
// assembles its three verbs (get / add / remove).
//
// The `record add` invocation is the 1:1 replacement for the
// consumer's `scripts/bind9-dns.sh --add-a-record ...` call (the
// 2026-05-04 / 2026-05-05 credential-leak surface — evoila-bosnia/
// claude-rdc-hetzner-dc#86); the verb shape matches the wrapper's
// positional / flag conventions.
func newRecordCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "record",
		Short:        "bind9 record verbs (get / add / remove)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRecordGetCmd())
	cmd.AddCommand(newRecordAddCmd())
	cmd.AddCommand(newRecordRemoveCmd())
	return cmd
}

// newRecordGetCmd returns `meho bind9 record get <fqdn> [--type T]`.
// Maps to op_id `bind9.record.get`. The `--type` flag defaults to
// A; other supported values are AAAA / CNAME / MX / TXT.
func newRecordGetCmd() *cobra.Command {
	var (
		targetName        string
		recordType        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get <fqdn>",
		Short: "Resolve an FQDN through the local bind9 (dig @localhost)",
		Long: "get dispatches bind9.record.get against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector. Resolution uses `dig @localhost`\n" +
			"so views, delegations, and cache hits behave as the rest of\n" +
			"the world sees them. --type defaults to A; AAAA / CNAME / MX /\n" +
			"TXT are the operator-relevant complement (other types ride\n" +
			"through `meho bind9 zone read`).\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho bind9 record get www.evba.lab --target vcf-router-bind9\n" +
			"  meho bind9 record get mail.evba.lab --type AAAA --target vcf-router-bind9",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRecordGet(cmd, args[0], recordType, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&recordType, "type", "",
		"record type (A / AAAA / CNAME / MX / TXT). Omitted → handler default (A).")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRecordGet(cmd *cobra.Command, fqdn, recordType, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"fqdn": fqdn}
	if recordType != "" {
		params["type"] = recordType
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.record.get", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.record.get", r, jsonOut, printRecordGet)
}

// printRecordGet renders the record get result. Same row shape as
// zone.read: `{name, ttl, class, type, rdata}`.
func printRecordGet(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s bind9.record.get — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	body, err := decodeFlatResult(r.Result)
	if err != nil || body == nil {
		fallbackResultRender(w, r)
		return
	}
	if fqdn, ok := body["fqdn"].(string); ok && fqdn != "" {
		fmt.Fprintf(w, "  fqdn: %s\n", fqdn)
	}
	if t, ok := body["type"].(string); ok && t != "" {
		fmt.Fprintf(w, "  type: %s\n", t)
	}
	rowsAny, ok := body["rows"].([]any)
	if !ok {
		fallbackResultRender(w, r)
		return
	}
	if len(rowsAny) == 0 {
		fmt.Fprintln(w, "  (0 records)")
		return
	}
	fmt.Fprintf(w, "%-40s %-6s %-5s %-7s %s\n", "name", "ttl", "class", "type", "rdata")
	for _, ra := range rowsAny {
		row, ok := ra.(map[string]any)
		if !ok {
			continue
		}
		fmt.Fprintf(w, "%-40s %-6d %-5s %-7s %s\n",
			truncate(stringField(row, "name"), 40),
			intField(row, "ttl"),
			stringField(row, "class"),
			stringField(row, "type"),
			stringField(row, "rdata"),
		)
	}
}

// newRecordAddCmd returns `meho bind9 record add <fqdn> <ip>`. Maps
// to op_id `bind9.record.add`. The write routes through the atomic-
// apply primitive in the backend — invalid input leaves /etc/bind/
// byte-identical to the pre-op snapshot. The `--zone` flag is
// optional; when omitted, the handler resolves the owning zone via
// longest-suffix match.
//
// This verb is the 1:1 replacement for
// `bind9-dns.sh --add-a-record <fqdn> <ip> --zone <zone>`.
func newRecordAddCmd() *cobra.Command {
	var (
		targetName        string
		zone              string
		recordType        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "add <fqdn> <ip>",
		Short: "Add an A/AAAA record (atomic-apply; rollback on failure)",
		Long: "add dispatches bind9.record.add against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector. Routes the write through the\n" +
			"atomic-apply primitive (#589): validate → stage → checkconf →\n" +
			"reload → dig-verify, with a rollback to the pre-op snapshot\n" +
			"if any step fails. Invalid input leaves /etc/bind/ byte-\n" +
			"identical to before the call.\n\n" +
			"--zone is optional; when omitted, the handler resolves the\n" +
			"owning zone via longest-suffix match against the active\n" +
			"`named-checkconf -p` output. --type accepts A or AAAA only\n" +
			"(CNAME / MX / TXT writes are out of scope for v0.2; the\n" +
			"consumer wrapper never wrote them either).\n\n" +
			"This verb is the 1:1 replacement for the consumer's\n" +
			"`bind9-dns.sh --add-a-record <fqdn> <ip> --zone <zone>`.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho bind9 record add esx-dc6.evba.lab 10.5.50.25 --zone evba.lab --target vcf-router-bind9\n" +
			"  meho bind9 record add v6.evba.lab 2001:db8::25 --type AAAA --target vcf-router-bind9",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRecordAdd(cmd, args[0], args[1], zone, recordType, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&zone, "zone", "",
		"owning zone (e.g. evba.lab). Omitted → handler resolves via longest-suffix match.")
	cmd.Flags().StringVar(&recordType, "type", "",
		"record type (A / AAAA). Omitted → handler default (A).")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRecordAdd(cmd *cobra.Command, fqdn, ip, zone, recordType, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{
		"fqdn": fqdn,
		"ip":   ip,
	}
	if zone != "" {
		params["zone"] = zone
	}
	if recordType != "" {
		params["type"] = recordType
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.record.add", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.record.add", r, jsonOut, printWriteResult)
}

// newRecordRemoveCmd returns `meho bind9 record remove <fqdn>`.
// Maps to op_id `bind9.record.remove`. Removes the A + AAAA
// rdatasets at <fqdn>; CNAME / MX / TXT are out of scope.
func newRecordRemoveCmd() *cobra.Command {
	var (
		targetName        string
		zone              string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "remove <fqdn>",
		Short: "Remove the A/AAAA records at an FQDN (atomic-apply)",
		Long: "remove dispatches bind9.record.remove against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector. Removes the A and AAAA rdatasets\n" +
			"at <fqdn> via the same atomic-apply primitive as record.add\n" +
			"— invalid input leaves /etc/bind/ byte-identical.\n\n" +
			"CNAME / MX / TXT removals are out of scope for v0.2 (the\n" +
			"consumer wrapper never removed them either; use\n" +
			"`meho bind9 config apply-file` for fine-grained zonefile\n" +
			"edits in the meantime).\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho bind9 record remove esx-dc6.evba.lab --zone evba.lab --target vcf-router-bind9",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRecordRemove(cmd, args[0], zone, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&zone, "zone", "",
		"owning zone (e.g. evba.lab). Omitted → handler resolves via longest-suffix match.")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRecordRemove(cmd *cobra.Command, fqdn, zone, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"fqdn": fqdn}
	if zone != "" {
		params["zone"] = zone
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.record.remove", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.record.remove", r, jsonOut, printWriteResult)
}

// printWriteResult renders the canonical write-op result envelope
// (`bind9.record.add`, `bind9.record.remove`, `bind9.config.apply_*`).
// Surfaces `op_class`, `zone` (when present), and the
// `result_state_before` / `result_state_after` summary that lets an
// operator confirm a successful atomic-apply landed the intended
// change. Full payload is on --json for diff-eyes inspection.
func printWriteResult(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, r.OpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	body, err := decodeFlatResult(r.Result)
	if err != nil || body == nil {
		fallbackResultRender(w, r)
		return
	}
	// op_class is "write" for atomic-apply ops.
	if opClass, ok := body["op_class"].(string); ok && opClass != "" {
		fmt.Fprintf(w, "  op_class: %s\n", opClass)
	}
	for _, key := range []string{"fqdn", "zone", "file", "type", "ip"} {
		if v, ok := body[key]; ok && v != nil {
			fmt.Fprintf(w, "  %-9s %v\n", key+":", v)
		}
	}
	// state_before / state_after are truncated previews; the full
	// pre/post-op file content is on result_state_before /
	// result_state_after for audit diffing via --json.
	if before, ok := body["state_before"].(string); ok && before != "" {
		fmt.Fprintf(w, "  state_before: %s\n", truncate(before, 80))
	}
	if after, ok := body["state_after"].(string); ok && after != "" {
		fmt.Fprintf(w, "  state_after:  %s\n", truncate(after, 80))
	}
	fmt.Fprintln(w, "  (see --json for result_state_before / result_state_after full content)")
}
