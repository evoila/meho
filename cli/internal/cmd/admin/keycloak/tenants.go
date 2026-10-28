// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/google/uuid"
)

// One person can be in role groups of several tenants
// (docs/decisions/2026-10-28-several-tenants-per-person.md). Every role
// group carries `meho_tenant` = `<tenant-uuid>:<tenant>:<Role>`, and the
// `meho-tenants` mapper copies the values of all the person's groups
// into the `meho_tenants` claim.

// platformAdminGroupName is the group whose members MEHO treats as
// platform admins. It holds no tenant and no role, so it is not a role
// group. The first admin user joins it.
const platformAdminGroupName = "MEHO-Platform-Admins"

// defaultTenantName is the first tenant's short name unless
// --tenant-name says otherwise. Same default as the chart.
const defaultTenantName = "main"

// tenantNamePattern is a tenant's short name: lowercase letters, digits
// and dashes. The name goes into group names and into the colon-separated
// `meho_tenant` value, so it never holds a colon.
var tenantNamePattern = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)

// reservedTenantNames would make extra-tenant group names that differ from
// a shipped group only in case (MEHO-sa-Operators, MEHO-platform-Admins,
// MEHO-ci-Admins).
var reservedTenantNames = []string{"sa", "platform", "ci"}

// groupSpec is one group the command makes and keeps up to date. Only the
// keys in Owned are set or removed; every other attribute is left alone.
type groupSpec struct {
	Name  string
	Attrs map[string][]string
	Owned []string
}

// mehoTenantValue is the `meho_tenant` value of a role group.
func mehoTenantValue(tenantID, tenantName, role string) string {
	return tenantID + ":" + tenantName + ":" + role
}

func validateTenantName(flag, name string) error {
	if !tenantNamePattern.MatchString(name) {
		return fmt.Errorf("%s %q: use lowercase letters, digits and dashes "+
			"(at most 63 characters, no dash at the start or the end)", flag, name)
	}
	if slices.Contains(reservedTenantNames, name) {
		return fmt.Errorf("%s %q: this name is taken by a group that ships "+
			"with MEHO; pick another name", flag, name)
	}
	return nil
}

// validateTenants checks the tenant flags. Called from validate.
func (o BootstrapOptions) validateTenants() error {
	if err := validateTenantName("--tenant-name", o.TenantName); err != nil {
		return err
	}
	if o.AddTenantName == "" {
		if o.AddTenantID != "" {
			return errors.New("--add-tenant-id needs --add-tenant <name>")
		}
		return nil
	}
	if err := validateTenantName("--add-tenant", o.AddTenantName); err != nil {
		return err
	}
	if o.AddTenantName == o.TenantName {
		return fmt.Errorf("--add-tenant %q is the first tenant's name "+
			"(--tenant-name); pick another name", o.AddTenantName)
	}
	if o.AddTenantID != "" {
		if _, err := uuid.Parse(o.AddTenantID); err != nil {
			return fmt.Errorf("--add-tenant-id %q is not a UUID", o.AddTenantID)
		}
		if strings.EqualFold(o.AddTenantID, o.TenantID) {
			return errors.New("--add-tenant-id is the first tenant's id " +
				"(--tenant-id); pick another UUID")
		}
	}
	return nil
}

// firstTenantGroups are the five role groups of the first tenant. They
// keep `tenant_id`, `tenant_role` and `approver`, and carry `meho_tenant`.
func (o BootstrapOptions) firstTenantGroups() []groupSpec {
	specs := make([]groupSpec, 0, len(roleGroups))
	for _, g := range roleGroups {
		specs = append(specs, groupSpec{
			Name:  g.Name,
			Attrs: g.desiredAttributes(o.TenantID, o.TenantName),
			Owned: roleGroupAttributeKeys,
		})
	}
	return specs
}

// extraTenantGroups are the five groups `MEHO-<tenant>-<Role>` of an extra
// tenant. They carry only `meho_tenant`: with no `tenant_id` or
// `tenant_role`, an older MEHO refuses a person who is only in these
// groups. The command owns the same keys as on the first tenant's groups,
// so a `tenant_id` set on one by hand is removed.
func extraTenantGroups(tenantID, tenantName string) []groupSpec {
	specs := make([]groupSpec, 0, len(roleGroups))
	for _, g := range roleGroups {
		specs = append(specs, groupSpec{
			Name: "MEHO-" + tenantName + "-" + g.Role,
			Attrs: map[string][]string{
				"meho_tenant": {mehoTenantValue(tenantID, tenantName, g.Role)},
			},
			Owned: roleGroupAttributeKeys,
		})
	}
	return specs
}

// platformAdminGroup sets `platform_admin=true`, like the built-in realm.
func platformAdminGroup() groupSpec {
	return groupSpec{
		Name:  platformAdminGroupName,
		Attrs: map[string][]string{"platform_admin": {"true"}},
		Owned: []string{"platform_admin"},
	}
}

// tenantsMapper copies `meho_tenant` from every group of the person into
// the `meho_tenants` claim, a list of strings. Multivalued and aggregate
// on: Keycloak then collects the values of all groups
// (KeycloakModelUtils.resolveAttribute).
func tenantsMapper() protocolMapperRep {
	return protocolMapperRep{
		Name:           "meho-tenants",
		Protocol:       "openid-connect",
		ProtocolMapper: "oidc-usermodel-attribute-mapper",
		Config: map[string]string{
			"user.attribute":            "meho_tenant",
			"claim.name":                "meho_tenants",
			"jsonType.label":            "String",
			"multivalued":               "true",
			"aggregate.attrs":           "true",
			"id.token.claim":            "false",
			"access.token.claim":        "true",
			"userinfo.token.claim":      "true",
			"introspection.token.claim": "true",
		},
	}
}

// resolveAddTenantID returns the id of the tenant --add-tenant names. An
// id given with --add-tenant-id wins. Else the id the tenant's groups
// already carry is reused, so a second run without --add-tenant-id
// changes nothing. Else a new UUID is made (created=true).
func resolveAddTenantID(
	ctx context.Context, c *adminClient, opts BootstrapOptions,
) (id string, created bool, err error) {
	if opts.AddTenantID != "" {
		return strings.ToLower(opts.AddTenantID), false, nil
	}
	for _, spec := range extraTenantGroups("", opts.AddTenantName) {
		found, ferr := c.findGroup(ctx, spec.Name)
		if ferr != nil {
			return "", false, fmt.Errorf("find group %s: %w", spec.Name, ferr)
		}
		if found == nil {
			continue
		}
		existing, gerr := c.getGroup(ctx, found.ID)
		if gerr != nil {
			return "", false, fmt.Errorf("read group %s: %w", spec.Name, gerr)
		}
		for _, v := range existing.Attributes["meho_tenant"] {
			head, _, _ := strings.Cut(v, ":")
			if _, perr := uuid.Parse(head); perr == nil {
				return strings.ToLower(head), false, nil
			}
		}
	}
	return uuid.NewString(), true, nil
}
