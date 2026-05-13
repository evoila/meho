// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// knownConnectors maps each product slug to the ops the CLI registers.
// v0.2 ships vault only. v0.2.next replaces this with manifest-driven
// discovery from the backplane's connector manifest endpoint.
var knownConnectors = []connectorSpec{
	{product: "vault", ops: []string{"kv.read"}},
}

type connectorSpec struct {
	product string
	ops     []string
}

// newConnectorCommands returns one root cobra.Command per known connector.
// Each product command hosts one sub-command per operation. The resulting
// slice is grafted onto the root command in root.go.
func newConnectorCommands() []*cobra.Command {
	cmds := make([]*cobra.Command, 0, len(knownConnectors))
	for _, spec := range knownConnectors {
		productCmd := &cobra.Command{
			Use:          spec.product,
			Short:        fmt.Sprintf("Run an operation against the %s connector", spec.product),
			SilenceUsage: true,
		}
		for _, op := range spec.ops {
			productCmd.AddCommand(buildOpCommand(spec.product, op))
		}
		cmds = append(cmds, productCmd)
	}
	return cmds
}

// buildOpCommand constructs the per-operation cobra.Command for
// `meho <product> <op> [flags]`.
//
// DisableFlagParsing lets the RunE receive all flags as raw args so
// arbitrary `--<key> <value>` pairs can be forwarded to the backplane
// as operation params without declaring them in advance. --target and
// --json are extracted from the args by parseOpArgs; everything else
// flows through as params.
func buildOpCommand(product, op string) *cobra.Command {
	return &cobra.Command{
		Use:                op,
		Short:              fmt.Sprintf("Run %s via the %s connector", op, product),
		DisableFlagParsing: true,
		SilenceUsage:       true,
		SilenceErrors:      true,
		RunE: func(cmd *cobra.Command, args []string) error {
			parsed, err := parseOpArgs(args)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), parsed.jsonOut)
			}
			if parsed.target == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("--target is required"),
					parsed.jsonOut,
				)
			}
			backplaneURL, err := resolveBackplaneURL(parsed.backplane)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(err.Error()), parsed.jsonOut)
			}
			return runConnectorOp(
				cmd.Context(), cmd,
				product, op,
				parsed.target, parsed.params,
				backplaneURL, parsed.jsonOut,
			)
		},
	}
}

// parsedOpArgs holds the result of parseOpArgs.
type parsedOpArgs struct {
	target    string
	jsonOut   bool
	backplane string
	params    map[string]interface{}
}

// parseOpArgs processes the raw []string cobra passes when DisableFlagParsing
// is set. Recognises:
//
//	--target <slug>           the target name to run the op against
//	--json                    emit machine-readable JSON to stdout
//	--backplane <url>         override the backplane URL
//	--params <json>           inline JSON object for operation params
//	--params @<file>          load operation params from a JSON file
//	--<key> <val>             shorthand for {"key": "val"} in params
//	--<key>=<val>             same, = form
//
// Unknown flags with no value (boolean flags other than --json) are
// silently ignored rather than returning an error — forward compatibility.
func parseOpArgs(args []string) (parsedOpArgs, error) {
	result := parsedOpArgs{params: make(map[string]interface{})}
	i := 0
	for i < len(args) {
		arg := args[i]
		if !strings.HasPrefix(arg, "--") {
			i++
			continue
		}
		key := strings.TrimPrefix(arg, "--")
		// Handle --key=value form.
		if idx := strings.IndexByte(key, '='); idx >= 0 {
			val := key[idx+1:]
			key = key[:idx]
			if err := assignFlag(&result, key, val); err != nil {
				return result, err
			}
			i++
			continue
		}
		// Boolean flag with no value.
		if key == "json" {
			result.jsonOut = true
			i++
			continue
		}
		// Flag that takes the next token as its value.
		if i+1 >= len(args) || strings.HasPrefix(args[i+1], "--") {
			// Single-token flag with no value: skip silently.
			i++
			continue
		}
		val := args[i+1]
		i += 2
		if err := assignFlag(&result, key, val); err != nil {
			return result, err
		}
	}
	return result, nil
}

// assignFlag populates parsedOpArgs from a key-value pair extracted by
// parseOpArgs. Reserved keys (target, json, backplane, params) are
// handled specially; everything else is forwarded into params.
func assignFlag(r *parsedOpArgs, key, val string) error {
	switch key {
	case "target":
		r.target = val
	case "json":
		r.jsonOut = val == "" || val == "true" || val == "1"
	case "backplane":
		r.backplane = val
	case "params":
		m, err := loadParamsValue(val)
		if err != nil {
			return fmt.Errorf("--params: %w", err)
		}
		for k, v := range m {
			r.params[k] = v
		}
	default:
		r.params[key] = val
	}
	return nil
}

