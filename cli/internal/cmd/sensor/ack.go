// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// validAckReasons mirrors the backend acknowledgement reasons.
var validAckReasons = map[string]bool{"accepted_risk": true, "fix_planned": true, "false_alarm": true}

// newAckCmd returns the `meho sensor ack` command.
//
//	meho sensor ack <sensor_id> --until <time|duration>
//	  --reason accepted_risk|fix_planned|false_alarm --note <text>
//	  [--json] [--backplane <url>]
//
// Role: operator, people only.
func newAckCmd() *cobra.Command {
	var opts ackOptions
	cmd := &cobra.Command{
		Use:   "ack <sensor_id>",
		Short: "Acknowledge a sensor finding until a set time, with a reason (operator)",
		Long: "ack calls POST /api/v1/sensors/{id}/acknowledgement. You say until " +
			"when and why. MEHO records who. The sensor keeps running and its " +
			"real state stays visible.\n\n" +
			"While the acknowledgement is active, the advisory on operation " +
			"answers says \"acknowledged\" and the warning mails for this sensor " +
			"are not sent again. Dashboards show who, until when and why.\n\n" +
			"It ends at --until, or earlier when the sensor's state gets worse " +
			"(for example degraded to critical). Then the warnings come back.\n\n" +
			"--until is a time (RFC 3339, for example 2026-12-01T18:00:00Z) or a " +
			"duration from now (for example 72h). At most 90 days. --reason is " +
			"accepted_risk, fix_planned or false_alarm. --note is a short text " +
			"(up to 500 characters).\n\n" +
			"Only a green or never-checked sensor is refused (422), and a sensor " +
			"that already has an active acknowledgement (409). Operator role; an " +
			"agent or service token is refused (403).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.SensorID = args[0]
			return runAck(cmd, opts, time.Now())
		},
	}
	f := cmd.Flags()
	f.StringVar(&opts.Until, "until", "", "end time (RFC 3339) or duration from now (e.g. 72h); required")
	f.StringVar(&opts.Reason, "reason", "", "accepted_risk | fix_planned | false_alarm; required")
	f.StringVar(&opts.Note, "note", "", "short text that says why; required")
	f.BoolVar(&opts.JSONOut, "json", false, "emit the raw acknowledgement JSON instead of the summary")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type ackOptions struct {
	SensorID          string
	Until             string
	Reason            string
	Note              string
	JSONOut           bool
	BackplaneOverride string
}

// parseUntil reads --until as an RFC 3339 time or as a positive duration from now.
func parseUntil(value string, now time.Time) (time.Time, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, fmt.Errorf("--until is required: an acknowledgement needs an end time")
	}
	if t, err := time.Parse(time.RFC3339, value); err == nil {
		return t, nil
	}
	d, err := time.ParseDuration(value)
	if err != nil || d <= 0 {
		return time.Time{}, fmt.Errorf("--until must be an RFC 3339 time or a positive duration such as 72h; got %q", value)
	}
	return now.Add(d).UTC(), nil
}

func buildAckBody(opts ackOptions, now time.Time) (api.SensorAcknowledgementCreate, error) {
	until, err := parseUntil(opts.Until, now)
	if err != nil {
		return api.SensorAcknowledgementCreate{}, err
	}
	if !validAckReasons[opts.Reason] {
		return api.SensorAcknowledgementCreate{},
			fmt.Errorf("--reason must be one of: accepted_risk, fix_planned, false_alarm")
	}
	if strings.TrimSpace(opts.Note) == "" {
		return api.SensorAcknowledgementCreate{}, fmt.Errorf("--note is required: say why in a few words")
	}
	return api.SensorAcknowledgementCreate{
		ExpiresAt: until,
		Reason:    api.SensorAcknowledgementCreateReason(opts.Reason),
		Note:      opts.Note,
	}, nil
}

func runAck(cmd *cobra.Command, opts ackOptions, now time.Time) error {
	var sensorID openapi_types.UUID
	if err := sensorID.UnmarshalText([]byte(opts.SensorID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("sensor-id is not a valid UUID: %v", err)),
			opts.JSONOut)
	}
	body, err := buildAckBody(opts, now)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postAck(cmd.Context(), backplaneURL, sensorID, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON201 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP 201 without an acknowledgement payload", backplaneURL)),
			opts.JSONOut)
	}
	ack := resp.JSON201
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), ack)
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "acknowledged sensor %s (state %s)\n", ack.SensorId.String(), ack.AckedState)
	fmt.Fprintf(w, "  by:     %s\n", sanitizeCell(ack.CreatedBySub))
	fmt.Fprintf(w, "  until:  %s\n", ack.ExpiresAt.UTC().Format(time.RFC3339))
	fmt.Fprintf(w, "  reason: %s\n", ack.Reason)
	fmt.Fprintf(w, "  note:   %s\n", sanitizeCell(ack.Note))
	output.WriteAuditReceipt(w, output.AuditIDFromResponse(resp.HTTPResponse))
	return nil
}

// postAck calls POST /api/v1/sensors/{id}/acknowledgement via the generated typed client.
func postAck(
	ctx context.Context,
	backplaneURL string,
	sensorID openapi_types.UUID,
	body api.SensorAcknowledgementCreate,
) (*api.AcknowledgeSensorFindingApiV1SensorsSensorIdAcknowledgementPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.AcknowledgeSensorFindingApiV1SensorsSensorIdAcknowledgementPostParams{}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.AcknowledgeSensorFindingApiV1SensorsSensorIdAcknowledgementPostResponse, error) {
			return authed.AcknowledgeSensorFindingApiV1SensorsSensorIdAcknowledgementPostWithResponse(
				ctx, sensorID, params, body)
		},
		func(r *api.AcknowledgeSensorFindingApiV1SensorsSensorIdAcknowledgementPostResponse) int {
			return r.StatusCode()
		},
	)
}
