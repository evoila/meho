// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// clearableFields maps each `--clear` value to the TargetUpdate field it
// sets to JSON null. The keys match the flag that sets the same field,
// so `--clear note` undoes `--note`. Only nullable columns are listed;
// the server rejects a null for the others.
var clearableFields = map[string]string{
	"secret-ref":      "secret_ref",
	"version":         "version",
	"port":            "port",
	"fqdn":            "fqdn",
	"tls-ca-pin":      "tls_ca_pin",
	"tls-server-name": "tls_server_name",
	"preferred-impl":  "preferred_impl_id",
	"note":            "notes",
	"secret-field":    "secret_field_map",
}

// emptyableFields maps the `--clear` values for the two collection
// fields to the TargetUpdate key and the empty value that removes all
// entries. `aliases` and `extras` are NOT NULL columns, so they are
// cleared with `[]` / `{}` instead of null. The values are typed
// (not nil) so they marshal as `[]` and `{}`.
var emptyableFields = map[string]struct {
	field string
	empty any
}{
	"alias": {"aliases", []string{}},
	"extra": {"extras", map[string]any{}},
}

// updateFlags holds the values of the `meho targets update` flags.
type updateFlags struct {
	aliases       []string
	product       string
	version       string
	host          string
	port          int
	fqdn          string
	secretRef     string
	authModel     string
	verifyTLS     bool
	tlsCaPin      string
	tlsServerName string
	vpnRequired   bool
	preferredImpl string
	note          string
	extras        []string
	secretFields  []string
	clear         []string
}

// newUpdateCmd returns the `meho targets update` command.
//
// CLI shape:
//
//	meho targets update <name, alias or id> [--product p] [--host h] \
//	  [--alias a ...] [--version v] [--port n] [--fqdn f] \
//	  [--secret-ref <ref>] [--auth-model <m>] [--verify-tls=false] \
//	  [--tls-ca-pin <pin>] [--tls-server-name <sni>] [--vpn-required=bool] \
//	  [--preferred-impl <id>] [--note <n>] [--extra key=value ...] \
//	  [--secret-field <connector_field>=<source_field> ...] \
//	  [--clear <field> ...] [--json] [--backplane <url>]
//
// It sends one PATCH /api/v1/targets/{name} (TargetUpdate). Only the
// flags the operator set land in the body, so every other field stays
// as it is. `--clear <field>` sends an explicit JSON null for that
// field, which the server stores as "no value" (for `secret-ref`: the
// target has no credential). Like `add`, all validation is server-side;
// the response renders like `meho targets describe`.
//
// Exit codes match `add`; 404 (no such target) and 409 (ambiguous
// alias) render through the shared renderHTTPStatus ladder.
func newUpdateCmd() *cobra.Command {
	var (
		f                 updateFlags
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "update <name, alias or id>",
		Short: "Change fields on an existing target",
		Long: "update changes one target via PATCH /api/v1/targets/{name}. " +
			"Only the flags you pass are sent; every other field keeps its " +
			"current value. The flags match `meho targets add`.\n\n" +
			"`--alias` replaces the whole alias list, and `--extra " +
			"key=value` replaces the whole `extras` map (pass every key " +
			"you want to keep). `--clear <field>` (repeatable) removes a " +
			"value: it sends null for a single field, an empty list for " +
			"`alias`, and an empty map for `extra`. Fields you can clear: " +
			strings.Join(clearableFieldNames(), ", ") + ". For example, " +
			"`--clear secret-ref --extra scheme=http` turns a target into " +
			"a plain-HTTP target with no credential, and `--clear alias` " +
			"removes every alias.\n\n" +
			"Re-sending `--secret-ref` with the same path makes MEHO read " +
			"the secret again, for example after you changed it in Vault.\n\n" +
			"`--secret-field <connector_field>=<source_field>` (repeatable) " +
			"adds or replaces one entry of the target's secret field map and " +
			"keeps the other entries. `--secret-field <connector_field>=` " +
			"(nothing after `=`) removes that entry, and `--clear " +
			"secret-field` removes the whole map.\n\n" +
			"All validation is server-side; a 422 body is printed as is. " +
			"On success the updated target is shown in the same shape as " +
			"`meho targets describe` (use `--json` for the raw Target).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body, serr := buildUpdateBody(cmd, &f)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, jsonOut)
			}
			return runUpdate(cmd, args[0], body, jsonOut, backplaneOverride)
		},
	}
	registerUpdateFlags(cmd, &f)
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the updated target as JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL the target lives in (defaults to the URL recorded by `meho login`)")
	return cmd
}

