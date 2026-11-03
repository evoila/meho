// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"io"
	"net/http"
	"strings"
)

// dryRunNewID stands for the id of a resource that a dry run would
// create. Reads of a path with this id answer an empty list.
const dryRunNewID = "dry-run-new"

// dryRunTransport lets every read (GET) reach Keycloak and answers
// every other request itself, so a dry run reads the realm and never
// changes it. A create gets 201 with a Location of dryRunNewID; an
// update or delete gets 204.
type dryRunTransport struct {
	next http.RoundTripper
}

func (t dryRunTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.Method == http.MethodGet && !strings.Contains(req.URL.Path, dryRunNewID) {
		return t.next.RoundTrip(req)
	}
	if req.Body != nil {
		_ = req.Body.Close()
	}
	resp := &http.Response{
		StatusCode: http.StatusNoContent,
		Header:     http.Header{},
		Body:       io.NopCloser(strings.NewReader("")),
		Request:    req,
	}
	switch req.Method {
	case http.MethodGet:
		// A list below a resource the dry run would create: still empty.
		resp.StatusCode = http.StatusOK
		resp.Header.Set("Content-Type", "application/json")
		resp.Body = io.NopCloser(strings.NewReader("[]"))
	case http.MethodPost:
		resp.StatusCode = http.StatusCreated
		resp.Header.Set("Location", req.URL.String()+"/"+dryRunNewID)
	}
	return resp, nil
}

// dryRunHTTPClient returns a copy of c whose writes never leave the
// process.
func dryRunHTTPClient(c *http.Client) *http.Client {
	next := c.Transport
	if next == nil {
		next = http.DefaultTransport
	}
	dry := *c
	dry.Transport = dryRunTransport{next: next}
	return &dry
}

// dryRunTags turns the tag of an event line into what a real run
// would do, e.g. "[updated]" into "[would update]".
var dryRunTags = strings.NewReplacer(
	"[created]", "[would create]",
	"[updated]", "[would update]",
	"[added]", "[would add]",
	"[removed]", "[would remove]",
)

// dryRunEvent rewrites the tag at the start of one event line.
func dryRunEvent(line string) string {
	end := strings.Index(line, "]")
	if !strings.HasPrefix(line, "[") || end < 0 {
		return line
	}
	return dryRunTags.Replace(line[:end+1]) + line[end+1:]
}
