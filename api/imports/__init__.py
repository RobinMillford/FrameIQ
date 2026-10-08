"""Data Portability Import Center (Task F6).

Public surface:

    from api.imports import preview, apply_import, ImportRejected, SOURCES

Layering, strictly one direction:

    uploads  (untrusted bytes  -> validated payload)
    sources  (payload          -> normalized records; no DB, no resolution)
    resolve  (record           -> local MediaItem; no writes)
    writer   (record           -> canonical FrameIQ write paths)
    service  (orchestration, preview vs apply, transaction boundary)

Every layer below ``service`` is independently testable and none of them can
write a row that the canonical write path would have refused.
"""
from api.imports.resolve import MediaIndex, resolve_episode, resolve_movie
from api.imports.service import (ALREADY_PRESENT, AMBIGUOUS, IMPORTED,
                                 INELIGIBLE, INVALID, UNRESOLVED,
                                 UNSUPPORTED, ImportRejected, apply_import,
                                 preview, summary_payload)
from api.imports.sources import SOURCES

__all__ = [
    'ALREADY_PRESENT', 'AMBIGUOUS', 'IMPORTED', 'INELIGIBLE', 'INVALID',
    'ImportRejected', 'MediaIndex', 'SOURCES', 'UNRESOLVED', 'UNSUPPORTED',
    'apply_import', 'preview', 'resolve_episode', 'resolve_movie',
    'summary_payload',
]