// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	"github.com/getkin/kin-openapi/openapi2"
	"github.com/getkin/kin-openapi/openapi2conv"
	oasyaml "github.com/oasdiff/yaml"
	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/api"
)

// MEHO imports only OpenAPI 3. Some vendors still ship only Swagger 2.0,
// so the CLI converts such a file on the operator's machine before the
// upload. The conversion uses kin-openapi's openapi2conv package; it reads
// only the bytes the CLI already holds and fetches nothing.

// swagger2Conversion records one --spec the CLI converted.
type swagger2Conversion struct {
	URI             string
	ToVersion       string
	Paths           int
	Operations      int
	SecuritySchemes int
}

// convertSwagger2Specs converts every Swagger 2.0 spec whose content the
// CLI read itself (file:// and docs:) to OpenAPI 3 in place. Specs with no
// content (https://, fetched by the backplane) and OpenAPI 3 specs are left
// unchanged. When saveTo is set, the one converted document is also written
// there so the operator can check it.
func convertSwagger2Specs(specs []api.SpecSource, saveTo string) ([]swagger2Conversion, error) {
	var (
		convs []swagger2Conversion
		saved []byte
	)
	for i := range specs {
		src := &specs[i]
		if src.Content == nil || !isSwagger2([]byte(*src.Content)) {
			continue
		}
		out, conv, err := convertSwagger2([]byte(*src.Content))
		if err != nil {
			return nil, fmt.Errorf("--spec %q: Swagger 2.0 conversion failed: %w", src.Uri, err)
		}
		conv.URI = src.Uri
		content := string(out)
		src.Content = &content
		convs = append(convs, conv)
		saved = out
	}
	if saveTo != "" && len(convs) == 0 {
		return nil, errNothingToSave
	}
	if saveTo != "" && len(convs) > 1 {
		return nil, fmt.Errorf(
			"--save-converted keeps one file, but %d --spec values are Swagger 2.0; "+
				"run one ingest per Swagger 2.0 file, or leave out --save-converted", len(convs))
	}
	if saveTo != "" {
		if err := os.WriteFile(saveTo, saved, 0o600); err != nil {
			return nil, fmt.Errorf("--save-converted %q: %w", saveTo, err)
		}
	}
	return convs, nil
}

// isSwagger2 reports whether a spec document says `swagger: "2.0"`. A
// document that does not parse is left to the backplane, which reports
// the parse error the same way it does today.
func isSwagger2(content []byte) bool {
	var head struct {
		Swagger any `json:"swagger" yaml:"swagger"`
	}
	if json.Unmarshal(content, &head) != nil {
		head.Swagger = nil
		if yaml.Unmarshal(content, &head) != nil {
			return false
		}
	}
	return isSwagger2Version(head.Swagger)
}

// isSwagger2Version accepts the string "2.0" and also the number 2, because
// YAML reads an unquoted `swagger: 2.0` as a number.
func isSwagger2Version(v any) bool {
	switch n := v.(type) {
	case string:
		return n == "2.0"
	case float64:
		return n == 2
	case int:
		return n == 2
	}
	return false
}

// convertSwagger2 converts one Swagger 2.0 document (JSON or YAML) to an
// OpenAPI 3 JSON document.
func convertSwagger2(content []byte) ([]byte, swagger2Conversion, error) {
	var conv swagger2Conversion
	asJSON, err := oasyaml.YAMLToJSON(content)
	if err != nil {
		return nil, conv, fmt.Errorf("read document: %w", err)
	}
	var tree any
	if err := json.Unmarshal(asJSON, &tree); err != nil {
		return nil, conv, fmt.Errorf("read document: %w", err)
	}
	if root, ok := tree.(map[string]any); ok && isSwagger2Version(root["swagger"]) {
		if _, isText := root["swagger"].(string); !isText {
			// An unquoted `swagger: 2.0` arrives as a number; the
			// Swagger 2.0 reader wants the text "2.0".
			root["swagger"] = "2.0"
			if asJSON, err = json.Marshal(root); err != nil {
				return nil, conv, fmt.Errorf("read document: %w", err)
			}
		}
	}
	if ref, where, found := findExternalRef(tree, "#", false); found {
		return nil, conv, fmt.Errorf(
			"the reference %q at %s points to another file or a URL; MEHO reads one "+
				"self-contained file and fetches nothing. Put the referenced definitions "+
				"into this file, then try again", ref, where)
	}
	var doc2 openapi2.T
	if err := doc2.UnmarshalJSON(asJSON); err != nil {
		return nil, conv, fmt.Errorf("read document: %w", err)
	}
	inheritRootProduces(&doc2)
	// ToV3 resolves references with a default loader, which refuses any
	// reference to another file or URL. findExternalRef above already
	// refused those with a clearer message.
	doc3, err := openapi2conv.ToV3(&doc2)
	if err != nil {
		return nil, conv, err
	}
	out, err := json.MarshalIndent(doc3, "", "  ")
	if err != nil {
		return nil, conv, fmt.Errorf("write OpenAPI 3 document: %w", err)
	}
	conv.ToVersion = doc3.OpenAPI
	if doc3.Paths != nil {
		for _, item := range doc3.Paths.Map() {
			conv.Paths++
			conv.Operations += len(item.Operations())
		}
	}
	if doc3.Components != nil {
		conv.SecuritySchemes = len(doc3.Components.SecuritySchemes)
	}
	return out, conv, nil
}