// registerUpdateFlags binds the TargetUpdate field flags onto cmd.
func registerUpdateFlags(cmd *cobra.Command, f *updateFlags) {
	fs := cmd.Flags()
	fs.StringVar(&f.product, "product", "", "new connector product slug")
	fs.StringVar(&f.host, "host", "", "new host or IP the connector dials")
	fs.StringArrayVar(&f.aliases, "alias", nil,
		"alias the target resolves by (repeatable; replaces the whole alias list)")
	fs.StringVar(&f.version, "version", "", "operator-asserted product version")
	fs.IntVar(&f.port, "port", 0, "connection port")
	fs.StringVar(&f.fqdn, "fqdn", "", "fully-qualified domain name, when distinct from --host")
	fs.StringVar(&f.secretRef, "secret-ref", "", "Vault path of the target's credential")
	fs.StringVar(&f.authModel, "auth-model", "", "per-target identity model")
	fs.BoolVar(&f.verifyTLS, "verify-tls", true,
		"verify the target's TLS certificate; pass --verify-tls=false to opt out")
	fs.StringVar(&f.tlsCaPin, "tls-ca-pin", "", "PEM CA bundle to pin for this target")
	fs.StringVar(&f.tlsServerName, "tls-server-name", "",
		"TLS SNI / certificate-verification hostname, decoupled from --host")
	fs.BoolVar(&f.vpnRequired, "vpn-required", false,
		"mark that a VPN must be up to reach the target; pass --vpn-required=false to clear it")
	fs.StringVar(&f.preferredImpl, "preferred-impl", "",
		"connector impl_id override for the resolver's tie-break")
	fs.StringVar(&f.note, "note", "", "free-form operator note stored on the target")
	fs.StringArrayVar(&f.extras, "extra", nil,
		"key=value for the target's extras map, e.g. scheme=http (repeatable; replaces the whole map)")
	fs.StringArrayVar(&f.secretFields, "secret-field", nil,
		"connector_field=source_field to add or replace in the secret field map; connector_field= removes it (repeatable)")
	fs.StringArrayVar(&f.clear, "clear", nil,
		"field to remove (repeatable; sends null, or an empty list for alias and an empty map for extra): "+
			strings.Join(clearableFieldNames(), ", "))
}

// buildUpdateBody turns the flags the operator set into a sparse
// TargetUpdate body. It fails when no field was given, when `--clear`
// names an unknown field, or when one field is both set and cleared.
// `--clear alias` and `--clear extra` send `[]` and `{}`; every other
// `--clear` sends null.
func buildUpdateBody(cmd *cobra.Command, f *updateFlags) (map[string]any, *output.StructuredError) {
	fs := cmd.Flags()
	body := map[string]any{}
	set := map[string]any{
		"alias":           f.aliases,
		"product":         f.product,
		"version":         f.version,
		"host":            f.host,
		"port":            f.port,
		"fqdn":            f.fqdn,
		"secret-ref":      f.secretRef,
		"auth-model":      f.authModel,
		"verify-tls":      f.verifyTLS,
		"tls-ca-pin":      f.tlsCaPin,
		"tls-server-name": f.tlsServerName,
		"vpn-required":    f.vpnRequired,
		"preferred-impl":  f.preferredImpl,
		"note":            f.note,
	}
	for flag, value := range set {
		if fs.Changed(flag) {
			body[updateFieldName(flag)] = value
		}
	}
	if fs.Changed("extra") {
		parsed, perr := parseExtraFlags(f.extras)
		if perr != nil {
			return nil, perr
		}
		body["extras"] = parsed
	}
	if fs.Changed("secret-field") {
		parsed, perr := parseSecretFieldFlags(f.secretFields, true)
		if perr != nil {
			return nil, perr
		}
		body["secret_field_map"] = parsed
	}
	for _, name := range f.clear {
		field, value, ok := clearValue(name)
		if !ok {
			return nil, output.Unexpected(fmt.Sprintf(
				"invalid --clear %q: expected one of %s",
				name, strings.Join(clearableFieldNames(), ", ")))
		}
		if fs.Changed(name) {
			return nil, output.Unexpected(fmt.Sprintf(
				"--%s and --clear %s cannot be used together", name, name))
		}
		body[field] = value
	}
	if len(body) == 0 {
		return nil, output.Unexpected(
			"nothing to update: pass at least one field flag or --clear")
	}
	return body, nil
}

// clearValue returns the TargetUpdate key and the value `--clear name`
// sends: nil for a nullable field, `[]` for alias, `{}` for extra.
// ok is false when name is not clearable.
func clearValue(name string) (field string, value any, ok bool) {
	if field, ok := clearableFields[name]; ok {
		return field, nil, true
	}
	if e, ok := emptyableFields[name]; ok {
		return e.field, e.empty, true
	}
	return "", nil, false
}

// updateFieldName maps a flag name to its snake_case TargetUpdate key.
func updateFieldName(flag string) string {
	if field, ok := clearableFields[flag]; ok {
		return field
	}
	if flag == "alias" {
		return "aliases"
	}
	return strings.ReplaceAll(flag, "-", "_")
}

// clearableFieldNames returns the `--clear` values in a stable order
// for help text and error messages.
func clearableFieldNames() []string {
	names := make([]string, 0, len(clearableFields)+len(emptyableFields))
	for name := range clearableFields {
		names = append(names, name)
	}
	for name := range emptyableFields {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// runUpdate PATCHes the body to /api/v1/targets/{name} and renders the
// updated Target. Same plumbing and error ladder as runAdd.
func runUpdate(cmd *cobra.Command, name string, body map[string]any, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("marshal request body: %v", err)), jsonOut)
	}
	raw, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodPatch,
		"/api/v1/targets/"+pathEscape(name), payload)
	if err != nil {
		return renderImportRequestError(cmd, backplaneURL, err, jsonOut)
	}
	var t api.Target
	if err := json.Unmarshal(raw, &t); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode updated target: %v", err)), jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), &t)
	}
	printTargetSummary(cmd.OutOrStdout(), &t)
	return nil
}
