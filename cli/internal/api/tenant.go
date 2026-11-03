// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"net/http"
)

// TenantHeader is the request header that names the current tenant
// (a tenant name or ID). A login in several tenants must send it on
// every call except `meho status`; the backplane answers 400
// tenant_not_chosen otherwise (#5239).
const TenantHeader = "Meho-Tenant"

// TenantEnv is the environment variable that overrides the tenant
// stored by `meho tenants use` for one shell.
const TenantEnv = "MEHO_TENANT"

type tenantKey struct{}

// WithTenant returns a copy of ctx that carries tenant. Requests built
// with that context get the Meho-Tenant header (see SetTenantHeader).
// An empty tenant sends no header, also when an outer context carried
// one: `meho tenants use` relies on that to read the full tenant list.
func WithTenant(ctx context.Context, tenant string) context.Context {
	return context.WithValue(ctx, tenantKey{}, tenant)
}

// TenantFrom returns the tenant carried by ctx, or "".
func TenantFrom(ctx context.Context) string {
	tenant, _ := ctx.Value(tenantKey{}).(string)
	return tenant
}

// SetTenantHeader sets the Meho-Tenant header on req when the
// request's context carries a tenant. The AuthedClient request editor
// calls it for every generated-client call; hand-rolled backplane
// requests call it next to their Authorization header.
func SetTenantHeader(req *http.Request) {
	if tenant := TenantFrom(req.Context()); tenant != "" {
		req.Header.Set(TenantHeader, tenant)
	}
}
