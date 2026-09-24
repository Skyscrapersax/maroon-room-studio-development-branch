"""Explicit PostgreSQL migration and one-time import from a verified SQLite source."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from app import create_app
from database import connect, transaction

TABLES = ("bookings", "requests", "limits", "booking_history")


def import_sqlite(source_path, destination):
    if not str(destination).startswith(("postgres://", "postgresql://")):
        raise ValueError("Import destination must be PostgreSQL.")
    source_uri = Path(source_path).resolve().as_uri() + "?mode=ro"
    counts = {}
    with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(connect(destination)) as target:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or source.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Source database failed integrity checks.")
        source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with transaction(target):
            existing = {row[0] for row in target.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()").fetchall()}
            for table in TABLES:
                if table in existing and target.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]:
                    raise ValueError("Import requires empty destination tables; no records changed.")
            for table in TABLES:
                if table not in source_tables:
                    continue
                if table not in existing:
                    raise ValueError("Source belongs to a different app; destination schema is missing "+table)
                columns = {row["name"] for row in target.execute("PRAGMA table_info("+table+")").fetchall()}
                counts[table] = 0
                for record in source.execute("SELECT * FROM "+table):
                    values = {key: record[key] for key in record.keys() if key in columns}
                    if table == "bookings":
                        values.setdefault("sequence", 1 if values["cancelled"] is not None else 0)
                        values.setdefault("updated", values["cancelled"] or values["created"])
                    names = list(values)
                    target.execute("INSERT INTO "+table+"("+",".join(names)+") VALUES("+",".join("?" for _ in names)+")", tuple(values.values()))
                    counts[table] += 1
                if "id" in columns:
                    target.execute("SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE((SELECT MAX(id) FROM "+table+"),0)+1, false)", (table,))
                if target.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] != counts[table]:
                    raise ValueError("Imported record count mismatch.")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-db", "import-sqlite"))
    parser.add_argument("source", nargs="?")
    args = parser.parse_args()
    if args.command == "import-sqlite" and not args.source:
        parser.error("import-sqlite requires a SQLite backup path")
    app = create_app({"HOSTED": True, "AUTO_MIGRATE": True})
    if args.command == "import-sqlite":
        counts = import_sqlite(args.source, app.config["DATABASE"])
        print("Import verified: " + ", ".join(f"{table}={count}" for table, count in counts.items()))
    else:
        print("PostgreSQL schema ready")


if __name__ == "__main__":
    main()
