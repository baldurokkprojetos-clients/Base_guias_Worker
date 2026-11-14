from backend.database import engine
from sqlalchemy import inspect

inspector = inspect(engine)
columns = inspector.get_columns('base_guias')
print("Columns in base_guias:")
for c in columns:
    print(f"- {c['name']} ({c['type']})")
