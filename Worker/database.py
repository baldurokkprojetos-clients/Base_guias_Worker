"""
Independent database connection for Worker
Connects directly to Supabase without depending on backend code
"""
import sys
import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    # 1. Try to load user-provided .env next to the executable
    env_path_exe = os.path.join(os.path.dirname(sys.executable), '.env')
    if os.path.exists(env_path_exe):
        load_dotenv(env_path_exe)
        
    # 2. Add bundled .env as fallback (if not overriden by above)
    env_path_meipass = os.path.join(sys._MEIPASS, '.env')
    if os.path.exists(env_path_meipass):
        load_dotenv(env_path_meipass)

# Load .env from Worker directory or parent directory (Development mode)
load_dotenv(os.path.join(os.getcwd(), '.env'))
try:
    load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
except: pass

DB_USER = os.getenv("SUPABASE_DB_USER", "postgres")
DB_PASSWORD = os.getenv("SUPABASE_PASSWORD", "")
DB_HOST = os.getenv("SUPABASE_DB_HOST", "")
DB_PORT = os.getenv("SUPABASE_DB_PORT", "5432")
DB_NAME = os.getenv("SUPABASE_DB_NAME", "postgres")

SQLALCHEMY_DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,        # testa conexão antes de reutilizar do pool
    pool_recycle=300,          # recicla conexões a cada 5 min (evita SSL stale)
    pool_size=3,               # worker local não precisa de pool grande
    max_overflow=5,
    pool_timeout=30,
    connect_args={
        "connect_timeout": 10,     # falha rápido se DB inacessível
        "keepalives": 1,           # habilita TCP keepalives
        "keepalives_idle": 30,     # keepalive após 30s idle
        "keepalives_interval": 10, # retransmite a cada 10s
        "keepalives_count": 5,     # abandona após 5 falhas consecutivas
    }
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
