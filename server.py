from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import os
import sys
import psutil
from contextlib import asynccontextmanager

# Add current directory to path so we can import ImportBaseGuias
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# Add parent directory for backend imports
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
# Fix for noconsole mode where stdout/stderr are None
class FileLogStream:
    def __init__(self, filename):
        self.filename = filename
        try: self.log_file = open(filename, "a", encoding="utf-8")
        except: self.log_file = None
    def write(self, data):
        try:
            if self.log_file:
                    self.log_file.write(data)
                    self.log_file.flush()
        except: pass
    def flush(self):
        try: 
            if self.log_file: self.log_file.flush()
        except: pass
    def isatty(self): return False

# Logs will be initialized later when port is known to avoid file conflicts
# if sys.stdout is None: sys.stdout = FileLogStream("server_debug.log")
# if sys.stderr is None: sys.stderr = FileLogStream("server_err.log")

from ImportBaseGuias import UnimedScraper
from clmf_scraper import CLMFScraper

import threading
import time
from datetime import datetime, timedelta

app = FastAPI()
unimed_scraper = None
clmf_scraper = None
last_activity_time = datetime.now()
driver_lock = threading.Lock()
INACTIVITY_LIMIT = timedelta(minutes=20)

def kill_orphan_chrome_processes():
    """Kill chrome/chromedriver processes spawned by automation (not the user's browser)."""
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = (proc.info.get('name') or '').lower()
                if 'chromedriver' in name:
                    proc.kill()
                elif 'chrome' in name:
                    # Only kill automation chrome instances (spawned by webdriver)
                    cmdline = ' '.join(proc.info.get('cmdline') or [])
                    if '--test-type=webdriver' in cmdline:
                        proc.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
    except: pass


def maintain_driver_lifecycle():
    global unimed_scraper, clmf_scraper, last_activity_time
    while True:
        time.sleep(60) # Check every minute
        with driver_lock:
            # Cleanup both scrapers if inactive
            if datetime.now() - last_activity_time > INACTIVITY_LIMIT:
                killed_any = False
                if unimed_scraper and unimed_scraper.driver:
                    print(">>> Inactivity limit reached. Closing Unimed driver.")
                    try:
                        unimed_scraper.close_driver()
                        unimed_scraper.driver = None
                        killed_any = True
                    except Exception as e:
                        print(f"Error closing Unimed driver: {e}")
                
                if clmf_scraper and clmf_scraper.driver:
                    print(">>> Inactivity limit reached. Closing CLMF driver.")
                    try:
                        clmf_scraper.close_driver()
                        clmf_scraper.driver = None
                        killed_any = True
                    except Exception as e:
                        print(f"Error closing CLMF driver: {e}")
                
                if killed_any:
                    kill_orphan_chrome_processes()

# Start background thread
threading.Thread(target=maintain_driver_lifecycle, daemon=True).start()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global unimed_scraper, clmf_scraper, last_activity_time

    # --- Inicializar Unimed (comportamento legado modificado para lazy) ---
    unimed_scraper = UnimedScraper()
    # Driver e login agora só são iniciados no primeiro job que solicitar, 
    # igual ao CLMF, para evitar abrir janelas desnecessárias no startup.

    # --- Inicializar CLMF ---
    clmf_login = os.getenv("CLMF_LOGIN", "")
    clmf_password = os.getenv("CLMF_PASSWORD", "")
    clmf_headless = os.getenv("SGUCARD_HEADLESS", "true").lower() == "true"
    clmf_scraper = CLMFScraper(
        login=clmf_login,
        senha=clmf_password,
        headless=clmf_headless,
    )
    # Login CLMF é feito sob demanda (lazy) no primeiro job,
    # para não bloquear a inicialização se as credenciais não estiverem configuradas.

    last_activity_time = datetime.now()
    yield

    # --- Cleanup ---
    with driver_lock:
        if unimed_scraper:
            unimed_scraper.close_driver()
    if clmf_scraper and clmf_scraper.driver:
        clmf_scraper.close_driver()

app = FastAPI(lifespan=lifespan)

class JobRequest(BaseModel):
    job_id: int
    carteirinha_id: int
    carteirinha: str
    paciente: str = ""
    rotina: str = ""    # identifica qual scraper usar; vazio = legado Unimed
    params: dict = {}   # parâmetros arbitrários (CLMF e futuros convênios)


@app.get("/")
async def health_check():
    is_busy = driver_lock.locked()
    unimed_alive = (unimed_scraper is not None and unimed_scraper.driver is not None)
    clmf_alive = (clmf_scraper is not None and clmf_scraper.driver is not None)
    return {"status": "ok", "busy": is_busy, "unimed_alive": unimed_alive, "clmf_alive": clmf_alive}

