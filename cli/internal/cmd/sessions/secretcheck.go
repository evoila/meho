// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"bytes"
	"encoding/json"
	"regexp"
	"strings"
	"unicode/utf8"
)

// The secret check that `meho sessions push` runs on the laptop, before
// a file leaves it. It follows the server's check
// (backend/src/meho_backplane/session_store/secret_check.py), and the
// server checks every upload again:
//
//   - Known secrets are replaced with `[REDACTED:<pattern>]`.
//   - When the check is not sure, the session is held back: a file that
//     is not UTF-8 text, a secret-like name that still has a value, or a
//     private key block in plain text with no END line.
//
// The check works line by line, and a line's result depends only on
// that line (and on an open private key block before it). So when an
// open session grows, the checked file grows at the end too, and the
// server can add the new tail to what it stored before. One more check
// reads all text lines at once, but it can only hold the session back:
// a BIND secret that runs over more than one line.
//
// A JSON line (a JSON Lines transcript) is parsed and each string in it
// is checked, so a replacement never breaks the JSON. A changed line is
// written back as compact JSON; a line with no finding keeps its bytes.

// secretPattern is one known secret shape. When the regexp has a group
// named "s", only that group is replaced, and the rest of the match stays.
// A pattern with find is code, not a regexp: it returns the spans to
// replace.
type secretPattern struct {
	name string
	re   *regexp.Regexp
	find func(text string) [][2]int
	// skip says that a match is no secret (for example a marker).
	skip func(match string) bool
}

const valueChars = `[^\s'"]`

var (
	alreadyMarker = regexp.MustCompile(`[=:]\s*['"]?\[REDACTED:[^\]\s'"]+\]['"]?$`)

	knownSecretPatterns = []secretPattern{
		{name: "pem_private_key", re: regexp.MustCompile(
			`(?s)-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\z)`)},
		{name: "kubeconfig", re: regexp.MustCompile(
			`(?i)apiVersion\s*:\s*v1\s*\n(?:.*\n){0,80}?kind\s*:\s*Config(?:\n(?:.*\n){0,200}?(?:\n|$))?`)},
		{name: "authorization_header", re: regexp.MustCompile(
			`(?i)authorization\s*:\s*[A-Za-z]+\s+` + valueChars + `+`)},
		{name: "bearer_token", re: regexp.MustCompile(`(?i)\bbearer\s+` + valueChars + `{8,}`)},
		{name: "jwt", re: regexp.MustCompile(
			`\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}`)},
		{name: "url_secret", find: urlSecretSpans},
		{name: "password_hash", re: regexp.MustCompile(
			`(?:^|[^A-Za-z0-9$])(?P<s>\$(?:1|2[abxy]|5|6|7|y|gy|sha1|argon2(?:id|i|d))\$` +
				`(?:[^\s$:'"]+\$){1,4}[./A-Za-z0-9+=]{8,})`)},
		{name: "ceph_key", re: regexp.MustCompile(
			`(?i:key)['"]?\s*[=:]\s*['"]?AQ[A-Za-z0-9+/]{30,}={0,2}['"]?`)},
		{name: "bind_key_secret", find: bindSecretSpans},
		{name: "github_token", re: regexp.MustCompile(`\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})`)},
		{name: "aws_access_key", re: regexp.MustCompile(`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`)},
		{name: "api_key", re: regexp.MustCompile(
			`(?i)(?:^|[^A-Za-z0-9])(?P<s>(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token` +
				`|session[_-]?token|secret(?:[_-]?(?:key|id))?|private[_-]?key|password|passwd|pwd` +
				`|client[_-]?secret|token)\s*[=:]\s*['"]?` + valueChars + `{8,}['"]?)`),
			skip: alreadyMarker.MatchString},
	}

	// A secret-like name, maybe quoted or escaped, then ":" or "=", then a
	// value of at least 6 characters, as in the server's check.
	unsureSecret = regexp.MustCompile(
		`(?i)(?:^|[^A-Za-z0-9])(?:password|passwd|pwd|passphrase|secret|client[_-]?secret` +
			`|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|token)` +
			`(?:\\?["'])?\s*[:=]\s*(?:\\?["'])?([^\s"'\\,}]{6,})`)
	secretName = regexp.MustCompile(
		`(?i)(?:^|[_-])(?:password|passwd|pwd|passphrase|secret|client[_-]?secret|api[_-]?key` +
			`|access[_-]?key|secret[_-]?key|private[_-]?key|token)$`)
	notAValue = regexp.MustCompile(`^(?:\[REDACTED|\$|<|\{|\*{3})`)
	// A quoted BIND value that runs to the end of the text: its closing
	// quote is not there yet. See bindValueOpen.
	bindOpenQuoted = regexp.MustCompile(`^"[A-Za-z0-9+/=\s]*$`)

	keyMark = regexp.MustCompile(`-----(BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY-----`)
)

