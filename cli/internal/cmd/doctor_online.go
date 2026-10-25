// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/netdiag"
	"github.com/evoila/meho/cli/internal/version"
)

// doctorProtocolVersion is the MCP revision the doctor asks for in
// initialize. The server answers with the revision it will use.
const doctorProtocolVersion = "2025-06-18"

// mcpResponseLimit caps how much of one MCP answer the doctor reads.
const mcpResponseLimit = 4 << 20

// doctorRequestTimeout bounds one HTTP request of the authenticated
// layer and doctorLayerTimeout bounds the whole layer. A server that
// accepts the connection and then stays silent ends as an "unreachable"
// failure in the report instead of a command that never returns.
const (
	doctorRequestTimeout = 15 * time.Second
	doctorLayerTimeout   = 60 * time.Second
)

func authed(name, status, class, detail, hint string) doctorCheck {
	return doctorCheck{Layer: layerAuthed, Name: name, Status: status, Class: class, Detail: detail, Hint: hint}
}

// failNet adds a failed check whose class comes from the shared network
// classifier.
func failNet(r *doctorReport, name string, err error, host string) {
	class := netdiag.Classify(err)
	if class == netdiag.ClassOther || class == netdiag.ClassNone {
		r.add(authed(name, doctorFail, classUnexpected, err.Error(), ""))
		return
	}
	r.add(authed(name, doctorFail, string(class), err.Error(), netdiag.Hint(class, host)))
}

// runAuthenticatedChecks resolves the backplane, checks the token, and
// runs the MCP handshake. It stops at the first failed step, because
// every later step depends on it.
func runAuthenticatedChecks(ctx context.Context, r *doctorReport, backplaneURL string, deps doctorDeps) {
	if deps.layerTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, deps.layerTimeout)
		defer cancel()
	}
	u, err := url.Parse(backplaneURL)
	if err != nil {
		r.add(authed("dns", doctorFail, classUnexpected, err.Error(), ""))
		return
	}
	host := u.Hostname()
	if !checkDNS(ctx, r, host, deps) {
		return
	}

	opts := deps.authedOpts
	if opts.HTTPClient == nil {
		opts.HTTPClient = deps.httpClient
	}
	opts.ResponseBodyLimit = mcpResponseLimit
	client, err := api.NewAuthedClient(ctx, backplaneURL, opts)
	if err != nil {
		if api.IsTokenNotFound(err) {
			r.add(authed("token", doctorFail, classNotLoggedIn, "no stored login for "+backplaneURL,
				"run `meho login "+backplaneURL+"`"))
			return
		}
		r.add(authed("token", doctorFail, classUnexpected, err.Error(), ""))
		return
	}
	if !checkToken(ctx, r, client, backplaneURL, host) {
		return
	}
	runMCPHandshake(ctx, r, backplaneURL+"/mcp", client, host)
}

func checkDNS(ctx context.Context, r *doctorReport, host string, deps doctorDeps) bool {
	if net.ParseIP(host) != nil {
		r.add(authed("dns", doctorOK, "", host+" is an IP address; no lookup needed", ""))
		return true
	}
	lctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	addrs, err := deps.lookupHost(lctx, host)
	if err != nil {
		r.add(authed("dns", doctorFail, string(netdiag.ClassDNS), err.Error(), netdiag.Hint(netdiag.ClassDNS, host)))
		return false
	}
	r.add(authed("dns", doctorOK, "", fmt.Sprintf("%s resolves (%d addresses)", host, len(addrs)), ""))
	return true
}

// checkToken calls the cheap liveness route (no role gate, no Vault).
// On a 401 it refreshes the token the normal way and tries once more.
func checkToken(ctx context.Context, r *doctorReport, client *api.AuthedClient, backplaneURL, host string) bool {
	resp, err := client.LivenessApiV1HealthLiveGetWithResponse(ctx, nil)
	if err != nil {
		failNet(r, "backplane", err, host)
		return false
	}
	refreshed := false
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			addRefreshFailure(r, rerr, backplaneURL)
			return false
		}
		refreshed = true
		if resp, err = client.LivenessApiV1HealthLiveGetWithResponse(ctx, nil); err != nil {
			failNet(r, "backplane", err, host)
			return false
		}
	}
	switch {
	case resp.StatusCode() == http.StatusUnauthorized:
		r.add(authed("token", doctorFail, classUnauthorized, "the backplane refused the token",
			"run `meho login "+backplaneURL+"`"))
		return false
	case resp.JSON200 == nil:
		r.add(authed("token", doctorFail, classUnexpected,
			fmt.Sprintf("HTTP %d from /api/v1/health/live", resp.StatusCode()), ""))
		return false
	}
	who := resp.JSON200.Operator.Sub
	if n := resp.JSON200.Operator.Name; n != nil && *n != "" {
		who = *n
	}
	detail := "token accepted; signed in as " + who
	if refreshed {
		detail += " (token was refreshed)"
	}
	r.add(authed("token", doctorOK, "", detail, ""))
	return true
}

