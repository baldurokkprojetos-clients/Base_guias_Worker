
import sys
import os
from sqlalchemy import text

# Add parent dir to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from database import engine, Base
from models import Worker

def migrate():
    print("Checking/Creating workers table...")
    try:
        # Create all tables that don't exist
        # correct way to import Base and models so they are registered
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