const keyMarker = "[REDACTED:pem_private_key]"

// Reasons why the check is not sure. They never hold a found value.
const (
	reasonNotUTF8      = "not UTF-8 text"
	reasonPossible     = "a secret-like name still has a value"
	reasonOpenKeyBlock = "a private key block has no END line"
	reasonJSONReEncode = "a JSON line could not be written back"
	reasonBindLines    = "a BIND secret runs over more than one line"
)

// checkedFile is the result of the check on one file.
type checkedFile struct {
	Content    []byte
	Redactions int
	// Unsure lists why the check is not sure. Empty when it is sure.
	Unsure []string
}

func (p secretPattern) spans(text string) [][2]int {
	if p.find != nil {
		return p.find(text)
	}
	group := p.re.SubexpIndex("s")
	var spans [][2]int
	for _, m := range p.re.FindAllStringSubmatchIndex(text, -1) {
		start, end := m[0], m[1]
		if group > 0 && m[2*group] >= 0 {
			start, end = m[2*group], m[2*group+1]
		}
		spans = append(spans, [2]int{start, end})
	}
	return spans
}

func (p secretPattern) replace(text string) (string, int) {
	spans := p.spans(text)
	if spans == nil {
		return text, 0
	}
	var out strings.Builder
	last, count := 0, 0
	for _, sp := range spans {
		start, end := sp[0], sp[1]
		if p.skip != nil && p.skip(text[start:end]) {
			continue
		}
		out.WriteString(text[last:start])
		out.WriteString("[REDACTED:" + p.name + "]")
		last = end
		count++
	}
	if count == 0 {
		return text, 0
	}
	out.WriteString(text[last:])
	return out.String(), count
}

func replaceKnown(text string) (string, int) {
	total := 0
	for _, p := range knownSecretPatterns {
		var n int
		text, n = p.replace(text)
		total += n
	}
	return text, total
}

func hasUnsureSecret(text string) bool {
	if bindValueOpen(text) {
		return true
	}
	for _, m := range unsureSecret.FindAllStringSubmatch(text, -1) {
		if !notAValue.MatchString(m[1]) {
			return true
		}
	}
	return false
}

// lineCheck holds the findings of one file while its lines are checked.
type lineCheck struct {
	redactions int
	unsure     bool
	badJSON    bool
	insideKey  bool
	// plain holds the checked text lines; a JSON line adds only its line
	// break. See bindAcrossLines.
	plain strings.Builder
}

func (c *lineCheck) text(value string) string {
	cleaned, n := replaceKnown(value)
	c.redactions += n
	if hasUnsureSecret(cleaned) {
		c.unsure = true
	}
	return cleaned
}

func (c *lineCheck) walk(node any) any {
	switch v := node.(type) {
	case string:
		return c.text(v)
	case []any:
		for i := range v {
			v[i] = c.walk(v[i])
		}
		return v
	case map[string]any:
		for key, value := range v {
			checked := c.walk(value)
			if s, ok := checked.(string); ok && secretName.MatchString(key) &&
				len(s) >= 6 && !notAValue.MatchString(s) {
				c.unsure = true
			}
			v[key] = checked
		}
		return v
	default:
		return node
	}
}

// jsonValue returns the parsed JSON object or array of one line, or nil.
func jsonValue(body string) any {
	trimmed := strings.TrimLeft(body, " \t")
	if !strings.HasPrefix(trimmed, "{") && !strings.HasPrefix(trimmed, "[") {
		return nil
	}
	dec := json.NewDecoder(strings.NewReader(body))
	dec.UseNumber()
	var parsed any
	if err := dec.Decode(&parsed); err != nil || dec.More() {
		return nil
	}
	return parsed
}

func compactJSON(value any) (string, bool) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(value); err != nil {
		return "", false
	}
	return strings.TrimSuffix(buf.String(), "\n"), true
}

// line returns the checked line; "" drops a line inside a private key block.
func (c *lineCheck) line(line string) string {
	checked, isJSON := c.checkLine(line)
	if isJSON || checked == "" {
		c.plain.WriteString("\n")
	} else {
		c.plain.WriteString(checked)
	}
	return checked
}

