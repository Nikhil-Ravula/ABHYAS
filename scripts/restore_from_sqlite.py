"""
Restore original ABHYAS SQLite database into Nidhi PostgreSQL.
Uses raw SQL INSERTs to bypass ORM model validation (auto-now, FKs, sequences).

Usage:
    docker cp /tmp/abhyas_db_dump abhyas_api:/tmp/abhyas_db_dump
    docker exec abhyas_api bash -c 'source /app/.nidhi_env.sh && python scripts/restore_from_sqlite.py'
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pyqproject.settings')

import django
django.setup()

from django.db import connection

# Tables in FK-safe dependency order (parents before children)
TABLE_ORDER = [
    'django_content_type',
    'auth_permission',
    'auth_group',
    'auth_group_permissions',
    'auth_user',
    'auth_user_groups',
    'auth_user_user_permissions',
    'django_migrations',
    'django_session',
    'django_admin_log',
    'pyqapp_sitevisit',
    'pyqapp_announcement',
    'pyqapp_ticket',
    'pyqapp_ticketreply',
    'pyqapp_paper',
    'pyqapp_paperview',
    'pyqapp_paperdownload',
    'pyqapp_importantquestionentry',
    'pyqapp_iqview',
    'pyqapp_iqdownload',
    'pyqapp_usersession',
    'pyqapp_activitylog',
]


def get_pg_column_info(cursor, table_name):
    """Get ordered column info for a PostgreSQL table."""
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, [table_name])
    return {row[0]: row[1] for row in cursor.fetchall()}


def main():
    sqlite_path = '/tmp/abhyas_db_dump'
    if not os.path.exists(sqlite_path):
        print(f"ERROR: {sqlite_path} not found. Copy it into the container first.")
        sys.exit(1)

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cur = sqlite_conn.cursor()

    # List tables available in SQLite
    sqlite_cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    sqlite_tables = {r['name'] for r in sqlite_cur.fetchall()}

    # Skip django_migrations — fresh migrations already ran
    skip_tables = {'django_migrations'}
    available = [t for t in TABLE_ORDER if t in sqlite_tables and t not in skip_tables]
    print(f"Tables to restore ({len(available)}): {available}")
    print()

    with connection.cursor() as cursor:
        # Delete all rows from target tables (reverse order for FK safety)
        for table_name in reversed(available):
            try:
                cursor.execute(f'DELETE FROM "{table_name}"')
                print(f"  Cleared {table_name}")
            except Exception as e:
                print(f"  Could not clear {table_name}: {e}")
        print()

        total = 0
        for table_name in available:
            # Get SQLite columns and rows
            sqlite_cur.execute(f'SELECT * FROM "{table_name}"')
            rows = sqlite_cur.fetchall()
            if not rows:
                print(f"  {table_name}: 0 rows (empty)")
                continue

            sqlite_cur.execute(f'PRAGMA table_info("{table_name}")')
            sqlite_cols = [r[1] for r in sqlite_cur.fetchall()]

            # Get PG columns with types
            pg_cols = get_pg_column_info(cursor, table_name)

            # Build column mapping (positional match, assuming same order)
            common_cols = [c for c in sqlite_cols if c in pg_cols]
            if not common_cols:
                print(f"  {table_name}: SKIP (no matching columns)")
                continue

            # Identify boolean columns in PG
            bool_cols = {c for c in common_cols if pg_cols.get(c) == 'boolean'}

            col_list = ', '.join(f'"{c}"' for c in common_cols)
            placeholders = ', '.join(['%s'] * len(common_cols))
            insert_sql = f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})'

            count = 0
            for row in rows:
                row_dict = dict(row)
                values = []
                for c in common_cols:
                    v = row_dict.get(c)
                    if c in bool_cols and v is not None:
                        v = bool(v)
                    values.append(v)
                try:
                    cursor.execute(insert_sql, values)
                    count += 1
                except Exception as e:
                    print(f"    ERROR pk={row_dict.get('id', '?')}: {e}")

            # Reset sequence
            try:
                cursor.execute(f"""
                    SELECT setval('{table_name}_id_seq',
                        GREATEST(COALESCE((SELECT MAX(id) FROM "{table_name}"), 0), 1))
                """)
            except Exception:
                pass

            print(f"  {table_name}: {count}/{len(rows)} rows inserted")
            total += count

    print()
    print(f"Done: {total} total rows restored")
    print()
    print("Run Cloudinary->MinIO migration next:")
    print("  docker exec abhyas_api bash -c 'source /app/.nidhi_env.sh && python scripts/migrate_from_cloudinary.py'")


if __name__ == '__main__':
    main()
