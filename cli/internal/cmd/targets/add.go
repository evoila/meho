// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newAddCmd returns the `meho targets add` command (alias `create`).
//
// CLI shape:
//
//	meho targets add <name> --product <p> --host <h> \
//	  [--alias a --alias b] [--version v] [--port n] [--fqdn f] \
//	  [--secret-ref <ref>] [--auth-model <m>] [--verify-tls=false] \
//	  [--tls-ca-pin <pin>] [--tls-server-name <sni>] \
//	  [--preferred-impl <id>] [--note <n>] [--json] [--backplane <url>]
//
// It POSTs a single TargetCreate to POST /api/v1/targets — the same
// route `meho targets import` drives for each new row — reusing this
// package's `doAuthedRequest` plumbing (bearer injection + one-shot
// 401-refresh-retry). On 201 it renders the created Target in the same
// key-value shape as `meho targets describe`; a 422 (unknown product,
// out-of-tenant secret_ref, SSRF-guarded host) surfaces the server's
// error body so the operator sees exactly what the backplane rejected.
//
// The verb is a thin transport over the server contract: it runs no
// validation of its own — that stays entirely server-side. `tenant_id`
// is never sent; the backplane binds it from the operator's JWT (the
// TargetCreate body shape has no tenant_id field to populate).
//
// Only flags the operator explicitly set land in the request body.
// Unset optionals are omitted rather than serialised as JSON null so
// the server applies its documented defaults — most importantly, an
// omitted `secret_ref` derives the per-tenant default
// (`tenants/<tenant_id>/<name>`, #1723), whereas an explicit null would
// persist "no credential" (#2872). This mirrors `import`'s
// entryToCreateBody, which likewise carries only the keys present in
// the source descriptor.
//
// Exit codes (shared with the sibling verbs):
//   - 0   target created; row rendered
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 409 duplicate name, 422 rejected
//     body, malformed response)
//   - 5   insufficient_role
func newAddCmd() *cobra.Command {
	var (
		product           string
		host              string
		aliases           []string
		version           string
		port              int
		fqdn              string
		secretRef         string
		authModel         string
		verifyTLS         bool
		tlsCaPin          string
		tlsServerName     string
		preferredImpl     string
		note              string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:     "add <name>",
		Aliases: []string{"create"},
		Short:   "Register a single target (alias `create`)",
		Long: "add registers one target in the operator's tenant via " +
			"POST /api/v1/targets — the single-target counterpart to the " +
			"bulk `meho targets import`. `--product` and `--host` are " +
			"required; `<name>` is the positional canonical name. Every " +
			"other flag maps 1:1 to an optional TargetCreate field and is " +
			"sent only when set, so the server's defaults apply to the " +
			"rest (a target created without `--secret-ref` derives the " +
			"per-tenant default credential path; `--verify-tls=false` opts " +
			"out of TLS verification).\n\n" +
			"All validation is server-side: an unknown `--product`, a " +
			"`--secret-ref` outside the operator's tenant subtree, or an " +
			"SSRF-guarded `--host` come back as a 422 whose body the CLI " +
			"prints verbatim. On success the created target is rendered in " +
			"the same shape as `meho targets describe` (use `--json` for " +
			"the raw Target).\n\n" +
			"Authentication uses the token `meho login` wrote, with the " +
			"same one-shot 401-refresh-retry as every other targets verb. " +
			"The tenant is the operator's JWT-bound tenant — `tenant_id` is " +
			"never sent by the client.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body := map[string]any{
				"name":    args[0],
				"product": product,
				"host":    host,
			}
			// Carry only the optionals the operator explicitly set (see
			// the doc comment: omit-vs-null is load-bearing for
			// secret_ref). Each flag maps to its snake_case TargetCreate
			// field name.
			fs := cmd.Flags()
			if fs.Changed("alias") {
				body["aliases"] = aliases
			}
			if fs.Changed("version") {
				body["version"] = version
			}
			if fs.Changed("port") {
				body["port"] = port
			}
			if fs.Changed("fqdn") {
				body["fqdn"] = fqdn
			}
			if fs.Changed("secret-ref") {
				body["secret_ref"] = secretRef
			}
			if fs.Changed("auth-model") {
				body["auth_model"] = authModel
			}
			if fs.Changed("verify-tls") {
				body["verify_tls"] = verifyTLS
			}
			if fs.Changed("tls-ca-pin") {
				body["tls_ca_pin"] = tlsCaPin
			}
			if fs.Changed("tls-server-name") {
				body["tls_server_name"] = tlsServerName
			}
			if fs.Changed("preferred-impl") {
				body["preferred_impl_id"] = preferredImpl
			}
			if fs.Changed("note") {
				body["notes"] = note
			}
			return runAdd(cmd, addOptions{
				Body:              body,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&product, "product", "",
		"connector product slug (required; must match a registered connector — see `meho connector list`)")
	cmd.Flags().StringVar(&host, "host", "",
		"host or IP the connector dials (required)")
	cmd.Flags().StringArrayVar(&aliases, "alias", nil,
		"additional name the target resolves by (repeatable)")
	cmd.Flags().StringVar(&version, "version", "",
		"operator-asserted product version (e.g. 9.0) the resolver consults before the first probe")
	cmd.Flags().IntVar(&port, "port", 0,
		"connection port (default: the connector's product default)")
	cmd.Flags().StringVar(&fqdn, "fqdn", "",
		"fully-qualified domain name, when distinct from --host")
	cmd.Flags().StringVar(&secretRef, "secret-ref", "",
		"Vault path of the target's credential (omit to derive the per-tenant default)")
	cmd.Flags().StringVar(&authModel, "auth-model", "",
		"per-target identity model (default: shared_service_account)")
	cmd.Flags().BoolVar(&verifyTLS, "verify-tls", true,
		"verify the target's TLS certificate; pass --verify-tls=false to opt out")
	cmd.Flags().StringVar(&tlsCaPin, "tls-ca-pin", "",
		"PEM CA bundle to pin for this target (mutually exclusive with --verify-tls=false)")
	cmd.Flags().StringVar(&tlsServerName, "tls-server-name", "",
		"TLS SNI / certificate-verification hostname, decoupled from --host")
	cmd.Flags().StringVar(&preferredImpl, "preferred-impl", "",
		"connector impl_id override for the G0.6 resolver's tie-break")
	cmd.Flags().StringVar(&note, "note", "",
		"free-form operator note stored on the target")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the created target as JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to create the target in (defaults to the URL recorded by `meho login`)")
	_ = cmd.MarkFlagRequired("product")
	_ = cmd.MarkFlagRequired("host")
	return cmd
}

// addOptions carries the resolved inputs for runAdd. Body is the JSON
// request payload already partitioned by the RunE closure (name /
// product / host plus only the optionals the operator set); keeping it
// pre-built lets runAdd stay a thin marshal-POST-render step and lets
// tests drive the cobra command end-to-end against an httptest server.
type addOptions struct {
	Body              map[string]any
	JSONOut           bool
	BackplaneOverride string
}

// runAdd marshals the TargetCreate body, POSTs it to /api/v1/targets
// through the shared doAuthedRequest plumbing, and renders the created
// Target. Non-2xx responses arrive as *httpError and route through the
// same renderImportRequestError ladder the import path uses, so a 422
// body reaches the operator verbatim and a 401 triggers the one-shot
// refresh inside doAuthedRequest before surfacing auth_expired.
func runAdd(cmd *cobra.Command, opts addOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	payload, err := json.Marshal(opts.Body)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("marshal request body: %v", err)), opts.JSONOut)
	}
	raw, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodPost, "/api/v1/targets", payload)
	if err != nil {
		return renderImportRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	var t api.Target
	if err := json.Unmarshal(raw, &t); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode created target: %v", err)), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), &t)
	}
	printTargetSummary(cmd.OutOrStdout(), &t)
	return nil
}