@app.post("/restart")
def restart_driver():
    global unimed_scraper, clmf_scraper
    print(">>> Received manual restart request. Closing drivers...")
    with driver_lock:
        try:
            if unimed_scraper:
                unimed_scraper.close_driver()
            if clmf_scraper:
                clmf_scraper.close_driver()
        except: pass
        
        # Kill orphan chrome/chromedriver processes for a clean slate
        kill_orphan_chrome_processes()
        time.sleep(1)  # Wait for processes to fully terminate
        
        try:
            if unimed_scraper:
                unimed_scraper.start_driver()
                unimed_scraper.login()
            # CLMF is lazy loaded, no need to start here unless required
        except Exception as e:
            return {"status": "error", "message": f"Failed to restart: {e}"}
            
    return {"status": "success", "message": "Drivers restarted"}

@app.post("/process_job")
def process_job(job: JobRequest):
    print(f">>> Received Job {job.job_id} | rotina='{job.rotina}' | carteirinha={job.carteirinha}")
    global unimed_scraper, clmf_scraper, last_activity_time

    # ── Roteamento por rotina ──────────────────────────────────────────────
    if job.rotina == "clmf_atualizar_rc":
        return _process_job_clmf(job)
    else:
        return _process_job_unimed(job)


def _process_job_clmf(job: JobRequest):
    """Processa job do convênio CLMF via CLMFScraper."""
    global clmf_scraper, last_activity_time

    if not clmf_scraper:
        raise HTTPException(status_code=503, detail="CLMFScraper não inicializado")

    # Inicializar driver CLMF sob demanda (lazy)
    if not clmf_scraper.driver:
        print(">>> CLMFScraper: driver não inicializado. Iniciando...")
        try:
            clmf_scraper.start_driver()
        except Exception as e:
            return {"status": "error", "message": f"Falha ao iniciar driver CLMF: {e}",
                    "carteirinha_id": job.carteirinha_id}

    last_activity_time = datetime.now()

    try:
        result = clmf_scraper.atualizar_rc(job.params)
        last_activity_time = datetime.now()
        return {
            "status": result.get("status", "error"),
            "data": result,
            "carteirinha_id": job.carteirinha_id,
        }
    except Exception as e:
        from database import SessionLocal
        from models import Log
        _log_error(job.job_id, job.carteirinha_id, f"CLMF Server Crash: {e}")
        # Reset driver em falha para não poluir próximo job
        try:
            clmf_scraper.close_driver()
        except Exception:
            pass
        return {"status": "error", "message": str(e), "carteirinha_id": job.carteirinha_id}


def _process_job_unimed(job: JobRequest):
    """Processa job do convênio Unimed via UnimedScraper (legado)."""
    global unimed_scraper, last_activity_time

    if not unimed_scraper:
        raise HTTPException(status_code=503, detail="Scraper não inicializado")

    with driver_lock:
        if not unimed_scraper.driver:
            print(">>> Driver Unimed fechado. Reiniciando...")
            try:
                unimed_scraper.start_driver()
                unimed_scraper.login()
            except Exception as e:
                return {"status": "error", "message": f"Failed to restart driver: {e}",
                        "carteirinha_id": job.carteirinha_id}

        last_activity_time = datetime.now()

    try:
        with driver_lock:
            if not unimed_scraper.driver:
                raise Exception("Driver morreu antes do scraping.")
            results = unimed_scraper.process_carteirinha(
                job.carteirinha,
                job_id=job.job_id,
                carteirinha_db_id=job.carteirinha_id,
            )
            last_activity_time = datetime.now()
            print(f">>> Retornando {len(results)} itens para Job {job.job_id}")

        return {"status": "success", "data": results, "carteirinha_id": job.carteirinha_id}
    except Exception as e:
        _log_error(job.job_id, job.carteirinha_id, f"Server Crash: {e}")
        print(f">>> ERRO no job Unimed: {e}. Resetando driver.")
        with driver_lock:
            try:
                if unimed_scraper:
                    unimed_scraper.close_driver()
                    unimed_scraper.driver = None
                kill_orphan_chrome_processes()
            except Exception:
                pass
        return {"status": "error", "message": str(e), "carteirinha_id": job.carteirinha_id}


def _log_error(job_id: int, carteirinha_id: int, message: str):
    """Grava log de erro no banco de forma segura."""
    from database import SessionLocal
    from models import Log
    db = SessionLocal()
    try:
        db.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="ERROR", message=message))
        db.commit()
    except Exception as log_e:
        print(f"Falha ao gravar log de erro: {log_e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()



class QueueLogger:
    def __init__(self, queue, prefix="Worker"):
        self.queue = queue
        self.prefix = prefix
    def write(self, message):
        if message.strip():
            self.queue.put(f"[{self.prefix}] {message.strip()}")
    def flush(self):
        pass
    def isatty(self):
        return False

def run_server(port=8000, log_queue=None):
    if log_queue:
        sys.stdout = QueueLogger(log_queue, f"Worker-{port}")
        sys.stderr = QueueLogger(log_queue, f"Worker-{port} ERR")
    else:
        # Port-specific log files for debugging
        sys.stdout = FileLogStream(f"server_{port}_debug.log")
        sys.stderr = FileLogStream(f"server_{port}_err.log")
        
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Port will be passed via arg or env, default 8000
    port = int(os.environ.get("PORT", 8010))
    run_server(port)
