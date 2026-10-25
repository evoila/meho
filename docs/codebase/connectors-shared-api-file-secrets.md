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
New API file reads use it from the start; `srm.api_file.get` and
`vr.api_file.get` do. Those reads also give the helper MEHO's general redaction rules as a text rule
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

The two sibling modules hold the parts: `api_file_values.py` has the
`Scrubber` walk over parsed values and the placeholder;
`api_file_markup.py` has the XML and HTML parsers and writers.

## What counts as a secret name

MEHO's one rule for secret-named fields,
`is_secret_param_name` in `broadcast/events.py`: `password`, `passwd`,
`pwd`, `passphrase`, `apikey`, `kubeconfig` anywhere in the name;
`secret`, `secrets`, `token`, `credential`, `credentials` at the end of
the name; and the pairs `api_key`, `access_key`, `private_key`,
`encryption_key`, `license_key`, `licence_key`, `secret_id`,
`secret_value`, `secret_data`. Names are read in snake, camel and kebab
case. #4780 added the licence key.

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
   - Any text value that is itself a JSON object or list (a sample
     request in a description, a `<pre>` block) is parsed and checked
     the same way, and always written back from the parsed copy, also
     when nothing was replaced: the original text can hold a repeated
     key the parser dropped, and its value with it.
   - HTML `<pre>` and `<code>` blocks whose text is split over child
     elements (a page that colours a JSON example with `<span>`): the
     text of the whole block is joined. When it is a JSON object or list,
     it is checked like a JSON file and comes back as plain JSON, without
     the child elements. A child the block marks as secret (`<span
     name="password">`) is replaced before the text is joined, so its
     value cannot hide inside a JSON value with a harmless name. The
     placeholder is written so the joined text stays JSON: as a JSON
     string when the child held a whole value (a number, a quoted
     string), bare when it held part of one. When no placeholder keeps
     the text JSON (the child held a key and its value), or when the
     child held a key or part of one (the replaced key would hide the
     value after it from the check by key), the block cannot be
     checked, and its whole content becomes the placeholder.
     Any other block keeps its child elements and is checked as before.
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
  parsed; MEHO's general redaction rules check it, as before.

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
