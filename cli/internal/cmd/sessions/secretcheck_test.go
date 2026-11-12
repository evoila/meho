// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"encoding/json"
	"strings"
	"testing"
)

// All values here are made up. The key marks are built from parts, so
// the secret scanners of the repo do not take the fixtures for keys.
const (
	keyKind  = "PRIVATE " + "KEY-----"
	keyBegin = "-----BEGIN " + keyKind
	keyEnd   = "-----END " + keyKind
)

func TestCheckReplacesKnownSecretsInText(t *testing.T) {
	cases := map[string]string{
		"authorization_header": "Authorization: Basic bWFkZTp1cA==",
		"bearer_token":         "use Bearer madeUpOpaque123 now",
		"jwt":                  "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJlMTIz",
		"url_secret":           "https://alice:madeUpPass@example.com/x",
		"api_key":              "DB_PASSWORD=madeUpPassword1",
		"github_token":         "ghp_" + strings.Repeat("Ab12", 9),
		"aws_access_key":       "AKIA" + "ABCDEFGHIJKLMNOP",
		"pem_private_key":      keyBegin + "abc" + keyEnd,
	}
	for name, text := range cases {
		got := checkFile([]byte(text + "\n"))
		if !strings.Contains(string(got.Content), "[REDACTED:"+name+"]") || got.Redactions == 0 {
			t.Errorf("%s: got %q", name, got.Content)
		}
		if len(got.Unsure) != 0 {
			t.Errorf("%s: unsure %v", name, got.Unsure)
		}
	}
}

func TestCheckKeepsTheSafePartOfAURL(t *testing.T) {
	got := string(checkFile([]byte("get https://example.com/f?X-Amz-Signature=abc123&expires=60\n")).Content)
	if !strings.Contains(got, "example.com/f?X-Amz-Signature=[REDACTED:url_secret]&expires=60") {
		t.Fatalf("got %q", got)
	}
}

func TestCheckLeavesNormalTextAndAMarker(t *testing.T) {
	for _, text := range []string{
		"restart vm web-01 on host 10.0.0.1\n",
		"password: [REDACTED:api_key]\n",
		`{"type":"user","message":"hello"}` + "\n",
	} {
		got := checkFile([]byte(text))
		if string(got.Content) != text || got.Redactions != 0 || len(got.Unsure) != 0 {
			t.Errorf("%q changed: %q %v", text, got.Content, got.Unsure)
		}
	}
}

func TestCheckKeepsJSONLinesValid(t *testing.T) {
	line := `{"b":1,"msg":"token=madeUpTokenValue1 \"q\" <x>","n":1.50}` + "\n"
	got := checkFile([]byte(line))
	var parsed map[string]any
	if err := json.Unmarshal(got.Content, &parsed); err != nil {
		t.Fatalf("not JSON: %v; %q", err, got.Content)
	}
	if !strings.Contains(parsed["msg"].(string), "[REDACTED:api_key]") {
		t.Fatalf("msg = %q", parsed["msg"])
	}
	if !strings.Contains(string(got.Content), `"n":1.50`) || !strings.Contains(string(got.Content), "<x>") {
		t.Fatalf("numbers or text changed: %q", got.Content)
	}
}

func TestCheckIsNotSure(t *testing.T) {
	cases := map[string]string{
		reasonPossible:     `{"input":{"api_key":"madeup1"}}` + "\n",
		reasonOpenKeyBlock: "-----BEGIN RSA " + keyKind + "\nmadeup\n",
	}
	for reason, text := range cases {
		got := checkFile([]byte(text))
		if len(got.Unsure) != 1 || got.Unsure[0] != reason {
			t.Errorf("%q: unsure = %v", text, got.Unsure)
		}
	}
	got := checkFile([]byte{0xff, 0xfe, 'a'})
	if len(got.Unsure) != 1 || got.Unsure[0] != reasonNotUTF8 {
		t.Fatalf("not UTF-8: %v", got.Unsure)
	}
	// A variable or a placeholder is no value.
	if got := checkFile([]byte(`{"password":"${DB_PASS}","token":"<token>"}` + "\n")); len(got.Unsure) != 0 {
		t.Fatalf("placeholders: %v", got.Unsure)
	}
}

