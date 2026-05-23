// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package k8s

import (
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// The verb files keep referring to the unqualified names; the operation-
// call logic lives once in the dispatch package. The k8s-specific shared
// flag set + verb pipeline stay here.
type (
	// CallResult is the decoded OperationResult envelope.
	CallResult = dispatch.CallResult
	// callRequestBody is the on-the-wire OperationCall body (asserted by tests).
	callRequestBody = dispatch.CallRequestBody
)

// errOpError is the structured-failure sentinel (status error/denied).
var errOpError = dispatch.ErrOpError

// conn binds this package's pre-baked connector_id + authed transport
// (doAuthedRequest) to the shared dispatch core.
var conn = dispatch.Connector{ID: ConnectorID, Request: doAuthedRequest}

type k8sAddrFlags struct {
	targetName        string
	jsonOut           bool
	backplaneOverride string
}

// bindK8sAddrFlags registers the shared address flags on a command and
// returns a pointer to the populated struct. Verbs add their own
// op-specific flags (--namespace, --label-selector, etc.) on top.
func bindK8sAddrFlags(cmd *cobra.Command) *k8sAddrFlags {
	f := &k8sAddrFlags{}
	cmd.Flags().StringVar(&f.targetName, "target", "",
		"K8s target slug to dispatch against (resolved server-side)")
	cmd.Flags().BoolVar(&f.jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&f.backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return f
}

// dispatchVerb runs the resolve-backplane → dispatch → render pipeline
// every K8s verb shares. opID + params come from the verb; the shared
// address flags carry target / json / backplane.
func dispatchVerb(cmd *cobra.Command, f *k8sAddrFlags, opID string, params map[string]any) error {
	backplaneURL, err := backplane.Resolve(f.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), f.jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, f.targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, f.jsonOut)
	}
	return conn.Render(cmd, opID, r, f.jsonOut, nil)
}
