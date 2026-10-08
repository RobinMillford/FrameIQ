"""Upload safety for the import center (Task F6).

Every byte arriving here is untrusted. This module is the gate, and it runs
BEFORE any parsing so a hostile or enormous upload never reaches a CSV parser
or a JSON decoder.

Order matters:

  1. **size first** — a cheap byte-count check before we allocate anything.
  2. **extension** — the declared name is a hint, never proof.
  3. **container sniffing** — we look at the actual magic bytes, because
     ``.json`` renamed to ``.exe`` is trivially produced.
  4. **archive inspection** — members are validated individually and read in
     memory. Nothing is ever extracted to a filesystem path.

Deliberately absent: pickle, yaml.load (unsafe loader), dynamic import, shell
execution. A data-import feature has no legitimate need for any of them.
"""
import io
import json
import os
import zipfile
from typing import List, NamedTuple, Optional

# Bounded well below the app's 5 MB MAX_CONTENT_LENGTH so a ZIP that is small
# compressed but enormous uncompressed cannot expand into memory. 64 MB of
# uncompressed content is far beyond any realistic watch history and well
# within what a normal server can serve.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 64

# Per-source allow-list. An upload whose extension is not listed for the
# chosen source is rejected outright — never "best effort".
ALLOWED_EXTENSIONS = {
    'letterboxd': frozenset({'.zip'}),
    'tvtime': frozenset({'.json', '.zip'}),
}


class UploadRejected(ValueError):
    """Untrusted input refused. Carries a safe, user-facing reason."""

    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class SafeMember(NamedTuple):
    """A validated archive member, held in memory with a SAFE name only."""

    name: str
    data: bytes


def safe_member_name(raw):
    """Return a validated member name, or ``None`` if it is unsafe.

    Rejects, in order: empty/NUL-bearing names, absolute POSIX paths, Windows
    drive/UNC paths, any ``..`` segment, and backslashes (a Windows client can
    produce ``..\\`` which some extractors normalise). Extracting such a name
    is the standard zip-slip primitive, so we never write names to disk at all
    and additionally refuse to expose them.
    """
    if not raw or '\x00' in raw:
        return None
    name = raw.replace('\\', '/')
    if name.startswith('/') or os.path.isabs(name):
        return None
    # Windows drive letters and UNC paths.
    if len(name) > 1 and name[1] == ':':
        return None
    segments = name.split('/')
    if any(segment in ('..', '.') for segment in segments):
        return None
    if any(segment.startswith('.') and segment not in ('.', '..')
           for segment in segments):
        # Refuse dotfiles (.env, .git/config) inside an archive.
        return None
    return name


def check_size(payload, limit=MAX_UPLOAD_BYTES):
    """Reject oversize input before parsing it."""
    if payload is None:
        raise UploadRejected('No file was uploaded.')
    if not payload:
        raise UploadRejected('The uploaded file is empty.')
    if len(payload) > limit:
        raise UploadRejected(
            'That file is too large (limit %d MB).'
            % (limit // (1024 * 1024)), status=413)
    return payload


def check_extension(source, filename):
    """Extension allow-list for the chosen source."""
    allowed = ALLOWED_EXTENSIONS.get(source)
    if allowed is None:
        raise UploadRejected('Unknown import source.', status=404)
    ext = os.path.splitext(filename or '')[1].lower()
    if ext not in allowed:
        raise UploadRejected(
            'Unsupported file type for %s (allowed: %s).'
            % (source, ', '.join(sorted(allowed))))
    return ext


def looks_like_zip(payload):
    """Magic-byte sniff. Local file headers are ``PK\\x03\\x04``."""
    return payload[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')


def looks_like_json(payload):
    """A JSON document, allowing a UTF-8 BOM and leading whitespace."""
    head = payload.lstrip(codec_bom()).lstrip()[:1]
    return head in (b'{', b'[')


def codec_bom():
    return b'\xef\xbb\xbf'


def read_archive(payload):
    """Validate and read every member of a ZIP into memory.

    Returns ``[SafeMember]``. Never extracts to disk, so zip-slip is not
    reachable even in principle; the name validation additionally means a
    hostile name cannot be echoed back into a UI or a log.
    """
    if not looks_like_zip(payload):
        raise UploadRejected('That file is not a valid ZIP archive.')
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError):
        raise UploadRejected('That file could not be read as a ZIP archive.')

    with archive:
        try:
            infos = archive.infolist()
        except (zipfile.BadZipFile, OSError):
            raise UploadRejected('That archive could not be inspected.')

        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise UploadRejected(
                'That archive has too many entries (limit %d).'
                % MAX_ARCHIVE_MEMBERS)

        total = sum(info.file_size for info in infos)
        if total > MAX_UNCOMPRESSED_BYTES:
            # Zip-bomb guard: the compressed upload can be tiny while the
            # declared uncompressed size is enormous.
            raise UploadRejected(
                'That archive expands to too much data.', status=413)

        members = []
        for info in infos:
            if info.is_dir():
                continue
            name = safe_member_name(info.filename)
            if name is None:
                raise UploadRejected(
                    'That archive contains an unsafe file name.')
            if info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise UploadRejected(
                    'That archive expands to too much data.', status=413)
            try:
                data = archive.read(info)
            except (zipfile.BadZipFile, RuntimeError, OSError, EOFError):
                raise UploadRejected(
                    'That archive could not be read (%s).' % _safe_info(name))
            members.append(SafeMember(name=name, data=data))
        if not members:
            raise UploadRejected('That archive is empty.')
        return members


def _safe_info(name):
    """Only ever the basename, and only if it is itself safe."""
    clean = safe_member_name(name)
    return os.path.basename(clean) if clean else 'an entry'


def pick_json_member(members: List[SafeMember]):
    """Choose the JSON document inside an archive.

    Prefers a conventional export filename, then any ``.json``. Deterministic
    (sorted) so two runs pick the same member.
    """
    preferred = ('tvtime.json', 'export.json', 'data.json')
    for candidate in preferred:
        for member in members:
            if member.name.lower().endswith(candidate):
                return member
    candidates = sorted(m for m in members
                        if os.path.splitext(m.name)[1].lower() == '.json')
    if not candidates:
        raise UploadRejected('No JSON file was found in that archive.')
    return candidates[0]


def decode_json(data):
    """Decode untrusted JSON with a hard size bound already applied."""
    try:
        return json.loads(data.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError):
        raise UploadRejected('That file is not valid JSON.')


def load_payload(source, filename, payload) -> Optional[object]:
    """Full gate: size, extension, container sniff, then JSON load.

    Returns the decoded JSON document, or ``None`` for sources whose payload
    is a CSV archive that the adapter handles itself (Letterboxd).
    """
    check_size(payload)
    ext = check_extension(source, filename)

    if ext == '.zip':
        members = read_archive(payload)
        if source == 'tvtime' or looks_like_json(payload):
            # TV Time GDPR export: JSON, optionally wrapped in a ZIP.
            member = pick_json_member(members)
            return decode_json(member.data), members
        return None, members

    # Bare .json (TV Time only — Letterboxd has no bare JSON export).
    if not looks_like_json(payload):
        raise UploadRejected('That file is not valid JSON.')
    return decode_json(payload), None