import sys
import os
import time
import requests
import logging
from datetime import datetime, timedelta


import socket
import threading

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

if sys.stdout is None: sys.stdout = FileLogStream("dispatcher_debug.log")
if sys.stderr is None: sys.stderr = FileLogStream("dispatcher_err.log")

# Use local Worker modules (independent of backend)
from database import SessionLocal
from models import Job, BaseGuia, Log, Carteirinha, Procedimento

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "http://localhost:8000")
HOSTNAME = socket.gethostname()




class QueueLogger:
    def __init__(self, queue, prefix="Dispatcher"):
        self.queue = queue
        self.prefix = prefix
    def write(self, message):
        if message.strip():
            self.queue.put(f"[{self.prefix}] {message.strip()}")
    def flush(self):
        pass
    def isatty(self):
        return False


# Missing CRUD function implemented inline
def get_pending_job(db):
    try:
        # Get oldest pending job with row-level lock to prevent duplicate assignment
        job = db.query(Job).filter(
            Job.status == "pending",
            (Job.locked_by == None) | (Job.locked_by == "")
        ).order_by(Job.created_at.asc()).with_for_update(skip_locked=True).first()
        return job
    except Exception as e:
        logger.error(f"Error fetching pending job: {e}")
        db.rollback()  # Rollback to release any locks on error
        return None


def retry_failed_jobs(db):
    try:
        # Check for jobs with status='error' and attempts < 3 and not locked
        failed_jobs = db.query(Job).filter(
            Job.status == "error",
            Job.attempts < 3,
            (Job.locked_by == None) | (Job.locked_by == "")
        ).all()
        
        if failed_jobs:
            logger.info(f"Retrying {len(failed_jobs)} failed jobs...")
            for job in failed_jobs:
                job.status = "pending"
                job.updated_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        logger.error(f"Error resetting failed jobs: {e}")


def recover_stuck_jobs(db):
    """Reset jobs that are 'processing' for too long (>15min) with no progress."""
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=15)
        stuck_jobs = db.query(Job).filter(
            Job.status == "processing",
            Job.updated_at < cutoff
        ).all()
        for job in stuck_jobs:
            logger.warning(f"Recovering stuck job {job.id} (locked_by: {job.locked_by})")
            job.status = "pending"
            job.locked_by = None
            job.attempts = max(0, (job.attempts or 0))  # keep attempts count
            job.updated_at = datetime.utcnow()
        if stuck_jobs:
            db.commit()
            logger.info(f"Recovered {len(stuck_jobs)} stuck jobs")
    except Exception as e:
        logger.error(f"Error recovering stuck jobs: {e}")
        try:
            db.rollback()
        except: pass

def send_heartbeat(status_map, cmd_queue=None, active_workers=None):
    """
    Sends heatbeat for each worker/slot.
    """
    try:
        for url, meta in status_map.items():
            # Derive a unique name for this slot
            try:
                port = url.split(":")[-1]
            except:
                port = "0"
            
            worker_name = f"{HOSTNAME}-{port}"
            
            # Check if worker is actually reachable
            real_status = "offline"
            try:
                # Short timeout check
                hr = requests.get(url, timeout=1)
                if hr.status_code == 200:
                    real_status = meta["status"] # Trust internal state if reachable
            except:
                # Unreachable. Distinguish Crash vs Stop based on active_workers dict
                is_expected = False
                if active_workers:
                    try:
                        # Keys in active_workers are likely ints (port)
                        is_expected = active_workers.get(int(port), False)
                    except:
                        pass
                
                if is_expected:
                    real_status = "error" # Should be running, but isn't -> CRASH
                else:
                    real_status = "offline" # NOT expected -> Offline

            payload = {
                "hostname": worker_name,
                "status": real_status,
                "current_job_id": meta.get("last_job") if meta["status"] == "busy" else None,
                "meta": {"url": url, "type": "slot"}
            }
            
            try:
                resp = requests.post(f"{BACKEND_API_URL}/workers/heartbeat", json=payload, timeout=5)
                # ...
                data = resp.json()
                
                if data.get("command") == "restart":
                     # Force reset in-memory status so dispatcher knows server is available again
                     status_map[url]["status"] = "idle"
                     status_map[url]["last_job"] = None
                     logger.info(f"Restart command received for {url}. In-memory status reset to idle.")
                     if cmd_queue:
                         cmd_queue.put(("RESTART", int(port)))
                     else:
                         try: requests.post(f"{url}/restart", timeout=10)
                         except: pass

            except Exception as req_e:
                pass

    except Exception as e:
        logger.error(f"Heartbeat Loop Error: {e}")

