"""Graph database backend abstraction.

Provides a pluggable graph DB layer separate from the triple store
(Delta views in Unity Catalog).  The default (and currently only)
engine is Lakebase Postgres (``lakebase``).  Future engines can be
added under ``back/core/graphdb/<engine>/`` — see ``_starter_kit/``
for a copy-paste template.
"""

from back.core.graphdb.GraphDBBackend import GraphDBBackend  # noqa: F401
from back.core.graphdb.GraphDBFactory import GraphDBFactory  # noqa: F401

get_graphdb = GraphDBFactory.get_graphdb
GRAPHDB_AVAILABLE = GraphDBFactory.LAKEBASE_AVAILABLE

__all__ = [
    "GraphDBBackend",
    "GraphDBFactory",
    "GRAPHDB_AVAILABLE",
    "get_graphdb",
]
