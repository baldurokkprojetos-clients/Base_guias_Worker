
import sys
import os
from sqlalchemy import text

# Add backend path manually
BACKEND_PATH = r"c:\dev\Clone_agenda_hub_basic\backend"
sys.path.append(BACKEND_PATH)

try:
    from database import engine, Base
    from models import Worker
except ImportError as e:
    print(f"Failed to import from backend: {e}")
    sys.exit(1)

def migrate():
    print("Checking/Creating workers table...")
    try:
        # Create all tables that don't exist
        Base.metadata.create_all(bind=engine)
        print("Tables created (if not existed).")
        
        # Verify if table exists and has columns
        with engine.connect() as conn:
            result = conn.execute(text("SELECT * FROM workers LIMIT 1;"))
            print("Workers table accessible.")
    except Exception as e:
        print(f"Error creating/checking table: {e}")

if __name__ == "__main__":
    migrate()
