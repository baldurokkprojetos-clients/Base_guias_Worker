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


def maintain_driver_lifecycle():
    global unimed_scraper, clmf_scraper, last_activity_time
    while True:
        time.sleep(60) # Check every minute
        with driver_lock:
            # Cleanup both scrapers if inactive
            # (close_driver já mata apenas os processos deste worker)
            if datetime.now() - last_activity_time > INACTIVITY_LIMIT:
                if unimed_scraper and unimed_scraper.driver:
                    print(">>> Inactivity limit reached. Closing Unimed driver.")
                    try:
                        unimed_scraper.close_driver()
                        unimed_scraper.driver = None
                    except Exception as e:
                        print(f"Error closing Unimed driver: {e}")

                if clmf_scraper and clmf_scraper.driver:
                    print(">>> Inactivity limit reached. Closing CLMF driver.")
                    try:
                        clmf_scraper.close_driver()
                        clmf_scraper.driver = None
                    except Exception as e:
                        print(f"Error closing CLMF driver: {e}")

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
        # Nota: NÃO matar chromedrivers globais aqui — cada worker só limpa
        # os próprios processos (close_driver já o faz). Kill global derrubaria
        # os drivers dos outros workers em pleno job.

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
    # Fail-fast: rotina desconhecida NUNCA cai no scraper Unimed (evita que um
    # worker desatualizado processe jobs de outra rotina pelo fluxo errado).
    if job.rotina == "clmf_atualizar_rc":
        return _process_job_clmf(job)
    elif job.rotina == "clmf_imprimir_evolucao":
        return _process_job_clmf_evolucao(job)
    elif not job.rotina:
        return _process_job_unimed(job)
    else:
        msg = f"Rotina desconhecida '{job.rotina}' — server desatualizado para esta rotina?"
        print(f">>> {msg}")
        return {"status": "error", "message": msg, "carteirinha_id": job.carteirinha_id}


def _ensure_driver(scraper, scraper_name: str, needs_login: bool = True) -> str | None:
    """Garante que o driver do scraper está vivo. Retorna None se ok, ou mensagem de erro."""
    if scraper.is_driver_alive():
        return None
    print(f">>> {scraper_name}: driver morto ou ausente. Reiniciando...")
    try:
        # close_driver já mata apenas os processos DESTE worker (kill por PID)
        scraper.close_driver()
        scraper.start_driver()
        if needs_login:
            scraper.login()
        return None
    except Exception as e:
        # Não vazar driver recém-criado se o login falhou
        try:
            scraper.close_driver()
        except Exception:
            pass
        return f"Falha ao reiniciar driver {scraper_name}: {e}"


def _process_job_clmf(job: JobRequest):
    """Processa job do convênio CLMF via CLMFScraper."""
    global clmf_scraper, last_activity_time

    if not clmf_scraper:
        raise HTTPException(status_code=503, detail="CLMFScraper não inicializado")

    # Job inteiro sob lock: evita corrida com o lifecycle de inatividade e com
    # outros jobs sobre o mesmo driver (mesmo padrão do Unimed). Lock não é
    # reentrante — o except abaixo NÃO deve readquirir driver_lock.
    with driver_lock:
        err = _ensure_driver(clmf_scraper, "CLMF", needs_login=True)
        if err:
            return {"status": "error", "message": err, "carteirinha_id": job.carteirinha_id}

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
            _log_error(job.job_id, job.carteirinha_id, f"CLMF Server Crash: {e}")
            # Reset driver em falha para não poluir próximo job
            try:
                clmf_scraper.close_driver()
            except Exception:
                pass
            return {"status": "error", "message": str(e), "carteirinha_id": job.carteirinha_id}


def _process_job_clmf_evolucao(job: JobRequest):
    """Processa job da OP2 ImprimirEvolucao (rotina clmf_imprimir_evolucao).

    Mesmo molde de _process_job_clmf: job inteiro sob driver_lock, driver
    garantido com login, reset em falha. A OP2 faz login sob demanda (lazy),
    persiste o resultado por item em evolucao_itens e pode processar jobs
    irmãos do mesmo paciente (afinidade) dentro do mesmo request.
    """
    global clmf_scraper, last_activity_time

    if not clmf_scraper:
        raise HTTPException(status_code=503, detail="CLMFScraper não inicializado")

    with driver_lock:
        err = _ensure_driver(clmf_scraper, "CLMF", needs_login=False)
        if err:
            return {"status": "error", "message": err, "carteirinha_id": job.carteirinha_id}

        last_activity_time = datetime.now()

        try:
            params = dict(job.params or {})
            params["job_id"] = job.job_id
            # URL deste server (afinidade marca locked_by nos jobs irmãos que reivindicar)
            params["server_url"] = f"http://127.0.0.1:{os.environ.get('PORT', '8010')}"

            result = clmf_scraper.imprimir_evolucao(params)
            last_activity_time = datetime.now()
            envelope = {
                "status": result.get("status", "error"),
                "data": result,
                "carteirinha_id": job.carteirinha_id,
            }
            # Propaga a mensagem de falha (ex.: 'Falha no login antes de OP2') para o
            # dispatcher registra-la — sem isso o log virava 'Unknown error from server'.
            if envelope["status"] != "success" and result.get("message"):
                envelope["message"] = result["message"]
            return envelope
        except Exception as e:
            _log_error(job.job_id, job.carteirinha_id, f"CLMF Evolucao Server Crash: {e}")
            try:
                clmf_scraper.close_driver()
            except Exception:
                pass
            return {"status": "error", "message": str(e), "carteirinha_id": job.carteirinha_id}


def _process_job_unimed(job: JobRequest):
    """Processa job do convênio Unimed via UnimedScraper (legado)."""
    global unimed_scraper, last_activity_time

    # Guarda: job de evoluções chegando aqui significa rotina ausente no payload
    # (dispatcher desatualizado) — falhar explícito em vez de raspar a âncora.
    if job.carteirinha == "EVOLUCOES-CLMF":
        msg = ("Job de evoluções (âncora EVOLUCOES-CLMF) roteado ao scraper Unimed — "
               "rotina ausente no payload (dispatcher desatualizado?)")
        _log_error(job.job_id, job.carteirinha_id, msg)
        return {"status": "error", "message": msg, "carteirinha_id": job.carteirinha_id}

    if not unimed_scraper:
        raise HTTPException(status_code=503, detail="Scraper não inicializado")

    # Auto-recovery: verificar e reiniciar driver se necessário
    with driver_lock:
        err = _ensure_driver(unimed_scraper, "Unimed", needs_login=True)
        if err:
            return {"status": "error", "message": err, "carteirinha_id": job.carteirinha_id}
        last_activity_time = datetime.now()

    try:
        with driver_lock:
            if not unimed_scraper.is_driver_alive():
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
                    # close_driver já mata apenas os processos DESTE worker
                    unimed_scraper.close_driver()
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
