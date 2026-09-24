"""Sobe servers (8010-13) + dispatcher sem GUI — uso em linha de comando/teste."""
import multiprocessing
import time

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Worker'))

from server import run_server
from dispatcher import run_dispatcher

if __name__ == "__main__":
    log_q = multiprocessing.Queue()
    cmd_q = multiprocessing.Queue()
    urls = ",".join(f"http://127.0.0.1:{p}" for p in (8010, 8011, 8012, 8013))
    for port in (8010, 8011, 8012, 8013):
        multiprocessing.Process(target=run_server, args=(port, log_q), daemon=False).start()
        time.sleep(1)
    multiprocessing.Process(target=run_dispatcher, args=(urls, 15, log_q, cmd_q), daemon=False).start()
    print("[start_stack] 4 servers + dispatcher iniciados", flush=True)
    while True:
        time.sleep(60)
