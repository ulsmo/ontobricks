``back.core.triplestore`` -- Pluggable RDF/Graph Backends
==========================================================

The triple store package covers Unity Catalog / Delta materialization and
related services. **Graph database** engines (Lakebase Postgres today,
plus the pluggable abstraction for future engines) live in
``back.core.graphdb``; see :doc:`app.core.graphdb`.

Constants
---------

.. automodule:: back.core.triplestore.constants
   :members:
   :undoc-members:
   :show-inheritance:

Abstract Base
-------------

.. automodule:: back.core.triplestore.TripleStoreBackend
   :members:
   :undoc-members:
   :show-inheritance:

Factory
-------

.. automodule:: back.core.triplestore.TripleStoreFactory
   :members:
   :undoc-members:
   :show-inheritance:

Delta Backend
~~~~~~~~~~~~~

.. automodule:: back.core.triplestore.delta.DeltaTripleStore
   :members:
   :undoc-members:
   :show-inheritance:
