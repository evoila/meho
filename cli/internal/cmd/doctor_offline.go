// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bufio"
	"bytes"
	"context"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/zalando/go-keyring"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/version"
)

// doctorDeps are the outside-world seams the doctor uses. Tests replace
// them; production uses defaultDoctorDeps.
type doctorDeps struct {
	getenv     func(string) string
	executable func() (string, error)
	homeDir    func() (string, error)
	// keyringGet is go-keyring's Get. The value it returns is parsed
	// only to apply the CLI's keyring-versus-file choice (see
	// keyringHit); no field of it is kept or reported.
	keyringGet func(service, user string) (string, error)
	// httpClient carries the per-request timeout of the authenticated
	// layer; layerTimeout bounds that layer as a whole.
	httpClient   *http.Client
	layerTimeout time.Duration
	lookupHost   func(ctx context.Context, host string) ([]string, error)
	// authedOpts is passed to api.NewAuthedClient (tests inject a store
	// and a refresh discoverer).
	authedOpts api.AuthedClientOptions
}

func defaultDoctorDeps() doctorDeps {
	return doctorDeps{
		getenv:       os.Getenv,
		executable:   os.Executable,
		homeDir:      os.UserHomeDir,
		keyringGet:   keyring.Get,
		httpClient:   &http.Client{Timeout: doctorRequestTimeout},
		layerTimeout: doctorLayerTimeout,
		lookupHost:   net.DefaultResolver.LookupHost,
	}
}

// runOfflineChecks runs every check that needs no network and returns
// the backplane URL to use for the authenticated layer ("" if none).
func runOfflineChecks(r *doctorReport, opts doctorOptions, deps doctorDeps) string {
	r.add(checkBinary(deps))
	backplaneURL, cfgCheck := checkConfig(opts.backplaneOverride)
	r.add(cfgCheck)
	r.add(checkCredentialStore(deps, backplaneURL))

	plugin := loadPluginSettings(deps)
	root, shimCheck := checkShim(opts.pluginRoot, deps)
	r.add(shimCheck)
	r.add(checkPluginOrigin(plugin, backplaneURL, shimDefaultScopes(root)))
	r.add(checkVendoredLock(root, plugin))
	r.add(checkCache(root, plugin))
	r.add(checkCABundle(plugin, deps))
	return backplaneURL
}

func offline(name, status, detail, hint string) doctorCheck {
	return doctorCheck{Layer: layerOffline, Name: name, Status: status, Detail: detail, Hint: hint}
}

func checkBinary(deps doctorDeps) doctorCheck {
	path, err := deps.executable()
	if err != nil {
		return offline("binary", doctorWarn, fmt.Sprintf("cannot find the meho binary path: %v", err), "")
	}
	return offline("binary", doctorOK, fmt.Sprintf("%s (version %s)", path, version.Get().Version), "")
}

// checkConfig parses the saved backplane URL (or the --backplane
// override). It makes no call.
func checkConfig(override string) (string, doctorCheck) {
	if override != "" {
		u, err := normalizeBackplaneURL(override)
		if err != nil {
			return "", offline("config", doctorFail, err.Error(), "fix the --backplane value")
		}
		return u, offline("config", doctorOK, "backplane "+u+" (from --backplane)", "")
	}
	path, _ := auth.ConfigPath()
	cfg, err := auth.LoadConfig()
	if errors.Is(err, auth.ErrConfigNotFound) {
		return "", offline("config", doctorWarn, "no saved backplane URL ("+path+" not found)",
			"run `meho login <backplane-url>`")
	}
	if err != nil {
		return "", offline("config", doctorFail, fmt.Sprintf("cannot read %s: %v", path, err),
			"fix or delete the file, then run `meho login <backplane-url>`")
	}
	u, err := normalizeBackplaneURL(cfg.BackplaneURL)
	if err != nil {
		return "", offline("config", doctorFail, fmt.Sprintf("%s: %v", path, err),
			"run `meho login <backplane-url>` to save a valid URL")
	}
	return u, offline("config", doctorOK, "backplane "+u+" (from "+path+")", "")
}

