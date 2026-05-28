// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

func newOverridesRemoveCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "remove <override-id>",
		Short: "Delete a broadcast-detail override rule by id",
		Long: "remove calls DELETE /api/v1/broadcast/overrides/{id}. Silent " +
			"on success (mirrors `meho` UX convention). A 404 means either " +
			"the id doesn't exist or it belongs to another tenant -- the " +
			"backend deliberately conflates the two so existence is not " +
			"leaked across tenant boundaries.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOverridesRemove(cmd, overridesRemoveOptions{
				OverrideID:        args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit JSON error envelope on failure (success is still silent)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type overridesRemoveOptions struct {
	OverrideID        string
	JSONOut           bool
	BackplaneOverride string
}

func runOverridesRemove(cmd *cobra.Command, opts overridesRemoveOptions) error {
	if opts.OverrideID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("remove requires a non-empty <override-id> argument"),
			opts.JSONOut,
		)
	}
	overrideID, err := parseOverrideID(opts.OverrideID)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	if err := deleteOverride(cmd.Context(), client, overrideID); err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// Silent on success -- mirrors `meho` UX convention for DELETE.
	return nil
}

// parseOverrideID validates the operator's `<override-id>` arg as a
// UUID at the verb edge. The typed-client's path parameter is
// `openapi_types.UUID` (an alias for `uuid.UUID`); parsing here
// keeps the bad-input error a clean output.Unexpected instead of a
// `fmt.Errorf("invalid UUID: %s")` mid-request or a server-side
// 422 after the round-trip. Returns a `uuid.UUID` (assignable to
// `openapi_types.UUID` since the alias resolves to the same type).
func parseOverrideID(idArg string) (uuid.UUID, error) {
	id, err := uuid.Parse(idArg)
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("override-id is not a valid UUID: %s", idArg)
	}
	return id, nil
}

// deleteOverride drives the typed-client
// `DeleteOverrideApiV1BroadcastOverridesOverrideIdDelete` endpoint
// with a one-shot 401-retry around the underlying AuthedClient's
// refresh path. The route returns 204 No Content on success;
// non-2xx responses come back as `*httpResponseError`.
func deleteOverride(
	ctx context.Context,
	client *api.AuthedClient,
	overrideID uuid.UUID,
) error {
	params := &api.DeleteOverrideApiV1BroadcastOverridesOverrideIdDeleteParams{}
	resp, err := client.DeleteOverrideApiV1BroadcastOverridesOverrideIdDeleteWithResponse(
		ctx, overrideID, params,
	)
	if err != nil {
		return err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return rerr
		}
		resp, err = client.DeleteOverrideApiV1BroadcastOverridesOverrideIdDeleteWithResponse(
			ctx, overrideID, params,
		)
		if err != nil {
			return err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return nil
}
