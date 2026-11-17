// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"github.com/evoila/meho/cli/internal/api"
)

// keycloakRealmCheckName names the doctor line for Keycloak targets
// that name no managed realm, or a realm that is not a plain name. MEHO
// has no built-in managed realm, so every operation on such a target is
// refused.
const keycloakRealmCheckName = "keycloak managed realm"

// keycloakRealmFix is the fix for a Keycloak target without a managed
// realm. `--extra` replaces the whole extras map, so the hint says to
// repeat the keys the target keeps.
const keycloakRealmFix = "run `meho targets update <name> --extra managed_realm=<realm>` for each target, " +
	"or set the realm in the console edit form; " +
	"--extra replaces the whole extras map, so repeat every extras key you keep " +
	"(for example `--extra admin_realm=master`)"

// keycloakTargetsPageSize is the page size for the target list.
const keycloakTargetsPageSize = 500

// addKeycloakRealmCheck adds one line that names every Keycloak target
// MEHO refuses. It asks the backplane's check route: a platform admin
// gets the targets of every tenant, any other caller those of their own
// tenant. An older backplane without the route answers 404; then the
// doctor reads the targets of the caller's tenant one by one. A list or
// read error is a warning: the doctor could not check, and the rest of
// the report still holds.
func addKeycloakRealmCheck(ctx context.Context, r *doctorReport, client *api.AuthedClient) {
	resp, err := client.KeycloakRealmCheckApiV1TargetsChecksKeycloakRealmGetWithResponse(ctx, nil)
	switch {
	case err != nil:
		r.add(authed(keycloakRealmCheckName, doctorWarn, "", "cannot check the Keycloak targets: "+err.Error(), ""))
	case resp.JSON200 != nil:
		addKeycloakRealmResult(r, resp.JSON200)
	case resp.StatusCode() == http.StatusNotFound:
		addKeycloakRealmScan(ctx, r, client)
	default:
		r.add(authed(keycloakRealmCheckName, doctorWarn, "",
			fmt.Sprintf("cannot check the Keycloak targets: HTTP %d", resp.StatusCode()), ""))
	}
}

// addKeycloakRealmResult turns the answer of the check route into the
// doctor line. Across tenants, each name carries its tenant id.
func addKeycloakRealmResult(r *doctorReport, check *api.KeycloakRealmCheck) {
	scope := "in your tenant"
	if check.AllTenants {
		scope = "in every tenant"
	}
	if len(check.Items) == 0 {
		r.add(authed(keycloakRealmCheckName, doctorOK, "",
			"every Keycloak target "+scope+" names a plain managed realm", ""))
		return
	}
	names := make([]string, 0, len(check.Items))
	for _, item := range check.Items {
		if check.AllTenants {
			names = append(names, fmt.Sprintf("%s (tenant %s)", item.Name, item.TenantId))
		} else {
			names = append(names, item.Name)
		}
	}
	r.add(authed(keycloakRealmCheckName, doctorWarn, "",
		fmt.Sprintf("these Keycloak targets %s name no managed realm, or a realm that is not a plain name, "+
			"so MEHO refuses every operation on them: %s", scope, strings.Join(names, ", ")),
		keycloakRealmFix))
}

// addKeycloakRealmScan reads the Keycloak targets of the caller's tenant
// one by one, for a backplane without the check route.
func addKeycloakRealmScan(ctx context.Context, r *doctorReport, client *api.AuthedClient) {
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
