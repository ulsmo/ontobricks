"""Triple store backend abstraction."""

from back.core.triplestore.TripleStoreBackend import TripleStoreBackend  # noqa: F401
from back.core.triplestore.delta import DeltaTripleStore  # noqa: F401
from back.core.triplestore.TripleStoreFactory import TripleStoreFactory  # noqa: F401
from back.core.triplestore.constants import RDF_TYPE, RDFS_LABEL  # noqa: F401

get_triplestore = TripleStoreFactory.get_triplestore
GRAPHDB_AVAILABLE = TripleStoreFactory.GRAPHDB_AVAILABLE

__all__ = [
    "TripleStoreBackend",
    "DeltaTripleStore",
    "TripleStoreFactory",
    "get_triplestore",
    "GRAPHDB_AVAILABLE",
    "RDF_TYPE",
    "RDFS_LABEL",
]
