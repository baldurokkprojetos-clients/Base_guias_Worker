import sys
import os
import pandas as pd
from backend.database import get_db, SessionLocal, engine
from backend.models import Carteirinha
from sqlalchemy import text

# Add current dir to path
sys.path.append(os.getcwd())

def test_file_read():
    print("Testing file read...")
    try:
        df = pd.read_excel('carteirinhas.xlsx')
        print(f"Columns found: {df.columns.tolist()}")
        if 'Carteirinha' not in df.columns:
            print("ERROR: 'Carteirinha' column missing")
        else:
            print("File read validation passed.")
    except Exception as e:
        print(f"File read FAILED: {e}")

def test_db_connection():
    print("\nTesting DB connection...")
    try:
        db = SessionLocal()
        # Try simple query
        result = db.execute(text("SELECT 1"))
        print(f"DB Connection successful: {result.fetchone()}")
        
        # Check if table exists
        try:
             count = db.query(Carteirinha).count()
             print(f"Carteirinha table row count: {count}")
        except Exception as e:
             print(f"Querying Carteirinha table FAILED: {e}")
             
        db.close()
    except Exception as e:
        print(f"DB Connection FAILED: {e}")

if __name__ == "__main__":
    test_file_read()
    test_db_connection()
