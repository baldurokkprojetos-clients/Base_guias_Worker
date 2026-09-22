import sys
import os
from sqlalchemy import text

# Add parent dir to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from backend.database import engine

def migrate():
    print("Starting migration...")
    with engine.connect() as conn:
        try:
            # 1. Rename column
            print("Renaming column nome_terapia to qtde_solicitada...")
            # Check if column exists first to avoid error? Or just try/except
            # PostgreSQL syntax
            try:
                conn.execute(text("ALTER TABLE base_guias RENAME COLUMN nome_terapia TO qtde_solicitada;"))
                print("Column renamed.")
            except Exception as e:
                print(f"Rename failed (maybe already renamed?): {e}")

            # 2. Change type to Integer
            print("Changing type to INTEGER...")
            try:
                # USING clause needed if there is data
                conn.execute(text("ALTER TABLE base_guias ALTER COLUMN qtde_solicitada TYPE INTEGER USING (TRIM(qtde_solicitada)::integer);"))
                print("Type changed to INTEGER.")
            except Exception as e:
                # If it's already integer or empty
                try:
                     conn.execute(text("ALTER TABLE base_guias ALTER COLUMN qtde_solicitada TYPE INTEGER USING qtde_solicitada::integer;"))
                     print("Type changed to INTEGER (simple cast).")
                except Exception as e2:
                     print(f"Type change failed: {e2}")
            
            conn.commit()
            print("Migration committed.")
        except Exception as e:
            print(f"Migration error: {e}")

if __name__ == "__main__":
    migrate()
