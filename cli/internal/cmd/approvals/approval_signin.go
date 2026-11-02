// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// approvalSignIn is the approval sign-in of one CLI run (#5515).
//
// MEHO accepts an approve on the command line only with a token from the
// person's own fresh sign-in in the browser, made for the approval
// client. One such sign-in may approve several requests for a short time
// (the approval window, 15 minutes by default). A destructive request
// needs a sign-in of its own, and after it that sign-in approves nothing
// more.
//
// The token lives in this struct only, for this one run. It is never
// written to the token store, a file, an environment variable or the
// output. The approval client of MEHO's realm gives no refresh token, so
// when the token runs out, the person signs in again in the browser.
// Where the identity provider gives a refresh token, the token source
// renews the token in memory; the renewal keeps the time of the
// sign-in, so the window does not grow.
type approvalSignIn struct {
	client *api.AuthedClient
	src    oauth2.TokenSource
	// notice gets one line when the sign-in has run out and a new one
	// starts. nil writes nothing.
	notice io.Writer
	// used: the current sign-in approved at least one request.
	used bool
	// spent: the current sign-in approved a destructive request, so it
	// approves nothing more.
	spent bool
}

func newApprovalSignIn(client *api.AuthedClient, notice io.Writer) *approvalSignIn {
	return &approvalSignIn{client: client, notice: notice}
}

// bearer returns the token for one approve. It opens a new browser
// sign-in when there is none yet, when the current one is spent, and
// for a destructive request when the current one was already used.
func (s *approvalSignIn) bearer(ctx context.Context, destructive bool) (string, error) {
	if s.src == nil || s.spent || (destructive && s.used) {
		if err := s.signIn(ctx); err != nil {
			return "", err
		}
	}
	tok, err := s.src.Token()
	if err != nil {
		// The token ran out, or the renewal failed, for example because
		// the sign-in session ended. A new sign-in is the only way on.
		if errors.Is(err, auth.ErrFreshSignInExpired) && s.notice != nil {
			fmt.Fprintln(s.notice, "\nThe approval sign-in has run out. Sign in again in the browser.")
		}
		if err := s.signIn(ctx); err != nil {
			return "", err
		}
		if tok, err = s.src.Token(); err != nil {
			return "", err
		}
	}
	if tok.AccessToken == "" {
		return "", errors.New("meho: the browser sign-in returned no access token")
	}
	return tok.AccessToken, nil
}

// approved records one approve with the current sign-in.
func (s *approvalSignIn) approved(destructive bool) {
	s.used = true
	if destructive {
		s.spent = true
	}
}

// forget drops the current sign-in after MEHO refused it, so the next
// approve opens a new one.
func (s *approvalSignIn) forget() {
	s.src = nil
	s.used, s.spent = false, false
}

func (s *approvalSignIn) signIn(ctx context.Context) error {
	src, err := startApprovalSignIn(ctx, s.client)
	if err != nil {
		return err
	}
	s.src = src
	s.used, s.spent = false, false
	return nil
}

// startApprovalSignIn runs one browser sign-in for approvals and returns
// its token source. A package variable so tests that are not about the
// sign-in can stand in for the browser and the identity provider.
var startApprovalSignIn = browserApprovalSignIn

// openSignInBrowser opens the fresh sign-in link. A package variable
// so tests can play the browser against a fake identity provider.
var openSignInBrowser = auth.OpenSystemBrowser

func browserApprovalSignIn(ctx context.Context, client *api.AuthedClient) (oauth2.TokenSource, error) {
	issuer, loginClientID := client.SignInClient()
	if issuer == "" {
		return nil, errors.New("meho: the stored login names no identity provider; " +
			"run `meho login` again, or approve in the web interface")
	}
	clientID, err := approvalClientID(ctx, client, loginClientID)
	if err != nil {
		return nil, err
	}
	doc, err := auth.FetchDiscoveryFromRealm(ctx, client.HTTPClient(), issuer)
	if err != nil {
		return nil, err
	}
	tok, err := auth.RunFreshSignIn(ctx, doc, clientID, auth.FreshSignInOptions{
		HTTPClient:  client.HTTPClient(),
		OpenBrowser: openSignInBrowser,
	})
	if err != nil {
		return nil, err
	}
	return auth.FreshSignInTokenSource(ctx, doc, clientID, tok, client.HTTPClient()), nil
}

// approvalClientID asks MEHO which client the approval sign-in uses
// (`cli_approve_client_id` of GET /api/v1/auth-config). A MEHO that does
// not send the field is older than #5515 and accepts the client of the
// stored login, so that one is used. An empty value means MEHO accepts
// no approve on the command line.
func approvalClientID(ctx context.Context, client *api.AuthedClient, loginClientID string) (string, error) {
	resp, err := client.AuthConfigApiV1AuthConfigGetWithResponse(ctx)
	if err == nil && resp.JSON200 != nil && resp.JSON200.CliApproveClientId != nil {
		if *resp.JSON200.CliApproveClientId == "" {
			return "", errors.New("meho: this MEHO accepts no approval on the command line; " +
				"approve in the web interface")
		}
		return *resp.JSON200.CliApproveClientId, nil
	}
	if loginClientID == "" {
		return "", errors.New("meho: the stored login names no client for the sign-in; " +
			"run `meho login` again, or approve in the web interface")
	}
	return loginClientID, nil
}

// isDestructive reports whether the request's operation is destructive.
func isDestructive(item *api.ApprovalRequestView) bool {
	level, _ := item.ProposedEffect["safety_level"].(string)
	return level == "destructive"
}

// showRequest writes what one approval would run, so the person can
// decide on it: the operation and connector (with its version), the
// target, the risk level, what it does and its parameters.
func showRequest(w io.Writer, header string, d *api.ApprovalRequestView) {
	fmt.Fprintf(w, "\n%s\n", header)
	rc := d.ReviewerContext
	if rc != nil && rc.Summary != nil && *rc.Summary != "" {
		fmt.Fprintf(w, "  What it does: %s\n", *rc.Summary)
	}
	fmt.Fprintf(w, "  Request:      %s\n", d.Id.String())
	fmt.Fprintf(w, "  Operation:    %s\n", d.OpId)
	fmt.Fprintf(w, "  Connector:    %s\n", d.ConnectorId)
	if level, ok := d.ProposedEffect["safety_level"].(string); ok && level != "" {
		fmt.Fprintf(w, "  Risk level:   %s\n", level)
	}
	if rc != nil && rc.Subject != nil && *rc.Subject != "" {
		fmt.Fprintf(w, "  Subject:      %s\n", *rc.Subject)
	}
	if d.TargetId != nil {
		fmt.Fprintf(w, "  Target:       %s\n", targetLabel(d.TargetId.String(), rc))
	}
	fmt.Fprintf(w, "  Requested by: %s\n", principalLabel(d.PrincipalSub, d.PrincipalName))
	if rc != nil && rc.BlastRadius != nil {
		b, _ := json.MarshalIndent(*rc.BlastRadius, "    ", "  ")
		fmt.Fprintf(w, "  What else is affected:\n    %s\n", string(b))
	}
	if len(d.ProposedEffect) > 0 {
		b, _ := json.MarshalIndent(d.ProposedEffect, "    ", "  ")
		fmt.Fprintf(w, "  Parameters and preview:\n    %s\n", string(b))
	}
}

// askYes writes prompt and reads one line. Only "yes" counts.
func askYes(in *bufio.Reader, w io.Writer, prompt string) bool {
	fmt.Fprint(w, prompt)
	line, _ := in.ReadString('\n')
	return strings.EqualFold(strings.TrimSpace(line), "yes")
}
