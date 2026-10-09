"""Data Portability Import Center (Tasks F6, F7).

Public surface:

    from api.imports import (preview, apply_import, summary_payload,
                             search_local, list_mappings, save_mapping,
                             delete_mapping, ImportRejected, SOURCES)

Layering, strictly one direction:

    uploads    (untrusted bytes  -> validated payload)
    sources    (payload          -> normalized records; no DB, no resolution)
    mappings   (the user's saved answers; no inference, no candidate guessing)
    resolve    (record           -> local MediaItem; no writes, no network)
    writer     (record           -> canonical FrameIQ write paths)
    service    (orchestration, preview vs apply, transaction boundary)

Every layer below ``service`` is independently testable and none of them can
write a row that the canonical write path would have refused.

Order of trust when resolving a title (Task F7):

    saved mapping  >  explicit user selection  >  source external id
                  >  unique title match        >  candidates  >  unresolved

A mapping and a selection both outrank inference: the user chose those titles
by comparing them, which is information no heuristic has.
"""
from api.imports.mappings import (MappingError, MappingSet, delete_mapping,
                                  encode_key, list_mappings, load_mapping_set,
                                  resolution_key, save_mapping)
from api.imports.resolve import (MediaIndex, candidates_for, media_payload,
                                 resolve_episode, resolve_movie, search_local)
from api.imports.service import (ALREADY_PRESENT, AMBIGUOUS, CONFLICT,
                                 IMPORTED, INELIGIBLE, INVALID, UNRESOLVED,
                                 UNSUPPORTED, ImportRejected, SelectionRejected,
                                 apply_import, preview, summary_payload)
from api.imports.sources import SOURCES
from api.imports.writer import write_movie_review

__all__ = [
    'ALREADY_PRESENT', 'AMBIGUOUS', 'CONFLICT', 'IMPORTED', 'INELIGIBLE',
    'INVALID', 'ImportRejected', 'MappingError', 'MappingSet', 'MediaIndex',
    'SOURCES', 'SelectionRejected', 'UNRESOLVED', 'UNSUPPORTED',
    'apply_import', 'candidates_for', 'delete_mapping', 'encode_key',
    'list_mappings', 'load_mapping_set', 'media_payload', 'preview',
    'resolution_key', 'resolve_episode', 'resolve_movie', 'save_mapping',
    'search_local', 'summary_payload', 'write_movie_review',
]