def start_heartbeat_loop(status_map, interval=10, cmd_queue=None, active_workers=None):
    def loop():
        while True:
            send_heartbeat(status_map, cmd_queue, active_workers)
            time.sleep(interval)
    
    t = threading.Thread(target=loop, daemon=True)
    t.start()


def run_dispatcher(server_urls_str=None, stagger=15, log_queue=None, cmd_queue=None, active_workers=None):
    if log_queue:
        sys.stdout = QueueLogger(log_queue, "Dispatcher")
        sys.stderr = QueueLogger(log_queue, "Dispatcher ERR")

    logger.info("Starting Dispatcher...")
    
    # Track active threads for watchdog
    active_threads = {}  # {server_url: {"thread": obj, "started_at": datetime, "job_id": int}}
    
    if server_urls_str:
        servers = [url.strip() for url in server_urls_str.split(",")]
    else:
        servers = [url.strip() for url in os.environ.get("API_SERVER_URLS", "http://127.0.0.1:8010").split(",")]

    server_status_map = {url: {"status": "idle", "last_job": None} for url in servers}
    dispatch_stagger_val = stagger

    # Start Heartbeat Thread
    start_heartbeat_loop(server_status_map, cmd_queue=cmd_queue, active_workers=active_workers)





    # Define call_server outside loop to avoid redefinition, but it needs access to server_status_map
    # easier to keep it inside or pass map as arg. Let's pass map as arg or use closure here.
    
    def call_server(url, job_id, carteirinha, carteirinha_id, rotina, params, status_map):
        try:
            payload = {
                "job_id": job_id,
                "carteirinha_id": carteirinha_id,
                "carteirinha": carteirinha,
                "paciente": "",
                "rotina": rotina or "",    # identifica qual scraper usar no server
                "params": params or {},     # parâmetros específicos do convênio
            }
            # Log attempt
            try:
                temp_log_session = SessionLocal()
                temp_log_session.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="INFO", message=f"Dispatching to {url}"))
                temp_log_session.commit()
                temp_log_session.close()
            except: pass

            
            resp = requests.post(f"{url}/process_job", json=payload, timeout=300)
            
            try:
                data = resp.json()
            except ValueError: 
                # JSONDecodeError
                err_msg = f"Invalid JSON ({resp.status_code}): {resp.text[:200]}"
                thread_db = SessionLocal() # Need to init here as original code did inside try? No, original had it in exception catch?
                # Original code: thread_db was not inited before this exception block in try/except line 116?
                # Wait, original line 121: thread_db = SessionLocal()
                # line 116 usage seems risky if thread_db not defined.
                # Let's fix that safely.
                # Re-opening session for logging error
                err_db = SessionLocal()
                err_db.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="ERROR", message=f"Worker Protocol Error: {err_msg}"))
                err_db.commit()
                err_db.close()
                raise Exception(err_msg)

            # New DB Session for thread
            thread_db = SessionLocal()
            current_job = thread_db.query(Job).filter(Job.id == job_id).first()
            
            valida_payload = None
            if data.get("status") == "success":
                current_job.status = "success"
                raw_results = data.get("data", {})
                
                # Support both old format (list) and new format (dict with guias_scraped/valida_prestador)
                import json as json_lib
                if isinstance(raw_results, dict):
                    results = raw_results.get("guias_scraped", [])
                    valida_payload = raw_results.get("valida_prestador", None)
                else:
                    # Legacy: plain list returned
                    results = raw_results if isinstance(raw_results, list) else []
                    valida_payload = None
                
                # Log valida_prestador JSON
                if valida_payload:
                    try:
                        valida_json_str = json_lib.dumps(valida_payload, ensure_ascii=False, indent=2)
                        vp_log_session = SessionLocal()
                        vp_log_session.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="INFO", message=f"valida_prestador JSON:\n{valida_json_str}"))
                        vp_log_session.commit()
                        vp_log_session.close()
                    except Exception as vp_e:
                        logger.error(f"Failed to log valida_prestador: {vp_e}")

                # Save guias to BaseGuia
                try:
                    count_inserted = 0
                    count_updated = 0

                    def parse_date(date_str):
                        if not date_str or not isinstance(date_str, str):
                            return None
                        try:
                            return datetime.strptime(date_str.strip(), "%d/%m/%Y").date()
                        except:
                            return None

                    logger.info(f"Processing {len(results)} items from worker response.")
                    for item in results:
                        logger.info(f"Processing item: {item}")
                        # Filter by Procedimento table
                        code_proc = item.get("codigo_procedimento") or item.get("codigo_terapia")
                        # Fetch carteirinha object to get id_pagamento
                        cart_db_obj = thread_db.query(Carteirinha).filter(Carteirinha.id == carteirinha_id).first()
                        if not cart_db_obj:
                            logger.error(f"Carteirinha {carteirinha_id} not found in DB during sync.")
                            continue

                        # Check Procedimento table
                        proc_authorized = thread_db.query(Procedimento).filter(
                            Procedimento.id_convenio == cart_db_obj.id_pagamento,
                            Procedimento.codigo_procedimento == code_proc
                        ).first()
                        
                        if not proc_authorized:
                            logger.warning(f"Ignoring guia {item.get('numero_guia')}: Procedure {code_proc} not authorized for convenio {cart_db_obj.id_pagamento}")
                            # Log the skip
                            try:
                                thread_db.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="WARN", message=f"Guia {item.get('numero_guia')} Ignorada: Procedimento {code_proc} não autorizado."))
                                thread_db.commit()
                            except: pass
                            continue
                        # Conversion helpers
                        try:
                            qtd_solic_val = int(item.get("qtde_solicitada"))
                        except:
                            qtd_solic_val = 0
                            
                        try:
                            qtd_aut_val = int(item.get("qtde_autorizada"))
                        except:
                            qtd_aut_val = 0
                            
                        try:
                            guia_num = str(item.get("numero_guia", "")).strip()
                        except:
                            guia_num = str(item.get("numero_guia"))
                        
                        data_auth_parsed = parse_date(item.get("data_autorizacao"))
                        validade_parsed = parse_date(item.get("validade_senha"))
                        
                        logger.info(f"Parsed values - Guia: {guia_num}, DatAuth: {data_auth_parsed}, Val: {validade_parsed}, QtdSol: {qtd_solic_val}, QtdAut: {qtd_aut_val}")
                        
                        # UPSERT Logic: Check if exists
                        logger.info("Checking if guia exists in DB...")
                        try:
                            existing_guia = thread_db.query(BaseGuia).filter(
                                BaseGuia.carteirinha_id == carteirinha_id,
                                BaseGuia.guia == guia_num
                            ).first()
                            logger.info(f"DB Query result: {existing_guia}")
                        except Exception as db_q_err:
                            logger.error(f"DB Query Failed: {db_q_err}")
                            raise db_q_err
                        
                        if existing_guia:
                            # Update
                            logger.info("Updating existing guia...")
                            existing_guia.data_autorizacao = data_auth_parsed
                            existing_guia.senha = item.get("senha")
                            existing_guia.validade = validade_parsed
                            existing_guia.codigo_procedimento = item.get("codigo_procedimento")
                            existing_guia.qtde_solicitada = qtd_solic_val
                            existing_guia.sessoes_autorizadas = qtd_aut_val
                            existing_guia.updated_at = datetime.utcnow()
                            
                            # Sync valida_prestador status for this specific guia
                            if valida_payload and "guias" in valida_payload:
                                guia_status = valida_payload["guias"].get(guia_num)
                                if guia_status:
                                    existing_guia.valida_prestador = guia_status
                            
                            count_updated += 1
                        else:
                            # Insert
                            logger.info(f"Inserting new guia: {guia_num}")
                            try:
                                new_guia = BaseGuia(
                                    carteirinha_id=carteirinha_id,
                                    guia=guia_num,
                                    data_autorizacao=data_auth_parsed,
                                    senha=item.get("senha"),
                                    validade=validade_parsed,
                                    codigo_procedimento=item.get("codigo_procedimento") or item.get("codigo_terapia"),
                                    qtde_solicitada=qtd_solic_val,
                                    sessoes_autorizadas=qtd_aut_val,
                                    created_at=datetime.utcnow()
                                )
                                
                                # Sync valida_prestador status for this new guia
                                if valida_payload and "guias" in valida_payload:
                                    guia_status = valida_payload["guias"].get(guia_num)
                                    if guia_status:
                                        new_guia.valida_prestador = guia_status
                                        
                                logger.info("BaseGuia object created successfully")
                                thread_db.add(new_guia)
                                logger.info("Added to session, incrementing count")
                                count_inserted += 1
                            except Exception as insert_err:
                                logger.error(f"Failed to create/add BaseGuia: {insert_err}")
                                raise insert_err
                    
                    # Explicit Commit Log
                    logger.info("Committing changes to DB...")
                    thread_db.commit()
                    logger.info("Commit successful.")
                    
                    # Create fresh session for final log
                    log_session = SessionLocal()
                    log_session.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="INFO", message=f"Sync complete. Inserted: {count_inserted}, Updated: {count_updated}"))
                    log_session.commit()
                    log_session.close()
                except Exception as save_e:
                    logger.error(f"Exception during save: {save_e}")
                    # Create fresh session for error log
                    err_log_session = SessionLocal()
                    err_log_session.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="ERROR", message=f"Error saving results: {save_e}"))
                    err_log_session.commit()
                    err_log_session.close()
                    current_job.status = "error"
            else:
                current_job.status = "error"
                # Log error from server
                err_msg = data.get("message") or data.get("detail") or "Unknown error from server"
                thread_db.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="ERROR", message=f"Worker Error: {err_msg}"))
            
            current_job.locked_by = None
            current_job.updated_at = datetime.utcnow()
            # Save valida_prestador JSON to the job row
            if valida_payload:
                current_job.valida_prestador = valida_payload
                
                # Robustness: Also sync all guides from valida_payload to base_guias 
                # even if they weren't in the successful scrape list (e.g. if they were skipped/blocked)
                guias_map = valida_payload.get("guias")
                if isinstance(guias_map, dict):
                    for g_num, g_status in guias_map.items():
                        try:
                            existing_g = thread_db.query(BaseGuia).filter(
                                BaseGuia.carteirinha_id == carteirinha_id,
                                BaseGuia.guia == g_num
                            ).first()
                            if existing_g:
                                existing_g.valida_prestador = g_status
                                logger.info(f"Robustness Sync: Updated status for Guia {g_num}")
                        except Exception as robust_e:
                            logger.error(f"Robustness Sync failed for {g_num}: {robust_e}")

            thread_db.commit()
            thread_db.close()
            
        except Exception as e:
            logger.error(f"Error calling server {url}: {e}")
            thread_db = SessionLocal()
            current_job = thread_db.query(Job).filter(Job.id == job_id).first()
            if current_job:
                current_job.status = "error"
                current_job.locked_by = None
                current_job.updated_at = datetime.utcnow()
            
                # Log dispatcher error
                try:
                    thread_db.add(Log(job_id=job_id, carteirinha_id=carteirinha_id, level="ERROR", message=f"Dispatcher Failed: {str(e)}"))
                except: pass
                
                thread_db.commit()
            thread_db.close()
            
        finally:
            status_map[url]["status"] = "idle"

    while True:
        try:
            db = SessionLocal()
        except Exception as db_open_err:
            logger.error(f"Dispatcher: could not open DB session: {db_open_err}")
            time.sleep(15)
            continue
        try:

            # 0. Recover stuck jobs (processing > 15 min)
            recover_stuck_jobs(db)
            
            # 0.5 Retry failed jobs
            retry_failed_jobs(db)

            # 1. Watchdog: check for dead or timed-out threads
            for url, info in list(active_threads.items()):
                thread = info["thread"]
                started = info["started_at"]
                job_id = info["job_id"]
                
                elapsed = (datetime.utcnow() - started).total_seconds() / 60.0
                
                if not thread.is_alive():
                    # Thread finished (normally handled by finally block, but double-check)
                    if server_status_map[url]["status"] == "busy":
                        logger.warning(f"Watchdog: Thread for {url} died but status still busy. Forcing reset.")
                        server_status_map[url]["status"] = "idle"
                        server_status_map[url]["last_job"] = None
                        # Mark job as error in DB
                        try:
                            stuck_job = db.query(Job).filter(Job.id == job_id).first()
                            if stuck_job and stuck_job.status == "processing":
                                stuck_job.status = "error"
                                stuck_job.locked_by = None
                                stuck_job.updated_at = datetime.utcnow()
                                db.commit()
                        except: pass
                    del active_threads[url]
                elif elapsed > 10:
                    # Thread alive but exceeded 10 min timeout
                    logger.warning(f"Watchdog: Thread for {url} exceeded 10 min (job {job_id}). Force-resetting status.")
                    server_status_map[url]["status"] = "idle"
                    server_status_map[url]["last_job"] = None
                    # Mark job as error in DB
                    try:
                        stuck_job = db.query(Job).filter(Job.id == job_id).first()
                        if stuck_job and stuck_job.status == "processing":
                            stuck_job.status = "error"
                            stuck_job.locked_by = None
                            stuck_job.updated_at = datetime.utcnow()
                            db.commit()
                    except: pass
                    del active_threads[url]

            # 2. Check available servers
            available_servers = [url for url, meta in server_status_map.items() if meta["status"] == "idle"]
            
            if not available_servers:
                logger.info("No servers available. Waiting...")
            else:
                for server_url in available_servers:
                    # Get Job (with row-level lock)
                    job = get_pending_job(db)
                    if not job:
                        logger.info("No pending jobs.")
                        break
                    
                    logger.info(f"Assigning Job {job.id} to {server_url}")
                    
                    # Lock Job
                    job.status = "processing"
                    job.locked_by = server_url
                    job.attempts += 1
                    job.updated_at = datetime.utcnow()
                    db.commit()
                    
                    # Update Local Server Status
                    server_status_map[server_url]["status"] = "busy"
                    server_status_map[server_url]["last_job"] = job.id
                    
                    import threading
                    # Fetch carteirinha
                    cart_obj = job.carteirinha_rel
                    t = threading.Thread(
                        target=call_server,
                        args=(
                            server_url,
                            job.id,
                            cart_obj.carteirinha,
                            cart_obj.id,
                            getattr(job, "rotina", "") or "",   # tipo do job (legado = "")
                            getattr(job, "params", {}) or {},   # parâmetros arbitrários
                            server_status_map,
                        )
                    )
                    t.start()
                    
                    # Track thread for watchdog
                    active_threads[server_url] = {
                        "thread": t,
                        "started_at": datetime.utcnow(),
                        "job_id": job.id
                    }
                    
                    # Stagger between assignments to avoid DB spikes
                    time.sleep(1) 
            
            db.close()
            # If we just assigned jobs, loop quickly. If we hit "No pending jobs", sleep slightly longer.
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Dispatcher Loop Error: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_dispatcher()