// Credential-store states. Each one is a distinct line in the report.
const (
	storeKeyring            = "keyring"
	storeFileForced         = "file-forced"
	storeFileFallback       = "file-fallback"
	storeKeyringUnavailable = "keyring-unavailable"
)

// checkCredentialStore reports which store holds the login, mirroring
// auth.NewTokenStore's choice: MEHO_KEYRING_DISABLE forces the file; a
// keyring that answers go-keyring's ErrNotFound is available (the entry
// just is not there); any other keyring error means no keyring backend.
// When both stores hold a login, keyringHit applies the same rule the
// CLI's store applies on read.
func checkCredentialStore(deps doctorDeps, backplaneURL string) doctorCheck {
	file, err := auth.NewFileStore()
	if err != nil {
		return offline("credential store", doctorFail, err.Error(), "")
	}
	service, user := auth.DefaultService+"-probe", "meho-availability-check"
	if backplaneURL != "" {
		service, user = auth.KeyForBackplane(backplaneURL)
	}
	var fileTok auth.StoredToken
	fileHas := false
	if backplaneURL != "" {
		var ferr error
		fileTok, ferr = file.Load(service, user)
		fileHas = ferr == nil
	}

	if deps.getenv("MEHO_KEYRING_DISABLE") != "" {
		return storeResult(storeFileForced, file.Describe()+" (MEHO_KEYRING_DISABLE is set)", fileHas, backplaneURL)
	}
	raw, kerr := deps.keyringGet(service, user)
	switch {
	case kerr == nil:
		return keyringHit(raw, file, fileTok, fileHas, backplaneURL)
	case errors.Is(kerr, keyring.ErrNotFound) && fileHas:
		return storeResult(storeFileFallback,
			file.Describe()+" (the keyring works, but the login was too big for it or was saved to the file)", true, backplaneURL)
	case errors.Is(kerr, keyring.ErrNotFound):
		return storeResult(storeKeyring, "OS keyring", false, backplaneURL)
	default:
		return storeResult(storeKeyringUnavailable,
			fmt.Sprintf("%s (no OS keyring backend: %v)", file.Describe(), kerr), fileHas, backplaneURL)
	}
}

// keyringHit describes the store when the keyring holds an entry for
// the login key. The entry is parsed only to apply auth.SecondaryWins,
// the rule the CLI's own store uses when the keyring and the file both
// hold a login; no field of it is kept or reported.
func keyringHit(raw string, file auth.TokenStore, fileTok auth.StoredToken, fileHas bool, backplaneURL string) doctorCheck {
	if backplaneURL == "" {
		return storeResult(storeKeyring, "OS keyring", true, "")
	}
	var keyringTok auth.StoredToken
	if json.Unmarshal([]byte(raw), &keyringTok) != nil {
		return offline("credential store", doctorFail, "store="+storeKeyring+": OS keyring; the saved login is not valid JSON",
			"run `meho login "+backplaneURL+"` to save it again")
	}
	switch {
	case fileHas && auth.SecondaryWins(keyringTok, fileTok):
		return storeResult(storeFileFallback,
			file.Describe()+" (both stores hold a login; the CLI uses the file entry because it is newer or still has a refresh token)",
			true, backplaneURL)
	case fileHas:
		return storeResult(storeKeyring, "OS keyring (the file also holds a login that the CLI does not use)", true, backplaneURL)
	}
	return storeResult(storeKeyring, "OS keyring", true, backplaneURL)
}

func storeResult(state, where string, has bool, backplaneURL string) doctorCheck {
	detail := "store=" + state + ": " + where
	switch {
	case backplaneURL == "":
		return offline("credential store", doctorOK, detail, "")
	case has:
		return offline("credential store", doctorOK, detail+"; a login is stored", "")
	default:
		return offline("credential store", doctorWarn, detail+"; no stored credential for "+backplaneURL,
			"run `meho login "+backplaneURL+"`")
	}
}

// pluginSettings are the values the plugin shim (bin/meho-mcp-remote)
// would use. The shim sources plugin.env after reading the environment,
// so a value in the file wins over the same variable in the environment.
type pluginSettings struct {
	configFile   string
	configExists bool
	values       map[string]string
	origin       map[string]string
	cacheDir     string
}

