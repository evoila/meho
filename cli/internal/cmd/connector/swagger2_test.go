// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

// sampleSwagger2YAML is a made-up Swagger 2.0 file: one JSON body
// operation, one file upload (formData), three login definitions, and a
// host + basePath.
const sampleSwagger2YAML = `swagger: "2.0"
info:
  title: Widget API
  version: "1.4"
host: api.widgets.example
basePath: /v1
schemes: [https]
consumes: [application/json]
produces: [application/json]
securityDefinitions:
  basicAuth:
    type: basic
  apiToken:
    type: apiKey
    in: header
    name: X-Widget-Token
  oauth:
    type: oauth2
    flow: application
    tokenUrl: https://login.widgets.example/token
    scopes:
      widgets.read: read widgets
security:
  - basicAuth: []
paths:
  /widgets:
    post:
      operationId: createWidget
      parameters:
        - name: body
          in: body
          required: true
          schema:
            $ref: "#/definitions/Widget"
      responses:
        201:
          description: created
          schema:
            $ref: "#/definitions/Widget"
  /widgets/{id}/image:
    post:
      operationId: uploadWidgetImage
      consumes: [multipart/form-data]
      parameters:
        - name: id
          in: path
          required: true
          type: string
        - name: image
          in: formData
          required: true
          type: file
        - name: caption
          in: formData
          type: string
      responses:
        200:
          description: ok
definitions:
  Widget:
    type: object
    required: [name]
    properties:
      name:
        type: string
`

const sampleOpenAPI3YAML = "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths: {}\n"

func strPtr(s string) *string { return &s }