func (c *lineCheck) checkLine(line string) (string, bool) {
	body := strings.TrimRight(line, "\r\n")
	ending := line[len(body):]
	if c.insideKey {
		if loc := endMark(body); loc >= 0 {
			c.insideKey = false
		}
		return "", false
	}
	if parsed := jsonValue(body); parsed != nil {
		before := c.redactions
		checked := c.walk(parsed)
		if c.redactions == before {
			return line, true
		}
		encoded, ok := compactJSON(checked)
		if !ok {
			c.badJSON = true
			return "", true
		}
		return encoded + ending, true
	}
	if begin := openKeyAt(body); begin >= 0 {
		// A private key starts on this line and ends on a later one: the
		// lines up to its END line become one marker.
		c.insideKey = true
		c.redactions++
		return c.text(body[:begin]) + keyMarker + ending, false
	}
	return c.text(body) + ending, false
}

// openKeyAt returns where a private key block starts that has no END on
// the same line, or -1.
func openKeyAt(body string) int {
	marks := keyMark.FindAllStringSubmatchIndex(body, -1)
	if len(marks) == 0 {
		return -1
	}
	last := marks[len(marks)-1]
	if body[last[2]:last[3]] != "BEGIN" {
		return -1
	}
	return last[0]
}

func endMark(body string) int {
	for _, m := range keyMark.FindAllStringSubmatchIndex(body, -1) {
		if body[m[2]:m[3]] == "END" {
			return m[0]
		}
	}
	return -1
}

// bindAcrossLines says whether the checked text lines still hold a BIND
// secret value. The finder reads one line at a time, so it misses a value
// that starts on a later line than its keyword, or that runs over a line
// break. Reading all text lines at once finds those.
func bindAcrossLines(text string) bool {
	for _, sp := range bindSecretSpans(text) {
		if !strings.HasPrefix(text[sp[0]:sp[1]], "[REDACTED:") {
			return true
		}
	}
	return false
}

// bindValueOpen says whether text ends inside the quoted value of a BIND
// secret: the keyword, then spaces and comments, then a quote that does not
// close. The finder needs the closing quote, so it cannot see that value
// yet. An open session can grow the rest of it later.
func bindValueOpen(text string) bool {
	for _, kw := range bindKeyword.FindAllStringIndex(text, -1) {
		start, end := kw[0], kw[1]
		if start > 0 && !isBindBoundary(text[start-1]) || end < len(text) && !isBindBoundary(text[end]) {
			continue
		}
		if bindOpenQuoted.MatchString(text[skipBindGap(text, end):]) {
			return true
		}
	}
	return false
}

// skipBindGap skips the spaces and comments after pos. An unclosed
// comment runs to the end of the text.
func skipBindGap(text string, pos int) int {
	for {
		pos = skipSpace(text, pos, len(text))
		rest := text[pos:]
		var gap int
		switch {
		case strings.HasPrefix(rest, "/*"):
			gap = strings.Index(rest[2:], "*/")
			if gap >= 0 {
				gap += 4
			}
		case strings.HasPrefix(rest, "//"), strings.HasPrefix(rest, "#"):
			gap = strings.IndexByte(rest, '\n')
		default:
			return pos
		}
		if gap < 0 {
			return len(text)
		}
		pos += gap
	}
}

// checkFile runs the secret check on the bytes of one file.
func checkFile(data []byte) checkedFile {
	if !utf8.Valid(data) {
		return checkedFile{Content: data, Unsure: []string{reasonNotUTF8}}
	}
	var c lineCheck
	var out strings.Builder
	text := string(data)
	for len(text) > 0 {
		cut := strings.IndexByte(text, '\n')
		line := text
		if cut >= 0 {
			line = text[:cut+1]
		}
		text = text[len(line):]
		out.WriteString(c.line(line))
	}
	var unsure []string
	if c.unsure {
		unsure = append(unsure, reasonPossible)
	}
	if plain := c.plain.String(); bindAcrossLines(plain) || bindValueOpen(plain) {
		unsure = append(unsure, reasonBindLines)
	}
	if c.insideKey {
		unsure = append(unsure, reasonOpenKeyBlock)
	}
	if c.badJSON {
		unsure = append(unsure, reasonJSONReEncode)
	}
	return checkedFile{Content: []byte(out.String()), Redactions: c.redactions, Unsure: unsure}
}
