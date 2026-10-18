// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package version

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
)

// ReleaseURLBase is the GitHub release page prefix. The CLI and the
// backplane are tagged from the same release, so the backplane's
// version names the CLI build to install.
const ReleaseURLBase = "https://github.com/evoila/meho/releases/tag/"

// Backplane is the build identity GET /version returns.
// ChartVersion is nil when the backplane runs without the helm chart.
type Backplane struct {
	GitSHA       string  `json:"git_sha"`
	BuildDate    string  `json:"build_date"`
	ChartVersion *string `json:"chart_version"`
}

// FetchBackplane reads GET <backplaneURL>/version. The route needs no
// login, so a plain HTTP client works.
func FetchBackplane(ctx context.Context, client *http.Client, backplaneURL string) (Backplane, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, backplaneURL+"/version", http.NoBody)
	if err != nil {
		return Backplane{}, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return Backplane{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return Backplane{}, fmt.Errorf("GET /version returned HTTP %d", resp.StatusCode)
	}
	var bp Backplane
	if err := json.NewDecoder(io.LimitReader(resp.Body, 16*1024)).Decode(&bp); err != nil {
		return Backplane{}, fmt.Errorf("decode /version: %w", err)
	}
	return bp, nil
}

var (
	semverRe = regexp.MustCompile(`^v?(\d+)\.(\d+)\.(\d+)`)
	// Builds from main carry a date-based chart version
	// (0.1.<YYYYMMDD>-<sha>). It does not follow the release numbers,
	// so it cannot be compared with a CLI release.
	calverRe = regexp.MustCompile(`^v?0\.1\.\d{8}-`)
)

// majorMinor returns the major and minor numbers of a release version.
// ok is false for anything that is not a release version ("dev",
// "unknown", a main-branch build).
func majorMinor(v string) (major, minor int, ok bool) {
	v = strings.TrimSpace(v)
	if calverRe.MatchString(v) {
		return 0, 0, false
	}
	m := semverRe.FindStringSubmatch(v)
	if m == nil {
		return 0, 0, false
	}
	major, _ = strconv.Atoi(m[1])
	minor, _ = strconv.Atoi(m[2])
	return major, minor, true
}

// SkewWarning returns a one-line warning when the CLI and the backplane
// are too far apart, or "" when they are in the supported range or
// cannot be compared.
//
// Supported: same major version, and the CLI minor version is equal to
// the backplane's or one lower (N-1). Dev builds and main-branch
// backplane builds are never compared.
func SkewWarning(cliVersion string, bp Backplane) string {
	if bp.ChartVersion == nil {
		return ""
	}
	bpVersion := strings.TrimSpace(*bp.ChartVersion)
	cliMajor, cliMinor, ok := majorMinor(cliVersion)
	if !ok {
		return ""
	}
	bpMajor, bpMinor, ok := majorMinor(bpVersion)
	if !ok {
		return ""
	}
	if cliMajor == bpMajor && (cliMinor == bpMinor || cliMinor == bpMinor-1) {
		return ""
	}
	url := ReleaseURLBase + "v" + strings.TrimPrefix(bpVersion, "v")
	if cliMajor < bpMajor || (cliMajor == bpMajor && cliMinor < bpMinor) {
		return fmt.Sprintf("warning: CLI %s vs backplane %s — this CLI is too old for this backplane. Upgrade: %s",
			cliVersion, bpVersion, url)
	}
	return fmt.Sprintf("warning: CLI %s vs backplane %s — this CLI is newer than the backplane. Install the matching CLI: %s",
		cliVersion, bpVersion, url)
}
