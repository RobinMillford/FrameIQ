"""Extract inline scripts from Jinja templates for Node syntax checking (F21).

WHY THIS EXISTS
---------------
Three templates shipped inline ``<script>`` blocks that do not parse:

* ``user_profile.html`` — an unterminated template literal, which killed a
  193-line block including the followers/following modal renderer.
* ``genre.html`` / ``tv_genre.html`` — an orphaned ``});``.
* ``feed.html`` — nested ``<script><script>`` tags, which browsers treat as
  text so neither body ever ran.

``node --check static/js/*.js`` cannot catch any of these: they live in
template inline scripts, which no lint job inspects. This module extracts
those blocks so a test can parse them.

DESIGN RULE — DO NOT SILENTLY SKIP
----------------------------------
An earlier version of this extractor tried to parse *every* non-``src`` block
and reported false failures for ``<script type="application/json">`` data
islands. The tempting "fix" is to swallow unparseable blocks, which would make
the check permanently green and worthless.

So the classification is explicit and an unrecognised ``type`` is an ERROR, not
a skip. Only genuinely non-executable blocks are excluded, and each exclusion
carries a reason that the caller can assert on.

Jinja is removed, not "handled": comments and statements are deleted and
expressions become the literal ``null``. Stripping cannot invent unbalanced
braces from a well-formed template, so a parse failure after stripping means the
JavaScript is genuinely broken.
"""
import re
import subprocess
import tempfile

# MIME types a browser will actually execute as script. Everything else (data
# islands, JSON-LD) is a payload, not code, and is excluded BY TYPE — never by
# "it failed to parse".
_EXECUTABLE_TYPES = {
    '',
    'text/javascript',
    'application/javascript',
    'text/ecmascript',
    'application/ecmascript',
    'module',
    'text/babel',
}

_SCRIPT_OPEN_RE = re.compile(r'<script\b([^>]*)>', re.I)
_TYPE_RE = re.compile(r'\btype\s*=\s*["\']?([^"\'\s>]+)', re.I)
_SRC_RE = re.compile(r'\bsrc\s*=', re.I)


def strip_jinja(source):
    """Remove Jinja so a template's JavaScript can be parsed on its own."""
    source = re.sub(r'\{#.*?#\}', '', source, flags=re.S)   # comments
    source = re.sub(r'\{%.*?%\}', '', source, flags=re.S)   # statements
    source = re.sub(r'\{\{.*?\}\}', 'null', source, flags=re.S)  # expressions
    return source


class UnknownScriptType(Exception):
    """A <script> carried a type this module has no opinion about.

    Raised instead of silently treating the block as JS or skipping it, so a
    future template using an unexpected type fails the check loudly rather than
    quietly falling out of coverage.
    """


def iter_script_blocks(source):
    """Yield ``(attrs, body, executable)`` for each <script> in *source*.

    ``executable`` is True only for blocks a browser runs as code.

    Scanning is sequential rather than regex-based. A regex over the whole
    document would also match a literal ``<script`` sitting inside a JavaScript
    comment (``// the enclosing <script> block…``) and treat it as a new block.
    Per the HTML parsing rules, a ``<script>`` element ends at the FIRST
    ``</script>``, so the body is consumed whole and scanning resumes after it.
    """
    pos = 0
    lowered = source.lower()
    while True:
        match = _SCRIPT_OPEN_RE.search(source, pos)
        if not match:
            return
        attrs = match.group(1)
        close = lowered.find('</script>', match.end())
        if close == -1:
            # Unterminated <script> is itself a markup defect worth surfacing.
            raise ValueError('unterminated <script> tag: %r' % match.group(0)[:80])
        body = source[match.end():close]
        pos = close + len('</script>')

        if _SRC_RE.search(attrs):
            continue   # external file — covered by node --check on the file

        type_match = _TYPE_RE.search(attrs)
        script_type = (type_match.group(1).strip().lower() if type_match else '')
        if script_type in _EXECUTABLE_TYPES:
            yield attrs, body, True
        elif script_type in ('application/json', 'application/ld+json'):
            yield attrs, body, False   # data island / JSON-LD: payload, not code
        else:
            raise UnknownScriptType(
                'script type %r is not recognised; refusing to guess whether it '
                'is executable' % script_type)


def javascript_blocks(source):
    """Yield only the blocks a browser executes."""
    for _attrs, body, executable in iter_script_blocks(source):
        if executable and body.strip():
            yield body


def node_check(js_source):
    """Return (ok, error_text) for a JavaScript string."""
    handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8')
    try:
        handle.write(js_source)
        handle.close()
        proc = subprocess.run(['node', '--check', handle.name],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return True, ''
        lines = [ln for ln in proc.stderr.splitlines() if 'Error' in ln]
        return False, (lines[0] if lines else proc.stderr.strip())[:200]
    finally:
        import os
        os.unlink(handle.name)
