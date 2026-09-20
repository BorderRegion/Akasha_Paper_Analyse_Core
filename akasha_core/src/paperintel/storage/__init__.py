"""Local content-addressed object store (frozen namespace: storage).

Object key layout (spec doc 01 §6.2)::

    data/objects/<kind>/sha256/<first2>/<full_sha256>
"""

from paperintel.storage.object_store import (
    RETENTION_BY_KIND,
    LocalObjectStore,
    parse_storage_key,
    sha256_bytes,
    sha256_file,
    storage_key_for,
)

__all__ = [
    "RETENTION_BY_KIND",
    "LocalObjectStore",
    "parse_storage_key",
    "sha256_bytes",
    "sha256_file",
    "storage_key_for",
]