func (p pluginSettings) get(key string) (string, string) {
	return p.values[key], p.origin[key]
}

var pluginKeys = []string{"MEHO_MCP_URL", "MEHO_INSTANCE", "MEHO_CA_CERT", "MEHO_MCP_SCOPES", "MEHO_PLUGIN_CACHE", "MEHO_MCP_CLIENT_ID"}

func loadPluginSettings(deps doctorDeps) pluginSettings {
	home, _ := deps.homeDir()
	xdgConfig := deps.getenv("XDG_CONFIG_HOME")
	if xdgConfig == "" {
		xdgConfig = filepath.Join(home, ".config")
	}
	p := pluginSettings{values: map[string]string{}, origin: map[string]string{}}
	p.configFile = deps.getenv("MEHO_PLUGIN_CONFIG")
	if p.configFile == "" {
		p.configFile = filepath.Join(xdgConfig, "meho", "plugin.env")
	}
	for _, k := range pluginKeys {
		if v := deps.getenv(k); v != "" {
			p.values[k], p.origin[k] = v, "environment"
		}
	}
	if data, err := os.ReadFile(p.configFile); err == nil {
		p.configExists = true
		for k, v := range parseEnvFile(data) {
			p.values[k], p.origin[k] = v, p.configFile
		}
	}
	p.cacheDir = p.values["MEHO_PLUGIN_CACHE"]
	if p.cacheDir == "" {
		xdgCache := deps.getenv("XDG_CACHE_HOME")
		if xdgCache == "" {
			xdgCache = filepath.Join(home, ".cache")
		}
		p.cacheDir = filepath.Join(xdgCache, "meho", "mcp-remote")
	}
	return p
}

// parseEnvFile reads simple KEY=value lines (optionally prefixed with
// `export`, optionally quoted). It does not run the file.
func parseEnvFile(data []byte) map[string]string {
	out := map[string]string{}
	sc := bufio.NewScanner(bytes.NewReader(data))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		v = strings.TrimSpace(v)
		if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') && v[len(v)-1] == v[0] {
			v = v[1 : len(v)-1]
		}
		out[strings.TrimSpace(k)] = v
	}
	return out
}

// checkPluginOrigin reports where the plugin shim reads its settings
// and which MCP URL and scopes it would use.
func checkPluginOrigin(p pluginSettings, backplaneURL, shimDefault string) doctorCheck {
	cfgNote := p.configFile + " (not present)"
	if p.configExists {
		cfgNote = p.configFile
	}
	mcpURL, origin := p.get("MEHO_MCP_URL")
	if mcpURL == "" {
		if inst, o := p.get("MEHO_INSTANCE"); inst != "" {
			mcpURL, origin = strings.TrimRight(inst, "/")+"/mcp", o+" via MEHO_INSTANCE"
		}
	}
	scopes, scopeOrigin := p.get("MEHO_MCP_SCOPES")
	if strings.TrimSpace(scopes) == "" {
		scopes, scopeOrigin = shimDefault, "shim default"
	}
	scopeNote := "scopes: " + scopeOrigin
	if scopes != "" {
		scopeNote = "scopes: " + scopes + " (" + scopeOrigin + ")"
	}
	if mcpURL == "" {
		return offline("plugin settings", doctorWarn, "plugin config "+cfgNote+"; no MEHO_MCP_URL or MEHO_INSTANCE set",
			"the Claude Code plugin needs MEHO_MCP_URL or MEHO_INSTANCE (environment or plugin.env); ignore this if you do not use the plugin")
	}
	u, err := url.Parse(mcpURL)
	if err != nil || u.Host == "" {
		return offline("plugin settings", doctorFail, fmt.Sprintf("MCP URL %q (from %s) does not parse", mcpURL, origin),
			"fix MEHO_MCP_URL / MEHO_INSTANCE")
	}
	detail := fmt.Sprintf("plugin config %s; MCP URL %s (from %s); %s", cfgNote, mcpURL, origin, scopeNote)
	if backplaneURL != "" {
		if b, berr := url.Parse(backplaneURL); berr == nil && !strings.EqualFold(b.Host, u.Host) {
			return offline("plugin settings", doctorWarn, detail,
				"the plugin points at "+u.Host+" but the CLI points at "+b.Host+"; they are checked against different backplanes")
		}
	}
	return offline("plugin settings", doctorOK, detail, "")
}

