-- Reference DDL for one OntoBricks flat triple table on Lakebase Postgres.
-- Replace <schema> and <table> with validated identifiers (see LakebaseFlatStore).

CREATE SCHEMA IF NOT EXISTS "<schema>";

CREATE TABLE IF NOT EXISTS "<schema>"."<table>" (
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    datatype    TEXT,
    lang        TEXT,
    PRIMARY KEY (subject, predicate, object)
);

CREATE INDEX IF NOT EXISTS ix_<table>_sp  ON "<schema>"."<table>" (subject, predicate);
CREATE INDEX IF NOT EXISTS ix_<table>_po  ON "<schema>"."<table>" (predicate, object);
CREATE INDEX IF NOT EXISTS ix_<table>_ops ON "<schema>"."<table>" (object, predicate);
