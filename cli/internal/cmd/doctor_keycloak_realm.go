// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"github.com/evoila/meho/cli/internal/api"
)

// keycloakRealmCheckName names the doctor line for Keycloak targets
// that name no managed realm. MEHO has no built-in managed realm, so
// every operation on such a target is refused.
const keycloakRealmCheckName = "keycloak managed realm"

// keycloakRealmFix is the fix for a Keycloak target without a managed
// realm. `--extra` replaces the whole extras map, so the hint says to
// repeat the keys the target keeps.
const keycloakRealmFix = "run `meho targets update <name> --extra managed_realm=<realm>` for each target; " +
	"--extra replaces the whole extras map, so repeat every extras key you keep " +
	"(for example `--extra admin_realm=master`)"

// keycloakTargetsPageSize is the page size for the target list.
const keycloakTargetsPageSize = 500

// addKeycloakRealmCheck adds one line that names every Keycloak target
// without a managed realm. A list or read error is a warning: the
// doctor could not check, and the rest of the report still holds.
func addKeycloakRealmCheck(ctx context.Context, r *doctorReport, client *api.AuthedClient) {
	names, err := keycloakTargetNames(ctx, client)
	if err != nil {
		r.add(authed(keycloakRealmCheckName, doctorWarn, "", "cannot check the Keycloak targets: "+err.Error(), ""))
		return
	}
	if len(names) == 0 {
		r.add(authed(keycloakRealmCheckName, doctorOK, "", "no Keycloak targets", ""))
		return
	}
	var missing []string
	for _, name := range names {
		resp, err := client.DescribeTargetApiV1TargetsNameGetWithResponse(ctx, name, nil)
		if err != nil {
			r.add(authed(keycloakRealmCheckName, doctorWarn, "", "cannot read target "+name+": "+err.Error(), ""))
			return
		}
		if resp.JSON200 == nil {
			r.add(authed(keycloakRealmCheckName, doctorWarn, "",
				fmt.Sprintf("cannot read target %s: HTTP %d", name, resp.StatusCode()), ""))
			return
		}
		if !hasManagedRealm(resp.JSON200.Extras) {
			missing = append(missing, name)
		}
	}
	if len(missing) == 0 {
		r.add(authed(keycloakRealmCheckName, doctorOK, "",
			fmt.Sprintf("every Keycloak target names a managed realm (%d)", len(names)), ""))
		return
	}
	sort.Strings(missing)
	r.add(authed(keycloakRealmCheckName, doctorWarn, "",
		"these Keycloak targets name no managed realm, so MEHO refuses every operation on them: "+
			strings.Join(missing, ", "),
		keycloakRealmFix))
}

// keycloakTargetNames lists the names of all Keycloak targets, page by page.
func keycloakTargetNames(ctx context.Context, client *api.AuthedClient) ([]string, error) {
	product := "keycloak"
	limit := keycloakTargetsPageSize
	params := &api.ListTargetsApiV1TargetsGetParams{Product: &product, Limit: &limit}
	var names []string
	for {
		resp, err := client.ListTargetsApiV1TargetsGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
		if resp.JSON200 == nil {
			return nil, fmt.Errorf("HTTP %d from /api/v1/targets", resp.StatusCode())
		}
		for _, t := range resp.JSON200.Items {
			names = append(names, t.Name)
		}
		next := resp.JSON200.NextCursor
		if next == nil || *next == "" {
			return names, nil
		}
		params.Cursor = next
	}
}

// hasManagedRealm mirrors the backend check: a non-blank string.
func hasManagedRealm(extras map[string]interface{}) bool {
	realm, ok := extras["managed_realm"].(string)
	return ok && strings.TrimSpace(realm) != ""
}