func addRefreshFailure(r *doctorReport, err error, backplaneURL string) {
	switch {
	case api.IsRefreshRejected(err):
		r.add(authed("token", doctorFail, classRefreshRejected,
			"the login expired: the identity provider rejected the refresh token",
			"run `meho login "+backplaneURL+"` (add --offline for a login that lasts longer)"))
	case api.IsNoRefreshToken(err):
		r.add(authed("token", doctorFail, classNoRefreshToken,
			"the token expired and no refresh token is stored",
			"run `meho login "+backplaneURL+"`"))
	default:
		// The refresh call goes to the identity provider, which may be
		// a different host than the backplane.
		failNet(r, "token refresh", err, "the identity provider host")
	}
}

// mcpSession is a minimal Streamable-HTTP MCP client: one POST per
// JSON-RPC message, JSON or SSE answers, session id echoed back.
type mcpSession struct {
	client    *http.Client
	url       string
	bearer    func() string
	sessionID string
	protocol  string
	nextID    int
}

type mcpHTTPError struct{ status int }

func (e *mcpHTTPError) Error() string { return fmt.Sprintf("HTTP %d from the MCP endpoint", e.status) }

type rpcResponse struct {
	ID     json.RawMessage `json:"id"`
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func (s *mcpSession) post(ctx context.Context, body map[string]any) (*http.Response, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	req.Header.Set("Authorization", "Bearer "+s.bearer())
	api.SetWorkRefHeader(req)
	if s.protocol != "" {
		req.Header.Set("MCP-Protocol-Version", s.protocol)
	}
	if s.sessionID != "" {
		req.Header.Set("Mcp-Session-Id", s.sessionID)
	}
	return s.client.Do(req)
}

// call sends one JSON-RPC request and returns its result.
func (s *mcpSession) call(ctx context.Context, method string, params any) (json.RawMessage, *http.Response, error) {
	s.nextID++
	id := s.nextID
	body := map[string]any{"jsonrpc": "2.0", "id": id, "method": method}
	if params != nil {
		body["params"] = params
	}
	resp, err := s.post(ctx, body)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, resp, &mcpHTTPError{status: resp.StatusCode}
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, mcpResponseLimit))
	if err != nil {
		return nil, resp, err
	}
	rpc, err := decodeRPC(data, resp.Header.Get("Content-Type"), id)
	if err != nil {
		return nil, resp, err
	}
	if rpc.Error != nil {
		return nil, resp, fmt.Errorf("JSON-RPC error %d: %s", rpc.Error.Code, rpc.Error.Message)
	}
	return rpc.Result, resp, nil
}

// decodeRPC reads a JSON-RPC response from a JSON body or, when the
// server chose SSE, from the first `data:` event carrying our id.
func decodeRPC(data []byte, contentType string, id int) (*rpcResponse, error) {
	if !strings.HasPrefix(strings.ToLower(contentType), "text/event-stream") {
		var rpc rpcResponse
		if err := json.Unmarshal(data, &rpc); err != nil {
			return nil, fmt.Errorf("the MCP answer is not JSON: %w", err)
		}
		return &rpc, nil
	}
	want := fmt.Sprint(id)
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 0, 64<<10), mcpResponseLimit)
	for sc.Scan() {
		line, ok := strings.CutPrefix(sc.Text(), "data:")
		if !ok {
			continue
		}
		var rpc rpcResponse
		if json.Unmarshal([]byte(strings.TrimSpace(line)), &rpc) == nil && string(rpc.ID) == want {
			return &rpc, nil
		}
	}
	return nil, errors.New("the MCP event stream ended without an answer")
}

// mcpFail adds a failed MCP step with the right class.
func mcpFail(r *doctorReport, name string, err error, host string) {
	var httpErr *mcpHTTPError
	if errors.As(err, &httpErr) {
		if httpErr.status == http.StatusUnauthorized {
			r.add(authed(name, doctorFail, classUnauthorized,
				"the MCP endpoint refused the token (the REST API accepted it)",
				"the token may lack the MCP audience; ask an admin to run `meho admin keycloak bootstrap-clients`"))
			return
		}
		r.add(authed(name, doctorFail, classUnexpected, err.Error(), ""))
		return
	}
	failNet(r, name, err, host)
}

