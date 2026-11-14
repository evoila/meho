// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"fmt"
	"regexp"
)

// ProfileEnv is the environment variable the root --profile flag falls
// back to when the flag is not set.
const ProfileEnv = "MEHO_PROFILE"

// DefaultProfile is the profile used when none is chosen. Its login is
// stored exactly where logins were stored before profiles existed, so
// a person who never uses --profile sees no change.
const DefaultProfile = "default"

var profileNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

// ValidProfileName reports whether name is a usable profile name:
// letters, digits, dot, dash and underscore, starting with a letter or
// digit, at most 64 characters.
func ValidProfileName(name string) bool {
	return profileNamePattern.MatchString(name)
}

type profileKey struct{}

// WithProfile returns a copy of ctx that carries the profile name. An
// empty name returns ctx unchanged.
func WithProfile(ctx context.Context, name string) context.Context {
	if name == "" {
		return ctx
	}
	return context.WithValue(ctx, profileKey{}, name)
}

// ProfileFrom returns the profile carried by ctx. It is DefaultProfile
// when ctx carries none (or ctx is nil).
func ProfileFrom(ctx context.Context) string {
	if ctx != nil {
		if name, _ := ctx.Value(profileKey{}).(string); name != "" {
			return name
		}
	}
	return DefaultProfile
}

// KeyForProfile returns the (service, user) addressing for one profile
// on one backplane. The default profile uses the same key as
// KeyForBackplane. A named profile adds a "#profile=<name>" suffix to
// the service, so every profile has its own slot in the same keyring or
// the same credentials file (mode 0600, same folder). The suffix is on
// the service, not on the URL: a URL can hold a '#' itself, so a suffix
// there could make two (URL, profile) pairs share one slot.
func KeyForProfile(backplaneURL, profile string) (service, user string) {
	service, user = KeyForBackplane(backplaneURL)
	if profile == "" || profile == DefaultProfile {
		return service, user
	}
	return service + "#profile=" + profile, user
}

// KeyForContext is KeyForProfile for the profile that ctx carries.
func KeyForContext(ctx context.Context, backplaneURL string) (service, user string) {
	return KeyForProfile(backplaneURL, ProfileFrom(ctx))
}

// ResolveProfile picks the profile name: the flag wins over the
// environment value, and with neither it is DefaultProfile. A name
// that is not valid is an error that says where it came from.
func ResolveProfile(flagValue string, flagChanged bool, envValue string) (string, error) {
	name, source := "", ""
	switch {
	case flagChanged:
		name, source = flagValue, "--profile"
	case envValue != "":
		name, source = envValue, ProfileEnv
	default:
		return DefaultProfile, nil
	}
	if !ValidProfileName(name) {
		return "", fmt.Errorf("%s %q is not a valid profile name: use letters, digits, '.', '-' or '_' (at most 64 characters, starting with a letter or digit)", source, name)
	}
	return name, nil
}
