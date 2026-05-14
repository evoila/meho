// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// TargetSummary is the short list shape returned by GET /api/v1/targets.
type TargetSummary struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Aliases []string `json:"aliases"`
	Product string   `json:"product"`
	Host    string   `json:"host"`
}

// Target is the full read shape returned by GET /api/v1/targets/{name}.
type Target struct {
	ID              string         `json:"id"`
	TenantID        string         `json:"tenant_id"`
	Name            string         `json:"name"`
	Aliases         []string       `json:"aliases"`
	Product         string         `json:"product"`
	Host            string         `json:"host"`
	Port            *int           `json:"port"`
	FQDN            *string        `json:"fqdn"`
	SecretRef       *string        `json:"secret_ref"`
	AuthModel       string         `json:"auth_model"`
	VPNRequired     bool           `json:"vpn_required"`
	Extras          map[string]any `json:"extras"`
	Notes           *string        `json:"notes"`
	Fingerprint     map[string]any `json:"fingerprint,omitempty"`
	PreferredImplID *string        `json:"preferred_impl_id,omitempty"`
	CreatedAt       string         `json:"created_at"`
	UpdatedAt       string         `json:"updated_at"`
}

// ProbeResult is the shape returned by POST /api/v1/targets/{name}/probe.
type ProbeResult struct {
	OK        bool     `json:"ok"`
	Reason    *string  `json:"reason"`
	LatencyMs *float64 `json:"latency_ms"`
	ProbedAt  string   `json:"probed_at"`
}

// TargetErrorDetail is the structured 404/409 detail shape from the backplane.
type TargetErrorDetail struct {
	Error   string          `json:"error"`
	Query   string          `json:"query"`
	Matches []TargetSummary `json:"matches"`
}

// ListTargetsParams holds the optional filters for GET /api/v1/targets.
type ListTargetsParams struct {
	Product *string
	Limit   *int
	Cursor  *string
}

// ListTargets calls GET /api/v1/targets with a one-shot 401-retry.
// Returns the typed slice on success. On error the response body is
// parsed for a structured error detail where possible; otherwise
// the raw status is surfaced.
func (c *AuthedClient) ListTargets(ctx context.Context, params *ListTargetsParams) ([]TargetSummary, int, error) {
	q := url.Values{}
	if params != nil {
		if params.Product != nil && *params.Product != "" {
			q.Set("product", *params.Product)
		}
		if params.Cursor != nil {
			q.Set("cursor", *params.Cursor)
		}
	}
	resp, err := c.doGet(ctx, "/api/v1/targets", q)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == http.StatusOK {
		var targets []TargetSummary
		if jerr := json.Unmarshal(body, &targets); jerr != nil {
			return nil, resp.StatusCode, fmt.Errorf("meho: decode targets list: %w", jerr)
		}
		return targets, resp.StatusCode, nil
	}
	return nil, resp.StatusCode, errFromBody(body, resp.StatusCode)
}

