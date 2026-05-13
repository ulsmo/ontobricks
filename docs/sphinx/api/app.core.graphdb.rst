``back.core.graphdb`` -- Pluggable Graph Database Backends
===========================================================

Package
-------

.. automodule:: back.core.graphdb
   :members:
   :undoc-members:
   :show-inheritance:
   :exclude-members: GraphDBBackend, GraphDBFactory

Abstract Base
-------------

.. automodule:: back.core.graphdb.GraphDBBackend
   :members:
   :undoc-members:
   :show-inheritance:

Factory
-------

.. automodule:: back.core.graphdb.GraphDBFactory
   :members:
   :undoc-members:
   :show-inheritance:

Lakebase (Postgres) subpackage
------------------------------

See :doc:`app.core.graphdb.lakebase` for ``back.core.graphdb.lakebase`` (flat triple
tables on the App-bound Lakebase Postgres instance).

Adding a new engine
-------------------

A copy-paste template for new engines lives at
``src/back/core/graphdb/_starter_kit/ExampleStore.py``. Implement the
:class:`back.core.graphdb.GraphDBBackend` contract, register the engine in
:class:`back.core.graphdb.GraphDBFactory`, and add it to
``ALLOWED_GRAPH_ENGINES`` in
:mod:`back.objects.session.global_config`. See ``docs/graphdb-integration.md``
for the full integration walkthrough.