// inheritRootProduces copies the document's `produces` list to every
// operation that has none. In Swagger 2.0 an operation without its own
// list inherits the root list; the converter reads only the operation's
// list and falls back to application/json, so an inherited media type
// would be lost. An operation's own list stays as it is.
func inheritRootProduces(doc2 *openapi2.T) {
	if len(doc2.Produces) == 0 {
		return
	}
	for _, item := range doc2.Paths {
		for _, op := range item.Operations() {
			if len(op.Produces) == 0 {
				op.Produces = doc2.Produces
			}
		}
	}
}

// findExternalRef walks a decoded JSON document and returns the first
// `$ref` that does not point inside the same document (it does not start
// with "#"), plus a JSON-pointer-like location. Map keys are visited in
// sorted order so the reported reference is stable. Keys are escaped as in
// a JSON pointer ("~" as "~0", "/" as "~1").
//
// Example data (`example`, `examples`), `default` and `enum` values, and
// `x-` extensions are free data, not part of the spec, so a "$ref" key
// inside them is not a reference and is skipped. In a map of names (named
// is true, for example the keys under `properties` or `definitions`) these
// keys are names, so they are walked. The `default` response under
// `responses` is such a name, so it is still checked.
func findExternalRef(node any, where string, named bool) (string, string, bool) {
	switch v := node.(type) {
	case map[string]any:
		if !named {
			if ref, ok := v["$ref"].(string); ok && !strings.HasPrefix(ref, "#") {
				return ref, where, true
			}
		}
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			if !named && (dataKeys[k] || strings.HasPrefix(k, "x-")) {
				continue
			}
			childNamed := !named && namedMapKeys[k]
			if ref, at, found := findExternalRef(v[k], where+"/"+pointerEscaper.Replace(k), childNamed); found {
				return ref, at, true
			}
		}
	case []any:
		for i, item := range v {
			if ref, at, found := findExternalRef(item, fmt.Sprintf("%s/%d", where, i), false); found {
				return ref, at, true
			}
		}
	}
	return "", "", false
}

// printSwagger2Conversions tells the operator what the CLI converted.
func printSwagger2Conversions(w io.Writer, convs []swagger2Conversion, savedTo string) {
	for _, c := range convs {
		fmt.Fprintf(w,
			"Converted Swagger 2.0 to OpenAPI %s on this machine: %s (%d paths, %d operations, %d login schemes). "+
				"Only the converted file is sent to MEHO.\n",
			c.ToVersion, c.URI, c.Paths, c.Operations, c.SecuritySchemes)
	}
	if savedTo != "" && len(convs) == 1 {
		fmt.Fprintf(w, "Saved the converted file to %s.\n", savedTo)
	}
}

var pointerEscaper = strings.NewReplacer("~", "~0", "/", "~1")

// dataKeys are the Swagger 2.0 keys whose value is free data, not spec
// objects, so a "$ref" key inside them is not a reference.
var dataKeys = map[string]bool{
	"default":  true,
	"enum":     true,
	"example":  true,
	"examples": true,
}

// namedMapKeys are the Swagger 2.0 keys whose value is a map from a name
// the spec author picked to a spec object.
var namedMapKeys = map[string]bool{
	"definitions":         true,
	"parameters":          true,
	"properties":          true,
	"responses":           true,
	"securityDefinitions": true,
}

// errNothingToSave is returned when --save-converted is set but no spec
// was Swagger 2.0.
var errNothingToSave = errors.New(
	"--save-converted was set, but no --spec is a Swagger 2.0 file the CLI reads " +
		"(file:// or docs:); nothing was converted")