// DescribeTarget calls GET /api/v1/targets/{name} with a one-shot 401-retry.
// Returns (target, statusCode, error). On 404 / 409, error wraps the
// structured detail from the backplane.
func (c *AuthedClient) DescribeTarget(ctx context.Context, name string) (*Target, int, *TargetErrorDetail, error) {
	resp, err := c.doGet(ctx, "/api/v1/targets/"+url.PathEscape(name), nil)
	if err != nil {
		return nil, 0, nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	switch resp.StatusCode {
	case http.StatusOK:
		var t Target
		if jerr := json.Unmarshal(body, &t); jerr != nil {
			return nil, resp.StatusCode, nil, fmt.Errorf("meho: decode target: %w", jerr)
		}
		return &t, resp.StatusCode, nil, nil
	case http.StatusNotFound, http.StatusConflict:
		// FastAPI wraps structured HTTPException detail in {"detail": {...}}.
		var envelope struct {
			Detail TargetErrorDetail `json:"detail"`
		}
		_ = json.Unmarshal(body, &envelope)
		return nil, resp.StatusCode, &envelope.Detail, errFromBody(body, resp.StatusCode)
	default:
		return nil, resp.StatusCode, nil, errFromBody(body, resp.StatusCode)
	}
}

// ProbeTarget calls POST /api/v1/targets/{name}/probe with a one-shot 401-retry.
// On 404 / 409, the structured near-miss detail is returned alongside the error.
func (c *AuthedClient) ProbeTarget(ctx context.Context, name string) (*ProbeResult, int, *TargetErrorDetail, error) {
	resp, err := c.doPost(ctx, "/api/v1/targets/"+url.PathEscape(name)+"/probe", nil)
	if err != nil {
		return nil, 0, nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	switch resp.StatusCode {
	case http.StatusOK:
		var pr ProbeResult
		if jerr := json.Unmarshal(body, &pr); jerr != nil {
			return nil, resp.StatusCode, nil, fmt.Errorf("meho: decode probe result: %w", jerr)
		}
		return &pr, resp.StatusCode, nil, nil
	case http.StatusNotFound, http.StatusConflict:
		var envelope struct {
			Detail TargetErrorDetail `json:"detail"`
		}
		_ = json.Unmarshal(body, &envelope)
		return nil, resp.StatusCode, &envelope.Detail, errFromBody(body, resp.StatusCode)
	default:
		return nil, resp.StatusCode, nil, errFromBody(body, resp.StatusCode)
	}
}

// doGet makes an authenticated GET to backplaneURL+path with optional query
// parameters, retrying once with a refreshed token on 401.
func (c *AuthedClient) doGet(ctx context.Context, path string, query url.Values) (*http.Response, error) {
	return c.doRequest(ctx, http.MethodGet, path, query)
}

// doPost makes an authenticated POST to backplaneURL+path (empty body).
func (c *AuthedClient) doPost(ctx context.Context, path string, query url.Values) (*http.Response, error) {
	return c.doRequest(ctx, http.MethodPost, path, query)
}

func (c *AuthedClient) doRequest(ctx context.Context, method, path string, query url.Values) (*http.Response, error) {
	resp, err := c.rawRequest(ctx, method, path, query)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusUnauthorized {
		return resp, nil
	}
	// 401: close the body we can't use, try a token refresh, then retry.
	_ = resp.Body.Close()
	if rerr := c.box.refresh(ctx); rerr != nil {
		return nil, rerr
	}
	return c.rawRequest(ctx, method, path, query)
}

func (c *AuthedClient) rawRequest(ctx context.Context, method, path string, query url.Values) (*http.Response, error) {
	rawURL := strings.TrimRight(c.backplaneURL, "/") + path
	if len(query) > 0 {
		rawURL += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, nil)
	if err != nil {
		return nil, fmt.Errorf("meho: build request: %w", err)
	}
	bearer := c.box.snapshot()
	if bearer != "" {
		req.Header.Set("Authorization", authorizationHeader(bearer))
	}
	req.Header.Set("Accept", "application/json")
	return c.httpClient.Do(req)
}

// errFromBody constructs a simple error from the HTTP status and response body.
// FastAPI wraps string details in {"detail": "..."} and dict details in
// {"detail": {...}}. We extract the string form if present; otherwise fall
// back to the raw status code.
func errFromBody(body []byte, status int) error {
	trimmed := strings.TrimSpace(string(body))
	if len(trimmed) == 0 || len(trimmed) > 512 {
		return fmt.Errorf("meho: HTTP %d", status)
	}
	// Try string detail first (most 401/403/501 responses).
	var envelope struct {
		Detail string `json:"detail"`
	}
	if jerr := json.Unmarshal(body, &envelope); jerr == nil && envelope.Detail != "" {
		return fmt.Errorf("meho: HTTP %d: %s", status, envelope.Detail)
	}
	return fmt.Errorf("meho: HTTP %d", status)
}
