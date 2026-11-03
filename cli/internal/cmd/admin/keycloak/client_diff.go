// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"slices"
	"sort"
	"strconv"
	"strings"
)

// clientChanges lists each field that a PUT of desired would change on
// existing, as "field: old -> new". It looks only at the fields that
// desired sends: Keycloak keeps a field that the PUT leaves out (null),
// and an attribute that the PUT does not name. An attribute sent with
// an empty value is removed, so a missing attribute equals "". The
// client scopes are not compared: a client PUT does not change them.
func clientChanges(existing, desired *clientRep) []string {
	var changes []string
	addString := func(field, got, want string) {
		if want != "" && got != want {
			changes = append(changes, fmt.Sprintf("%s: %s -> %s", field, quoted(got), quoted(want)))
		}
	}
	addBool := func(field string, got, want *bool) {
		if want != nil && (got == nil || *got != *want) {
			changes = append(changes, fmt.Sprintf("%s: %s -> %t", field, boolText(got), *want))
		}
	}
	addList := func(field string, got, want []string) {
		if want != nil && !sameSet(got, want) {
			changes = append(changes, fmt.Sprintf("%s: %s -> %s", field, listText(got), listText(want)))
		}
	}
	addString("name", existing.Name, desired.Name)
	addString("description", existing.Description, desired.Description)
	addBool("enabled", existing.Enabled, desired.Enabled)
	addBool("publicClient", existing.PublicClient, desired.PublicClient)
	addBool("standardFlowEnabled", existing.StandardFlowEnabled, desired.StandardFlowEnabled)
	addBool("implicitFlowEnabled", existing.ImplicitFlowEnabled, desired.ImplicitFlowEnabled)
	addBool("directAccessGrantsEnabled", existing.DirectAccessGrantsEnabled, desired.DirectAccessGrantsEnabled)
	addBool("serviceAccountsEnabled", existing.ServiceAccountsEnabled, desired.ServiceAccountsEnabled)
	addBool("frontchannelLogout", existing.FrontchannelLogout, desired.FrontchannelLogout)
	addList("redirectUris", existing.RedirectURIs, desired.RedirectURIs)
	addList("webOrigins", existing.WebOrigins, desired.WebOrigins)
	changes = append(changes, mapChanges("attribute", existing.Attributes, desired.Attributes)...)
	changes = append(changes, mapChanges("login flow override",
		existing.AuthenticationFlowBindingOverrides, desired.AuthenticationFlowBindingOverrides)...)
	return changes
}

// mapChanges lists each key of want whose value differs in got. A
// missing key and an empty value count as the same.
func mapChanges(what string, got, want map[string]string) []string {
	keys := make([]string, 0, len(want))
	for k := range want {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var changes []string
	for _, k := range keys {
		if got[k] != want[k] {
			changes = append(changes, fmt.Sprintf("%s %s: %s -> %s",
				what, k, valueText(got[k]), valueText(want[k])))
		}
	}
	return changes
}

// mapperChanges lists what a PUT of want changes on the mapper got. The
// PUT replaces the whole config, so a key that only got has is removed.
func mapperChanges(got, want protocolMapperRep) []string {
	var changes []string
	if got.ProtocolMapper != want.ProtocolMapper {
		changes = append(changes, fmt.Sprintf("type: %s -> %s", got.ProtocolMapper, want.ProtocolMapper))
	}
	keys := make([]string, 0, len(got.Config)+len(want.Config))
	for k := range got.Config {
		keys = append(keys, k)
	}
	for k := range want.Config {
		if _, ok := got.Config[k]; !ok {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	for _, k := range keys {
		g, hasGot := got.Config[k]
		w, hasWant := want.Config[k]
		switch {
		case !hasWant:
			changes = append(changes, fmt.Sprintf("%s: %s -> (removed)", k, quoted(g)))
		case !hasGot || g != w:
			changes = append(changes, fmt.Sprintf("%s: %s -> %s", k, configText(g, hasGot), quoted(w)))
		}
	}
	return changes
}

func configText(v string, ok bool) string {
	if !ok {
		return "(none)"
	}
	return quoted(v)
}

func valueText(v string) string {
	if v == "" {
		return "(none)"
	}
	return quoted(v)
}

func quoted(v string) string { return strconv.Quote(v) }

func boolText(b *bool) string {
	if b == nil {
		return "(none)"
	}
	return strconv.FormatBool(*b)
}

func listText(l []string) string {
	if len(l) == 0 {
		return "[]"
	}
	return "[" + strings.Join(l, " ") + "]"
}

// sameSet reports whether a and b hold the same values in any order.
// Keycloak keeps redirect URIs and web origins as a set.
func sameSet(a, b []string) bool {
	x, y := slices.Clone(a), slices.Clone(b)
	slices.Sort(x)
	slices.Sort(y)
	return slices.Equal(slices.Compact(x), slices.Compact(y))
}
