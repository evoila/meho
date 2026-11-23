// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"fmt"
	"maps"
)

// bindWebApproveClient binds the console's approval client (#6282,
// default `meho-web-approve`) to the approval sign-in flow, the flow of
// the command line's approval client. So a console approval sign-in gets
// the approval level `meho-approve` from the same steps.
//
// The bootstrap does not make this client: like the console client
// `meho-web`, it is a confidential client, and it has the same secret as
// `meho-web`. A realm without it is skipped with a note. A client that
// is bound to the flow stays bound. flowID "" (the realm's flow has other
// steps) binds nothing.
func bindWebApproveClient(ctx context.Context, c *adminClient, clientID, flowID string) ([]string, error) {
	if clientID == "" || flowID == "" {
		return nil, nil
	}
	client, err := c.findClient(ctx, clientID)
	if err != nil {
		return nil, err
	}
	if client == nil {
		return []string{fmt.Sprintf(
			"[skip] console approval client %q not found; make it next to the console client "+
				"(see the Keycloak realm guide), then run again to bind it to login flow %q",
			clientID, approvalFlowAlias)}, nil
	}
	if client.AuthenticationFlowBindingOverrides["browser"] == flowID {
		return []string{fmt.Sprintf("[skip] client %q uses login flow %q", clientID, approvalFlowAlias)}, nil
	}
	overrides := maps.Clone(client.AuthenticationFlowBindingOverrides)
	if overrides == nil {
		overrides = map[string]string{}
	}
	overrides["browser"] = flowID
	client.AuthenticationFlowBindingOverrides = overrides
	if err := c.updateClient(ctx, client.ID, client); err != nil {
		return nil, err
	}
	return []string{fmt.Sprintf("[updated] client %q uses login flow %q", clientID, approvalFlowAlias)}, nil
}
