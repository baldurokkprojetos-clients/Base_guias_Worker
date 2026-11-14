from backend.database import SessionLocal
from backend.models import Log

db = SessionLocal()
logs = db.query(Log).order_by(Log.created_at.desc()).limit(20).all()
print("Recent Logs:")
for log in logs:
    print(f"[{log.level}] {log.message} (Job: {log.job_id})")