func TestCheckRemovesAPrivateKeyOverManyLines(t *testing.T) {
	text := "before\n-----BEGIN OPENSSH " + keyKind + "\nmadeupline1\nmadeupline2\n" +
		"-----END OPENSSH " + keyKind + "\nafter\n"
	got := checkFile([]byte(text))
	want := "before\n" + keyMarker + "\nafter\n"
	if string(got.Content) != want || len(got.Unsure) != 0 {
		t.Fatalf("got %q, unsure %v", got.Content, got.Unsure)
	}
}

func TestCheckGrowsOnlyAtTheEnd(t *testing.T) {
	// An open session grows at the end. The checked file must grow at the
	// end too, so the server can add the new tail.
	first := `{"msg":"Bearer madeUpOpaque123"}` + "\n" + "plain text\n" +
		keyBegin + "\nmadeup\n" + keyEnd + "\n"
	grown := first + `{"msg":"token=madeUpTokenValue2"}` + "\nmore\n"
	a := checkFile([]byte(first))
	b := checkFile([]byte(grown))
	if !strings.HasPrefix(string(b.Content), string(a.Content)) {
		t.Fatalf("%q is no prefix of %q", a.Content, b.Content)
	}
}

func TestCheckReplacesBINDKeySecrets(t *testing.T) {
	cases := []string{
		`key "rndc-key" { algorithm hmac-sha256; secret "bWFkZVVwU2VjcmV0VmFsdWU=" ; };`,
		`options { cookie-secret "0123456789abcdef0123456789abcdef"; };`,
		`secret /* the key */ bWFkZVVwU2VjcmV0VmFsdWU=;`,
		`# secret "bWFkZVVwU2VjcmV0VmFsdWU=";`,
	}
	for _, text := range cases {
		got := checkFile([]byte(text + "\n"))
		if strings.Contains(string(got.Content), "bWFkZVVwU2VjcmV0VmFsdWU=") ||
			strings.Contains(string(got.Content), "0123456789abcdef0123456789abcdef") ||
			!strings.Contains(string(got.Content), "[REDACTED:bind_key_secret]") || len(got.Unsure) != 0 {
			t.Errorf("%q: got %q, unsure %v", text, got.Content, got.Unsure)
		}
	}
	// The keyword in a string and plain prose are left alone.
	for _, text := range []string{
		`key "transfer-secret" { algorithm hmac-sha256; };`,
		"the secret is in the vault",
	} {
		got := checkFile([]byte(text + "\n"))
		if string(got.Content) != text+"\n" || len(got.Unsure) != 0 {
			t.Errorf("%q changed: %q, unsure %v", text, got.Content, got.Unsure)
		}
	}
}

func TestCheckHoldsBackABINDSecretOverTwoLines(t *testing.T) {
	got := checkFile([]byte("secret \"bWFkZVVw\n  U2VjcmV0VmFsdWU=\";\n"))
	if len(got.Unsure) != 1 || got.Unsure[0] != reasonPossible {
		t.Fatalf("unsure = %v", got.Unsure)
	}
}

func TestCheckReplacesEverySecretPartOfAURL(t *testing.T) {
	cases := map[string]string{
		"https://madeUpToken99@example.com/repo.git":                            "madeUpToken99",
		"https://example.com/f?%74oken=madeUpValue42":                           "madeUpValue42",
		"https://example.com/f?accessToken=madeUpValue43":                       "madeUpValue43",
		"https://example.com/f?a=1&amp;sig=madeUpValue44":                       "madeUpValue44",
		"see 'https://example.com/a?sig=madeUpValue45','https://example.com/b'": "madeUpValue45",
	}
	for text, secret := range cases {
		got := checkFile([]byte(text + "\n"))
		if strings.Contains(string(got.Content), secret) || !strings.Contains(string(got.Content), "[REDACTED:url_secret]") {
			t.Errorf("%q: got %q", text, got.Content)
		}
		if !strings.Contains(string(got.Content), "example.com/") {
			t.Errorf("%q: the host is gone: %q", text, got.Content)
		}
	}
	// A link with no secret part, and a scheme glued to a word, stay.
	for _, text := range []string{"https://example.com/f?expires=60", "xhttps://a:b"} {
		if got := checkFile([]byte(text + "\n")); string(got.Content) != text+"\n" {
			t.Errorf("%q changed: %q", text, got.Content)
		}
	}
}