// loadParamsValue parses a --params value. Prefixing with '@' loads the
// named file as JSON; otherwise the value itself is parsed as inline JSON.
func loadParamsValue(val string) (map[string]interface{}, error) {
	var raw []byte
	if strings.HasPrefix(val, "@") {
		path := strings.TrimPrefix(val, "@")
		var err error
		raw, err = os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read params file %q: %w", path, err)
		}
	} else {
		raw = []byte(val)
	}
	var m map[string]interface{}
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse params JSON: %w", err)
	}
	return m, nil
}

// runConnectorOp calls POST /api/v1/connectors/{product}/{op} and renders
// the result to the operator.
func runConnectorOp(
	ctx context.Context,
	cmd *cobra.Command,
	product, op, target string,
	params map[string]interface{},
	backplaneURL string,
	jsonOut bool,
) error {
	client, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		if api.IsTokenNotFound(err) {
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf(
					"no stored credentials for %s; run `meho login %s`",
					backplaneURL, backplaneURL,
				)),
				jsonOut,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("build authed client: %v", err)),
			jsonOut,
		)
	}

	body := api.ConnectorExecRequest{
		Target: target,
		Params: &params,
	}

	resp, err := client.ExecuteOpApiV1ConnectorsProductOpIdPostWithResponse(
		ctx, product, op, nil, body,
	)
	if err != nil {
		if api.IsNoRefreshToken(err) {
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf(
					"stored token rejected and no refresh_token present; run `meho login %s`",
					backplaneURL,
				)),
				jsonOut,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, redactedError(err))),
			jsonOut,
		)
	}

	switch resp.StatusCode() {
	case http.StatusOK:
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
		}
		return printConnectorResult(cmd.OutOrStdout(), product, op, resp.JSON200)

	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"backplane rejected stored credentials; run `meho login %s`", backplaneURL,
			)),
			jsonOut,
		)

	case http.StatusNotFound:
		detail := extractErrorDetail(resp.Body)
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("unknown connector product %q: %s", product, detail)),
			jsonOut,
		)

	case http.StatusBadRequest:
		detail := extractErrorDetail(resp.Body)
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("%s: %s", op, detail)),
			jsonOut,
		)

	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d from %s", resp.StatusCode(), backplaneURL)),
			jsonOut,
		)
	}
}

// printConnectorResult renders an OperationResult map for human consumption.
// The default rendering prints status + op_id, then the result fields if
// present. Errors surface the structured detail from the backplane.
func printConnectorResult(w io.Writer, product, op string, result *map[string]interface{}) error {
	if result == nil {
		fmt.Fprintf(w, "%s %s: (empty response)\n", product, op)
		return nil
	}
	m := *result
	status, _ := m["status"].(string)
	if status == "ok" {
		if res, ok := m["result"]; ok && res != nil {
			blob, err := json.MarshalIndent(res, "", "  ")
			if err != nil {
				return fmt.Errorf("meho: marshal result: %w", err)
			}
			fmt.Fprintln(w, string(blob))
		} else {
			fmt.Fprintf(w, "%s %s: ok\n", product, op)
		}
		return nil
	}
	// Error or denied status.
	errStr, _ := m["error"].(string)
	if errStr == "" {
		errStr = status
	}
	fmt.Fprintf(w, "meho: connector error: %s\n", errStr)
	return nil
}

// extractErrorDetail parses the FastAPI error detail string from a non-200
// body. Returns a short human-readable summary, never a raw stack trace.
func extractErrorDetail(body []byte) string {
	var envelope struct {
		Detail interface{} `json:"detail"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		return "(unreadable error body)"
	}
	switch d := envelope.Detail.(type) {
	case string:
		return d
	case map[string]interface{}:
		// Structured 400: {"error": "unknown_op", "known_ops": [...]}
		errCode, _ := d["error"].(string)
		if ops, ok := d["known_ops"].([]interface{}); ok {
			strs := make([]string, 0, len(ops))
			for _, o := range ops {
				if s, ok := o.(string); ok {
					strs = append(strs, s)
				}
			}
			return fmt.Sprintf("%s; known ops: %s", errCode, strings.Join(strs, ", "))
		}
		return errCode
	default:
		return fmt.Sprintf("%v", d)
	}
}

// redactedError is defined in status.go (same package).
// connector.go calls it directly.