func runMCPHandshake(ctx context.Context, r *doctorReport, mcpURL string, client *api.AuthedClient, host string) {
	s := &mcpSession{client: client.HTTPClient(), url: mcpURL, bearer: client.AccessToken}

	raw, resp, err := s.call(ctx, "initialize", map[string]any{
		"protocolVersion": doctorProtocolVersion,
		"capabilities":    map[string]any{},
		"clientInfo":      map[string]any{"name": "meho-doctor", "version": version.Get().Version},
	})
	if err != nil {
		mcpFail(r, "mcp initialize", err, host)
		return
	}
	var initRes struct {
		ProtocolVersion string `json:"protocolVersion"`
		ServerInfo      struct {
			Name    string `json:"name"`
			Version string `json:"version"`
		} `json:"serverInfo"`
	}
	_ = json.Unmarshal(raw, &initRes)
	s.protocol = initRes.ProtocolVersion
	s.sessionID = resp.Header.Get("Mcp-Session-Id")
	r.MCPSessionID = s.sessionID
	r.add(authed("mcp initialize", doctorOK, "",
		fmt.Sprintf("protocol %s; server %s %s", initRes.ProtocolVersion, initRes.ServerInfo.Name, initRes.ServerInfo.Version), ""))

	if nresp, nerr := s.post(ctx, map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"}); nerr == nil {
		_ = nresp.Body.Close()
	}

	if raw, _, err = s.call(ctx, "resources/list", nil); err != nil {
		mcpFail(r, "mcp resources/list", err, host)
		return
	}
	var resList struct {
		Resources []json.RawMessage `json:"resources"`
	}
	_ = json.Unmarshal(raw, &resList)
	r.add(authed("mcp resources/list", doctorOK, "", fmt.Sprintf("%d resources", len(resList.Resources)), ""))

	if raw, _, err = s.call(ctx, "tools/list", nil); err != nil {
		mcpFail(r, "mcp tools/list", err, host)
		return
	}
	var toolList struct {
		Tools []struct {
			Name string `json:"name"`
		} `json:"tools"`
	}
	_ = json.Unmarshal(raw, &toolList)
	listed := map[string]bool{}
	for _, t := range toolList.Tools {
		listed[t.Name] = true
	}
	r.add(authed("mcp tools/list", doctorOK, "", fmt.Sprintf("%d tools", len(listed)), ""))

	health, err := callMehoStatus(ctx, s)
	if err != nil {
		mcpFail(r, "meho_status", err, host)
		return
	}
	r.add(authed("meho_status", doctorOK, "", describeStatus(health), ""))
	if health.GrantedScopes != nil {
		r.GrantedScopes = append([]string(nil), (*health.GrantedScopes)...)
	}
	addSurfaceCheck(r, health, listed)
}

func callMehoStatus(ctx context.Context, s *mcpSession) (*api.HealthResponse, error) {
	raw, _, err := s.call(ctx, "tools/call", map[string]any{"name": "meho_status", "arguments": map[string]any{}})
	if err != nil {
		return nil, err
	}
	var res struct {
		IsError bool `json:"isError"`
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		StructuredContent json.RawMessage `json:"structuredContent"`
	}
	if err := json.Unmarshal(raw, &res); err != nil {
		return nil, fmt.Errorf("meho_status answer is not valid: %w", err)
	}
	if res.IsError {
		return nil, errors.New("meho_status returned an error result")
	}
	payload := []byte(res.StructuredContent)
	if len(payload) == 0 && len(res.Content) > 0 {
		payload = []byte(res.Content[0].Text)
	}
	var health api.HealthResponse
	if err := json.Unmarshal(payload, &health); err != nil {
		return nil, fmt.Errorf("meho_status payload is not valid: %w", err)
	}
	return &health, nil
}

func describeStatus(h *api.HealthResponse) string {
	parts := []string{
		"operator " + h.Operator.Sub,
		fmt.Sprintf("vault reachable=%t read_ok=%t", h.Vault.Reachable, h.Vault.ReadOk),
	}
	if h.Db.Migrated != nil {
		parts = append(parts, fmt.Sprintf("db migrated=%t", *h.Db.Migrated))
	}
	return strings.Join(parts, "; ")
}

// addSurfaceCheck compares the working-surface tools the server says
// this session should list with the tools the handshake really saw.
func addSurfaceCheck(r *doctorReport, h *api.HealthResponse, listed map[string]bool) {
	if h.SurfaceSummary == nil || h.SurfaceSummary.ListedWorkingTools == nil {
		r.add(authed("working surface", doctorWarn, "",
			"the backplane does not report a surface summary (older version); cannot compare", ""))
		return
	}
	ss := h.SurfaceSummary
	surface := &doctorSurface{Listed: len(listed), Expected: append([]string(nil), (*ss.ListedWorkingTools)...)}
	if ss.MissingWorkingTools != nil {
		surface.HiddenByServer = append([]string(nil), (*ss.MissingWorkingTools)...)
	}
	if ss.OperatorSurfaceListed != nil {
		surface.OperatorSurface = *ss.OperatorSurfaceListed
	}
	surface.Gap = []string{}
	for _, name := range surface.Expected {
		if !listed[name] {
			surface.Gap = append(surface.Gap, name)
		}
	}
	sort.Strings(surface.Gap)
	r.Surface = surface
	if len(surface.Gap) > 0 {
		r.add(authed("working surface", doctorFail, classSurfaceGap,
			fmt.Sprintf("%d expected tools missing from tools/list: %s", len(surface.Gap), strings.Join(surface.Gap, ", ")),
			"see https://evoila.github.io/meho/clients/mcp-surface-and-scopes/"))
		return
	}
	r.add(authed("working surface", doctorOK, "",
		fmt.Sprintf("all %d expected working-surface tools are listed", len(surface.Expected)), ""))
}