// checkShim finds the installed Claude Code plugin and its shim.
func checkShim(flagRoot string, deps doctorDeps) (string, doctorCheck) {
	root, how := flagRoot, "--plugin-root"
	if root == "" {
		root, how = deps.getenv("CLAUDE_PLUGIN_ROOT"), "CLAUDE_PLUGIN_ROOT"
	}
	if root == "" {
		home, _ := deps.homeDir()
		root, how = findPluginRoot(filepath.Join(home, ".claude", "plugins")), "search under ~/.claude/plugins"
	}
	if root == "" {
		return "", offline("plugin shim", doctorSkip, "Claude Code plugin not found",
			"pass --plugin-root <dir> if the plugin is installed somewhere else")
	}
	shim := filepath.Join(root, "bin", "meho-mcp-remote")
	info, err := os.Stat(shim)
	if err != nil {
		return root, offline("plugin shim", doctorFail, fmt.Sprintf("%s not found (plugin root from %s)", shim, how),
			"reinstall the plugin: /plugin install meho@meho")
	}
	if info.Mode()&0o111 == 0 {
		return root, offline("plugin shim", doctorFail, shim+" is not executable", "reinstall the plugin")
	}
	return root, offline("plugin shim", doctorOK, fmt.Sprintf("%s (plugin root from %s)", shim, how), "")
}

// findPluginRoot walks dir (bounded depth, skipping node_modules) for
// bin/meho-mcp-remote next to a package-lock.json, and returns the most
// recently changed match.
func findPluginRoot(dir string) string {
	const maxDepth = 7
	var best string
	var bestTime int64
	base := strings.Count(filepath.Clean(dir), string(os.PathSeparator))
	_ = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if d.Name() == "node_modules" || strings.Count(path, string(os.PathSeparator))-base > maxDepth {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() != "meho-mcp-remote" || filepath.Base(filepath.Dir(path)) != "bin" {
			return nil
		}
		root := filepath.Dir(filepath.Dir(path))
		if _, serr := os.Stat(filepath.Join(root, "package-lock.json")); serr != nil {
			return nil
		}
		if info, ierr := d.Info(); ierr == nil && info.ModTime().UnixNano() > bestTime {
			best, bestTime = root, info.ModTime().UnixNano()
		}
		return nil
	})
	return best
}

// mcpRemoteLockVersion reads the pinned mcp-remote version from a
// package-lock.json.
func mcpRemoteLockVersion(lock []byte) string {
	var doc struct {
		Packages map[string]struct {
			Version string `json:"version"`
		} `json:"packages"`
	}
	if json.Unmarshal(lock, &doc) != nil {
		return ""
	}
	return doc.Packages["node_modules/mcp-remote"].Version
}

