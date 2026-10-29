# Shared API file secret check

## Overview

`backend/src/meho_backplane/connectors/_shared/api_file_secrets.py` checks
every API description file before a read returns it. An API description
file is a file an appliance serves to describe its own API: a Swagger or
OpenAPI file (JSON or YAML), a WSDL or XSD file (XML) or a documentation
page (HTML).

The helper reads the file with a real parser for its format and replaces
every value under a secret name with the placeholder
`[REDACTED:param_name]`. It then writes the file back from the parsed
copy. So a secret is the value under a secret-named key, wherever it
hides and however it is written. A text scan was tried first (#4690,
#4731) and lost a race against every new trick: a value on the next line,
a YAML alias, an escaped key, a quoted `>` inside an HTML attribute. The
parser sees the structure, so those tricks do not exist for it.

Every API file read uses this helper (#4780): `fleet.api_file.get`,
`hcx.api_file.get` and `vcenter.sso_admin.api_file.get` on main, and the
reads of #4690 (VCF Operations) and #4731 (Aria Automation 8) after it.
New API file reads use it from the start; `srm.api_file.get`,
`vr.api_file.get` and `vrli.api_file.get` (Logs 8.18 and 9.0) do. These
reads also give the helper MEHO's general redaction rules as a text rule
(`text_redactor` in `connectors/_shared/api_file_read.py`), so text they
match comes back as a placeholder instead of stopping the read. The rule
runs on each parsed text value before the copy is written back, so the
copy keeps its format and every value keeps its type.

## Key types

- `check_api_file(text, *, content_type, path, text_rule=None) ->
  CheckedApiFile` — the entry point. Returns the checked copy (`text`)
  and the number of replaced values (`secrets_replaced`). Raises
  `ApiFileFormatError` when the format is unknown or the file does not
  parse. An empty file comes back empty. `text_rule` is an optional
  function for one text value: it returns the changed value and how many
  places changed. It runs on every text value that is not replaced
  whole, in every format, and its count joins `secrets_replaced`.
- `detect_format(content_type, path)` — `json`, `yaml`, `xml`, `html` or
  `None`. The content type decides. When it is `text/plain` or missing,
  the file ending decides (`.json`, `.yaml`, `.yml`, `.xml`, `.wsdl`,
  `.xsd`, `.html`, `.htm`). YAML is never tried on other text: almost any
  text is valid YAML.
- `scrub_query_string(path) -> (path, count)` — the same check for the
  query of a redirect path. The names are decoded first, so
  `license%4Bey` reads as `licenseKey`.
- `REDACTED` — the placeholder, the same one the governed Vault read
  returns for a secret-named value.
- `ApiFileFormatError` (a `ValueError`) — the message names the reason
  (format, line, column), never the text.

The three sibling modules hold the parts: `api_file_values.py` has the
`Scrubber` walk over parsed values, the placeholder and the search for
JSON documents inside a text value; `api_file_yaml.py` has the YAML
check on the node graph, for a YAML file and for a YAML example in an
HTML page; `api_file_markup.py` has the XML and HTML parsers and writers.

## What counts as a secret name

MEHO's one rule for secret-named fields,
`is_secret_param_name` in `broadcast/events.py`: `password`, `passwd`,
`pwd`, `passphrase`, `apikey`, `kubeconfig` anywhere in the name;
`secret`, `secrets`, `token`, `credential`, `credentials` at the end of
the name; and the pairs `api_key`, `access_key`, `private_key`,
`encryption_key`, `license_key`, `licence_key`, `secret_id`,
`secret_value`, `secret_data`. Names are read in snake, camel and kebab
case. #4780 added the licence key.

The helper adds one rule of its own (`is_secret_name` in
`api_file_values.py`): a name that ends in a session id (`sessionId`,
`session_id`, `X-Session-Id`, `JSESSIONID`) is a secret name too. Some
appliances use the session id as the login token; the Logs session API
returns its token as `sessionId`. The rule stays in the helper, not in
MEHO's general rule, because elsewhere a `session_id` is often MEHO's own
agent session, which is no secret. A name that only starts with
`session` (`sessionTimeout`) is not a secret name.

There is no exception for schema words. The value under a secret name
is replaced even when it is the description of a field (`"password":
{"type": "string"}`) or a path item (`"/reset-password": {...}`). This
loses a little detail; `secrets_replaced` tells the reader how much. In
return, no trick can use an exception: the exception for schema words
is how `{"credentials": {"default": "..."}}` got through a text scan.

## Control flow

1. Pick the parser from the content type, or from the file ending.
2. Parse. JSON with `json`; YAML with PyYAML's safe loader; XML with
   `defusedxml` (a file that declares entities is refused); HTML with
   `html.parser`. A parse error names the kind of error and the line
   and column, never the text there (a YAML error message would quote
   an unknown tag). A YAML value that does not fit its tag (`!!int
   made-up`) fails outside PyYAML's own errors, in the stdlib call that
   reads it, and that error quotes the value; the file is refused with
   the error class only.
3. Replace:
   - JSON and YAML: the whole value of every secret-named key, at any
     depth, also inside lists, whatever it is (text, number, list,
     object, `null`). A YAML alias (`*name`) is the same node as its
     anchor, so the secret is marked on the node graph before the values
     are built, and every place that points at it (anchor, alias, a
     mapping merged with `<<`) gets the placeholder.
   - XML and HTML: the whole content of a secret-named element, and the
     value of a secret-named attribute. An element whose `name`, `id`,
     `key` or `type` attribute is a secret name (`<input type="password"
     value="...">`) loses its content and every attribute value except
     the label that marks it: `<input name="password" id="abc">` comes
     back with `id="[REDACTED:param_name]"`, and `<xsd:element
     name="password" type="xsd:string"/>` loses its `type`. Every
     attribute the parser saw is checked, so a repeated attribute name
     (`name="password" name="normal"`) cannot hide the first one. The
     HTML parser lowercases element and attribute names, so `licenseKey`
     arrives as `licensekey`; HTML names go through
     `is_secret_param_name_lowercased`, which asks whether any casing of
     the name is a secret name. It matches a few more names than the
     exact rule (`passwordless`), which only replaces more.
   - HTML `<script>` blocks: JSON content is checked like a JSON file.
     Any other script content becomes the placeholder; the helper cannot
     parse JavaScript, so it does not return it. `<style>` content is
     free text.
   - An HTML `<pre>` or `<code>` block that holds text alone is an
     example. When its text is not JSON but a YAML mapping or list
     (`sessionId: ...`, a YAML request body), it is checked like a YAML
     file and written back from the parsed copy: a comment or a repeated
     key is not in the copy, so a secret the parser dropped does not come
     back either, and the dropped value is counted. YAML is tried before
     the JSON documents in the text, so a YAML example with a flow
     mapping in it (`body: {"sessionId": "..."}`) is checked whole. The
     copy comes from the safe dumper, so the text can change its look
     (`roles: [reader]` comes back as a block list; an unquoted `12:30`
     is a number in YAML 1.1). Any other text in such a block (a request
     line and its JSON body, a shell command, prose) is checked like any
     text value: every JSON document in it is checked, the rest goes
     through the general rules. A `<span>` is ordinary page text and is
     not an example in YAML.
   - An HTML `<pre>` or `<code>` block with child elements (a page
     colours its example with `<span>`) fails closed. Two checks run:
     every child element gets the check it would get on its own (a
     secret-marked child, a secret-named attribute, a `<script>` that is
     not JSON), and the text joined over the children, as the page wrote
     it, must be one JSON or YAML object or list, which is then checked
     like a file. The block comes back as it was only when neither check
     would replace anything. Otherwise the whole block is the placeholder,
     counted once; it is never rebuilt from the two checks, because a
     child element can split a secret name or stand inside a key, so the
     text with the children replaced is another document than the one
     the page shows. Because the block comes back as the page wrote it,
     the parsed copy must hold all of its text: text that names a key
     twice in one object, at any depth (the parser keeps one value and
     drops the other), or YAML text with a comment, is no document, and
     the block is withheld. The same holds for a text value inside the
     document that is itself JSON with a key named twice: the value
     check withholds and counts it (next point), so the block is
     withheld too. A `<span>` with child elements is ordinary page text
     and gets the element check.
   - Every JSON object in a text value (or list that holds one) is
     parsed and checked the same way, and written back from the parsed
     copy, also when nothing was replaced. This holds for every text
     value in every format: a sample request in a description, a `<pre>`
     block, prose in a `<p>`, an attribute value. A request line before
     the body (`"example": "POST /sessions\n{...}"`) does not hide the
     body: each `{` or `[` in the text is tried as the start of a
     document, so a document inside other text is found. The text around
     the documents goes through the general rules. A text value whose
     JSON names a key twice in one object, at any depth, is withheld
     whole and counted once: the parser would keep one value and drop
     the other, so neither the parsed copy nor the count would say
     anything about the dropped one. The rule holds at every depth of
     nesting, also for JSON text inside a text value inside JSON text.
     So is a text value the parser cannot finish within a bounded amount
     of work (brackets that open over and over and never close).
   - Any text value that holds a private key in PEM form
     (`-----BEGIN ... PRIVATE KEY-----`), under any name.
4. Write the copy in the same format from the parsed file. JSON is
   compact (no spaces), close to the minified form appliances serve.
   YAML comes from the safe dumper, keys in order. XML keeps every
   namespace declaration on the element that declared it, with the
   prefixes as declared, because WSDL and XSD files use them inside
   attribute values (`type="xsd:string"`) and a child can bind a prefix
   to another namespace; comments, the XML declaration and CDATA markers
   do not come back. HTML comes back without comments.
5. Count every placeholder written.

## What the reads do with it

Each read runs the helper on the whole file before it cuts the text into
parts, so a part boundary cannot hide a secret. The answer carries
`secrets_replaced`; `size` and `sha256` describe the checked copy, and
joining the parts gives the copy. A redirect path goes through
`scrub_query_string`, and its count is added.

The size limit stays as before: a file too big for one stored result is
refused with its size, before parsing. The last check stays too: the
read stops when MEHO's general redaction rules (`redaction/patterns.py`)
would still change the copy, for example an `Authorization` header line
in free text. A read that gives those rules as `text_rule` has already
replaced such text, value by value, so this check passes. The `api_key` rule skips a value that already is a
complete `[REDACTED:...]` placeholder, so a replaced value does not set
it off.

## What the reader loses

- The parts give the checked copy, not the bytes the appliance sent:
  the same content, without comments, with secret values replaced.
- A value under a secret name is replaced even when it is a schema or a
  path item. In XML and HTML, an element that a `name`, `id`, `key` or
  `type` attribute marks as secret keeps only that label; its other
  attribute values go too, also a schema `type`.
- Inline JavaScript does not come back. JavaScript, CSS and plain text
  files are refused.
- Free text that is not JSON, such as "the password is abc", cannot be
  parsed; MEHO's general redaction rules check it, as before. A YAML
  example is parsed only in a `<pre>` or `<code>` block that holds text
  alone; `sessionId: ...` in a `<p>` or a `<span>` is free text.
- A YAML example in a `<pre>` or `<code>` block comes back from the safe
  dumper, like a YAML file: comments gone, keys in order, flow lists as
  block lists, the YAML 1.1 reading of an unquoted value (`yes` is
  `true`).

## Dependencies

`json`, `html.parser` and `urllib.parse` from the standard library;
PyYAML (`yaml`, the C loader and dumper when libyaml is present);
`defusedxml`; `is_secret_param_name` from `broadcast/events.py`.

## Known issues

- A YAML file with several documents (`---`) does not parse as one
  document and is refused. OpenAPI files are single documents.
- The HTML parser does not close elements the way a browser does
  (`<p>` and `<li>` are not closed by the next one). The written copy
  keeps everything in order, so this changes nesting only.

## References

- Tests: `backend/tests/test_connectors_shared_api_file_secrets.py` (one
  test per trick from the code reviews), and the tests of the three reads.
- The reads: `connectors-vcf-fleet.md`, `connectors-hcx.md`,
  `connectors-vmware-rest.md` (the SSO Admin read), `connectors-srm.md`,
  `connectors-vr.md`.
- The general redaction rules: `redaction.md`.