// convertSample converts sampleSwagger2YAML and decodes the result.
func convertSample(t *testing.T) (map[string]any, swagger2Conversion) {
	t.Helper()
	out, conv, err := convertSwagger2([]byte(sampleSwagger2YAML))
	if err != nil {
		t.Fatalf("convertSwagger2: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(out, &doc); err != nil {
		t.Fatalf("converted output is not JSON: %v", err)
	}
	return doc, conv
}

// dig walks nested maps by key and fails the test when a step is missing.
func dig(t *testing.T, node any, keys ...string) any {
	t.Helper()
	for _, k := range keys {
		m, ok := node.(map[string]any)
		if !ok {
			t.Fatalf("at %q: want an object, got %T", k, node)
		}
		node, ok = m[k]
		if !ok {
			t.Fatalf("missing key %q (path %v)", k, keys)
		}
	}
	return node
}

func TestConvertSwagger2Summary(t *testing.T) {
	doc, conv := convertSample(t)
	if _, has := doc["swagger"]; has {
		t.Error("converted document still has a swagger key")
	}
	if v, _ := doc["openapi"].(string); !strings.HasPrefix(v, "3.0") {
		t.Errorf("openapi = %v, want 3.0.x", doc["openapi"])
	}
	if conv.Paths != 2 || conv.Operations != 2 || conv.SecuritySchemes != 3 {
		t.Errorf("summary = %+v, want 2 paths, 2 operations, 3 login schemes", conv)
	}
}

func TestConvertSwagger2BodyBecomesRequestBody(t *testing.T) {
	doc, _ := convertSample(t)
	op := dig(t, doc, "paths", "/widgets", "post").(map[string]any)
	if params, ok := op["parameters"].([]any); ok && len(params) > 0 {
		t.Errorf("body parameter must not stay a parameter: %v", params)
	}
	ref := dig(t, op, "requestBody", "content", "application/json", "schema", "$ref")
	if ref != "#/components/schemas/Widget" {
		t.Errorf("request body schema ref = %v", ref)
	}
	if dig(t, op, "requestBody", "required") != true {
		t.Error("request body must stay required")
	}
	if dig(t, doc, "components", "schemas", "Widget", "type") != "object" {
		t.Error("definitions must become components.schemas")
	}
}

func TestConvertSwagger2FormDataBecomesFormBody(t *testing.T) {
	doc, _ := convertSample(t)
	op := dig(t, doc, "paths", "/widgets/{id}/image", "post")
	schema := dig(t, op, "requestBody", "content", "multipart/form-data", "schema")
	image := dig(t, schema, "properties", "image").(map[string]any)
	if image["type"] != "string" || image["format"] != "binary" {
		t.Errorf("file field = %v, want type string, format binary", image)
	}
	if dig(t, schema, "properties", "caption", "type") != "string" {
		t.Error("caption form field missing")
	}
	req, _ := dig(t, schema, "required").([]any)
	if len(req) != 1 || req[0] != "image" {
		t.Errorf("required form fields = %v, want [image]", req)
	}
	params, _ := dig(t, op, "parameters").([]any)
	if len(params) != 1 || dig(t, params[0], "in") != "path" {
		t.Errorf("only the path parameter must stay a parameter: %v", params)
	}
}

func TestConvertSwagger2SecurityDefinitionsBecomeSchemes(t *testing.T) {
	doc, _ := convertSample(t)
	schemes := dig(t, doc, "components", "securitySchemes")
	basic := dig(t, schemes, "basicAuth").(map[string]any)
	if basic["type"] != "http" || basic["scheme"] != "basic" {
		t.Errorf("basicAuth = %v, want http basic", basic)
	}
	token := dig(t, schemes, "apiToken").(map[string]any)
	if token["type"] != "apiKey" || token["in"] != "header" || token["name"] != "X-Widget-Token" {
		t.Errorf("apiToken = %v", token)
	}
	tokenURL := dig(t, schemes, "oauth", "flows", "clientCredentials", "tokenUrl")
	if tokenURL != "https://login.widgets.example/token" {
		t.Errorf("oauth token URL = %v", tokenURL)
	}
	sec, _ := doc["security"].([]any)
	if len(sec) != 1 {
		t.Errorf("top-level security = %v, want one requirement", doc["security"])
	}
}

func TestConvertSwagger2HostAndBasePathBecomeServer(t *testing.T) {
	doc, _ := convertSample(t)
	servers, _ := doc["servers"].([]any)
	if len(servers) != 1 || dig(t, servers[0], "url") != "https://api.widgets.example/v1" {
		t.Errorf("servers = %v, want one https://api.widgets.example/v1", servers)
	}
}

func TestConvertSwagger2AcceptsJSON(t *testing.T) {
	in := `{"swagger":"2.0","info":{"title":"t","version":"1"},"basePath":"/api",` +
		`"paths":{"/ping":{"get":{"responses":{"200":{"description":"ok"}}}}}}`
	if !isSwagger2([]byte(in)) {
		t.Fatal("isSwagger2: JSON Swagger 2.0 not detected")
	}
	out, conv, err := convertSwagger2([]byte(in))
	if err != nil {
		t.Fatalf("convertSwagger2: %v", err)
	}
	if conv.Operations != 1 || !strings.Contains(string(out), `"url": "/api"`) {
		t.Errorf("JSON input: conv=%+v out=%s", conv, out)
	}
}

func TestConvertSwagger2RefusesCrossDocumentRef(t *testing.T) {
	for _, ref := range []string{"common.yaml#/definitions/Widget", "https://specs.widgets.example/common.json#/Widget"} {
		in := strings.Replace(sampleSwagger2YAML, `"#/definitions/Widget"`, `"`+ref+`"`, 1)
		_, _, err := convertSwagger2([]byte(in))
		if err == nil {
			t.Fatalf("ref %q: want refusal, got nil", ref)
		}
		if !strings.Contains(err.Error(), ref) {
			t.Errorf("ref %q: error must name the reference: %v", ref, err)
		}
		if !strings.Contains(err.Error(), "#/paths/~1widgets/post/parameters/0/schema") {
			t.Errorf("ref %q: error must say where the reference is: %v", ref, err)
		}
	}
}

func TestConvertSwagger2AcceptsUnquotedVersion(t *testing.T) {
	in := strings.Replace(sampleSwagger2YAML, `swagger: "2.0"`, `swagger: 2.0`, 1)
	if !isSwagger2([]byte(in)) {
		t.Fatal("isSwagger2: unquoted swagger: 2.0 not detected")
	}
	_, conv, err := convertSwagger2([]byte(in))
	if err != nil {
		t.Fatalf("convertSwagger2: %v", err)
	}
	if conv.Operations != 2 {
		t.Errorf("summary = %+v, want 2 operations", conv)
	}
	if isSwagger2([]byte(`openapi: 3.0.3`)) || isSwagger2([]byte(`swagger: 3`)) {
		t.Error("isSwagger2: only version 2.0 counts")
	}
}

func TestConvertSwagger2AllowsRefKeyInExampleData(t *testing.T) {
	in := strings.Replace(sampleSwagger2YAML, "definitions:\n", `x-sample:
  $ref: other.json#/Thing
definitions:
`, 1)
	in = strings.Replace(in, "  Widget:\n    type: object\n", `  Widget:
    type: object
    example:
      $ref: https://specs.widgets.example/sample.json
`, 1)
	if !strings.Contains(in, "example:") || !strings.Contains(in, "x-sample:") {
		t.Fatal("test setup: sample spec changed shape")
	}
	if _, _, err := convertSwagger2([]byte(in)); err != nil {
		t.Fatalf("a $ref key inside example data or an x- extension must not be refused: %v", err)
	}
}

func TestConvertSwagger2RefusesRefInPropertyNamedExample(t *testing.T) {
	in := strings.Replace(sampleSwagger2YAML, "    properties:\n", `    properties:
      example:
        $ref: common.yaml#/definitions/Sample
`, 1)
	if !strings.Contains(in, "common.yaml") {
		t.Fatal("test setup: sample spec changed shape")
	}
	_, _, err := convertSwagger2([]byte(in))
	if err == nil || !strings.Contains(err.Error(), "#/definitions/Widget/properties/example") {
		t.Fatalf("a property named example is a schema; its external $ref must be refused: %v", err)
	}
}

func TestConvertSwagger2SpecsLeavesOthersUnchanged(t *testing.T) {
	specs := []api.SpecSource{
		{Uri: "file:///specs/openapi.yaml", Content: strPtr(sampleOpenAPI3YAML)},
		{Uri: "https://specs.widgets.example/swagger.json"},
		{Uri: "file:///specs/broken.yaml", Content: strPtr("{not: [valid")},
	}
	convs, err := convertSwagger2Specs(specs, "")
	if err != nil {
		t.Fatalf("convertSwagger2Specs: %v", err)
	}
	if len(convs) != 0 {
		t.Errorf("nothing should be converted: %+v", convs)
	}
	if *specs[0].Content != sampleOpenAPI3YAML {
		t.Error("OpenAPI 3 content changed")
	}
	if specs[1].Content != nil {
		t.Error("https spec got content")
	}
	if *specs[2].Content != "{not: [valid" {
		t.Error("unparsable content changed; the backplane must report it")
	}
}

func TestConvertSwagger2SpecsSaveConverted(t *testing.T) {
	dir := t.TempDir()
	saveTo := filepath.Join(dir, "widgets.openapi3.json")
	specs := []api.SpecSource{{Uri: "file:///specs/swagger.yaml", Content: strPtr(sampleSwagger2YAML)}}
	convs, err := convertSwagger2Specs(specs, saveTo)
	if err != nil {
		t.Fatalf("convertSwagger2Specs: %v", err)
	}
	saved, err := os.ReadFile(saveTo) // #nosec G304 -- test temp dir
	if err != nil {
		t.Fatalf("saved file: %v", err)
	}
	if string(saved) != *specs[0].Content {
		t.Error("saved file differs from the uploaded content")
	}
	var out bytes.Buffer
	printSwagger2Conversions(&out, convs, saveTo)
	if !strings.Contains(out.String(), "file:///specs/swagger.yaml") || !strings.Contains(out.String(), saveTo) {
		t.Errorf("summary = %q", out.String())
	}

	t.Run("two Swagger 2.0 specs", func(t *testing.T) {
		two := []api.SpecSource{
			{Uri: "file:///a.yaml", Content: strPtr(sampleSwagger2YAML)},
			{Uri: "file:///b.yaml", Content: strPtr(sampleSwagger2YAML)},
		}
		if _, err := convertSwagger2Specs(two, filepath.Join(dir, "x.json")); err == nil {
			t.Error("want refusal: one file cannot hold two conversions")
		}
	})
	t.Run("nothing to convert", func(t *testing.T) {
		one := []api.SpecSource{{Uri: "file:///a.yaml", Content: strPtr(sampleOpenAPI3YAML)}}
		if _, err := convertSwagger2Specs(one, filepath.Join(dir, "y.json")); err == nil {
			t.Error("want refusal: no Swagger 2.0 spec to save")
		}
	})
}

// onlyHostTransport fails any request to a host other than the allowed
// one, and records it.
type onlyHostTransport struct {
	allowed string
	next    http.RoundTripper
	mu      sync.Mutex
	other   []string
	seen    int
}

func (o *onlyHostTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.URL.Host != o.allowed {
		o.mu.Lock()
		o.other = append(o.other, r.URL.String())
		o.mu.Unlock()
		return nil, io.ErrUnexpectedEOF
	}
	o.mu.Lock()
	o.seen++
	o.mu.Unlock()
	return o.next.RoundTrip(r)
}

// TestRunIngestConvertsSwagger2WithoutNetwork — a file:// Swagger 2.0
// spec goes through `meho connector ingest`: the backplane receives only
// OpenAPI 3 content, and the CLI calls no host but the backplane.
func TestRunIngestConvertsSwagger2WithoutNetwork(t *testing.T) {
	var rawBody []byte
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/ingest": func(w http.ResponseWriter, r *http.Request) {
			rawBody, _ = io.ReadAll(r.Body)
			writeJSON(t, w, 200, api.IngestResponse{
				Ingestion: api.IngestionResultModel{ConnectorId: "widgets-1.4", InsertedCount: 2},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	u, _ := url.Parse(srv.URL)
	guard := &onlyHostTransport{allowed: u.Host, next: http.DefaultTransport}
	orig := http.DefaultTransport
	http.DefaultTransport = guard
	t.Cleanup(func() { http.DefaultTransport = orig })

	specPath := filepath.Join(t.TempDir(), "swagger.yaml")
	if err := os.WriteFile(specPath, []byte(sampleSwagger2YAML), 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	cmd.SetOut(&bytes.Buffer{})
	var stderr bytes.Buffer
	cmd.SetErr(&stderr)

	if err := runIngest(cmd, ingestOptions{
		Product: "widgets", Version: "1.4", ImplID: "widgets-rest",
		Specs:             []string{"file://" + specPath},
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runIngest: %v (stderr %q)", err, stderr.String())
	}
	if guard.seen == 0 {
		t.Fatal("the guard saw no request; it does not watch the CLI's HTTP calls")
	}
	if len(guard.other) != 0 {
		t.Errorf("the CLI called other hosts: %v", guard.other)
	}
	var posted struct {
		Specs []struct {
			URI     string `json:"uri"`
			Content string `json:"content"`
		} `json:"specs"`
	}
	if err := json.Unmarshal(rawBody, &posted); err != nil {
		t.Fatalf("decode posted body: %v", err)
	}
	if len(posted.Specs) != 1 || posted.Specs[0].URI != "file://"+specPath {
		t.Fatalf("posted specs = %+v", posted.Specs)
	}
	var sent map[string]any
	if err := json.Unmarshal([]byte(posted.Specs[0].Content), &sent); err != nil {
		t.Fatalf("sent content is not the converted JSON: %v", err)
	}
	if _, has := sent["swagger"]; has || sent["openapi"] == nil {
		t.Errorf("sent content must be OpenAPI 3 only: openapi=%v swagger=%v", sent["openapi"], sent["swagger"])
	}
	if !strings.Contains(stderr.String(), "Converted Swagger 2.0 to OpenAPI 3.0") {
		t.Errorf("stderr must summarise the conversion: %q", stderr.String())
	}
}