// checkVendoredLock mirrors the shim's own `cmp -s` test: the cached
// lock file must be byte-identical to the plugin's committed one, and
// the installed mcp-remote must match the pinned version.
func checkVendoredLock(root string, p pluginSettings) doctorCheck {
	if root == "" {
		return offline("vendored lock", doctorSkip, "no plugin found", "")
	}
	committed, err := os.ReadFile(filepath.Join(root, "package-lock.json"))
	if err != nil {
		return offline("vendored lock", doctorFail, fmt.Sprintf("cannot read the plugin lock file: %v", err), "reinstall the plugin")
	}
	pin := mcpRemoteLockVersion(committed)
	if pin == "" {
		return offline("vendored lock", doctorFail, "the plugin lock file does not pin mcp-remote", "reinstall the plugin")
	}
	cached, err := os.ReadFile(filepath.Join(p.cacheDir, "package-lock.json"))
	if err != nil {
		return offline("vendored lock", doctorWarn, "mcp-remote "+pin+" pinned; not vendored yet",
			"the plugin installs it on its next start (needs npm and access to the npm registry)")
	}
	if !bytes.Equal(committed, cached) {
		return offline("vendored lock", doctorWarn, "mcp-remote "+pin+" pinned; the cached lock differs from the plugin lock",
			"the plugin re-installs on its next start; if that fails, delete "+p.cacheDir)
	}
	installed := ""
	if data, rerr := os.ReadFile(filepath.Join(p.cacheDir, "node_modules", "mcp-remote", "package.json")); rerr == nil {
		var pkg struct {
			Version string `json:"version"`
		}
		if json.Unmarshal(data, &pkg) == nil {
			installed = pkg.Version
		}
	}
	if installed != "" && installed != pin {
		return offline("vendored lock", doctorFail,
			fmt.Sprintf("mcp-remote %s pinned but %s is installed in the cache", pin, installed),
			"delete "+p.cacheDir+" so the plugin re-installs from the lock file")
	}
	return offline("vendored lock", doctorOK, "mcp-remote "+pin+" pinned; cached lock matches the plugin lock", "")
}

func checkCache(root string, p pluginSettings) doctorCheck {
	if root == "" {
		return offline("cache", doctorSkip, "no plugin found", "")
	}
	origin := "default"
	if _, o := p.get("MEHO_PLUGIN_CACHE"); o != "" {
		origin = "MEHO_PLUGIN_CACHE from " + o
	}
	if _, err := os.Stat(filepath.Join(p.cacheDir, "node_modules", "mcp-remote")); err != nil {
		return offline("cache", doctorWarn, fmt.Sprintf("%s (%s): node_modules/mcp-remote not present", p.cacheDir, origin),
			"the plugin fills the cache on its next start")
	}
	return offline("cache", doctorOK, fmt.Sprintf("%s (%s): node_modules/mcp-remote present", p.cacheDir, origin), "")
}

// checkCABundle checks that every configured CA bundle can be read and
// holds at least one certificate.
func checkCABundle(p pluginSettings, deps doctorDeps) doctorCheck {
	type src struct{ name, path string }
	var srcs []src
	if v, _ := p.get("MEHO_CA_CERT"); v != "" {
		srcs = append(srcs, src{"MEHO_CA_CERT", v})
	}
	for _, k := range []string{"NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE"} {
		if v := deps.getenv(k); v != "" {
			srcs = append(srcs, src{k, v})
		}
	}
	if len(srcs) == 0 {
		return offline("CA bundle", doctorOK, "no extra CA bundle set; the system trust store is used", "")
	}
	var notes []string
	for _, s := range srcs {
		n, err := countPEMCerts(s.path)
		if err != nil {
			return offline("CA bundle", doctorFail, fmt.Sprintf("%s=%s: %v", s.name, s.path, err),
				"point "+s.name+" at a readable PEM file")
		}
		notes = append(notes, fmt.Sprintf("%s=%s (%d certificates)", s.name, s.path, n))
	}
	return offline("CA bundle", doctorOK, strings.Join(notes, "; "), "")
}

func countPEMCerts(path string) (int, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, fmt.Errorf("cannot read: %w", err)
	}
	n := 0
	for {
		var block *pem.Block
		block, data = pem.Decode(data)
		if block == nil {
			break
		}
		if block.Type == "CERTIFICATE" {
			if _, perr := x509.ParseCertificate(block.Bytes); perr == nil {
				n++
			}
		}
	}
	if n == 0 {
		return 0, errors.New("no PEM certificate found")
	}
	return n, nil
}

var shimDefaultScopesRe = regexp.MustCompile(`(?m)^default_scopes="([^"]*)"`)

// shimDefaultScopes reads the default scope list from the installed shim
// itself, so the doctor never keeps its own copy that could drift.
func shimDefaultScopes(root string) string {
	if root == "" {
		return ""
	}
	data, err := os.ReadFile(filepath.Join(root, "bin", "meho-mcp-remote"))
	if err != nil {
		return ""
	}
	if m := shimDefaultScopesRe.FindSubmatch(data); m != nil {
		return string(m[1])
	}
	return ""
}
