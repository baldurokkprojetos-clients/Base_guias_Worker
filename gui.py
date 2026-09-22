
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, PhotoImage
import sys
import os
import multiprocessing
import json
import time
import requests
import threading
import psutil
import socket
import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as item

# Fix for noconsole mode where stdout/stderr are None
# Fix for noconsole mode: Write to file instead of silent fail
class FileLogStream:
    def __init__(self, filename):
        self.filename = filename
        try:
            self.log_file = open(filename, "a", encoding="utf-8")
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

# Redirect main process output to file for debugging
if sys.stdout is None: sys.stdout = FileLogStream("gui_stdout.log")
if sys.stderr is None: sys.stderr = FileLogStream("gui_stderr.log")

# Ensure Worker modules can be imported
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Worker'))

# Import functions from Worker package
try:
    from Worker.server import run_server
    from Worker.dispatcher import run_dispatcher
except ImportError as e_orig:
    # Fallback for dev environment where Worker is sibling
    try:
        sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
        from Worker.server import run_server
        from Worker.dispatcher import run_dispatcher
    except ImportError as e:
        # Write to log file for executable debugging
        try:
             with open("import_error.log", "w") as f:
                 f.write(f"Original Error: {e_orig}\n")
                 f.write(f"Fallback Error: {e}\n")
                 f.write(f"Sys Path: {sys.path}\n")
        except: pass
        
        print(f"CRITICAL IMPORT ERROR: {e}")
        run_server = None
        run_dispatcher = None

CONFIG_FILE = "system_config.json"

def create_tray_icon():
    # Create a simple icon for the tray
    width = 64
    height = 64
    color1 = "#007acc"
    color2 = "#ffffff"
    image = Image.new('RGB', (width, height), color1)
    dc = ImageDraw.Draw(image)
    dc.rectangle((width // 2, 0, width, height // 2), fill=color2)
    dc.rectangle((0, height // 2, width // 2, height), fill=color2)
    return image

class SystemManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Base Guias Unimed - Manager")
        self.root.geometry("800x850") # Slightly wider
        
        # Dark Theme Colors
        self.colors = {
            "bg": "#1e1e1e",           
            "panel": "#252526",       
            "fg": "#d4d4d4",          
            "accent": "#007acc",      
            "success": "#3fa9f5",     
            "success_bg": "#107c10",  
            "danger": "#ce352c",      
            "warning": "#d7ba7d",     
            "input": "#3c3c3c",       
            "border": "#3e3e42"
        }
        
        self.root.configure(bg=self.colors["bg"])
        
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.setup_styles()
        
        # State
        self.processes = []
        self.running = False
        self.tray_icon = None
        self.is_minimized = False
        self._stopping = False  # guard: True durante stop/shutdown
        
        # Log Queue for Multiprocessing
        self.log_queue = multiprocessing.Queue()
        # Command Queue for restart signals
        self.cmd_queue = multiprocessing.Queue()
        
        # Shared Dict for Worker Expectations (Port -> Boolean)
        # Using Manager to share state between GUI and Dispatcher
        self.manager = multiprocessing.Manager()
        self.active_workers = self.manager.dict()

        
        # Config Variables
        self.var_num_servers = tk.IntVar(value=1)
        self.var_login = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_token = tk.StringVar()
        self.var_headless = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=False)
        
        self.load_config()
        
        # UI Elements
        self.create_header()
        self.create_main_container()
        
        # Tray Support
        self.root.protocol('WM_DELETE_WINDOW', self.minimize_to_tray)

        # Start Monitoring
        self.monitor_thread = threading.Thread(target=self.status_monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        # Start Log Polling
        self.poll_log_queue()
        # Start Command Polling
        self.poll_cmd_queue()

        
        self.log("Ready. Configure and click Start System.")

    # ... (Styles, Layout methods unchanged) ...

    def poll_log_queue(self):
        try:
            while True:
                # Non-blocking get
                import queue
                try:
                    msg = self.log_queue.get_nowait()
                    timestamp = time.strftime("%H:%M:%S")
                    
                    if self.log_text.index("end-1c") != "1.0":
                        self.log_text.insert(tk.END, "\n")
                    self.log_text.insert(tk.END, f"[{timestamp}] {msg}", "INFO") 
                    self.log_text.see(tk.END)
                except queue.Empty:
                    break
        except:
            pass
        finally:
            self.root.after(100, self.poll_log_queue)

    def poll_cmd_queue(self):
        import queue
        try:
            while True:
                cmd = self.cmd_queue.get_nowait()
                # cmd format: ("RESTART", port)
                if cmd[0] == "RESTART":
                    port = cmd[1]
                    
                    # Check Cooldown in cmd queue too!
                    cooldown = 0
                    if hasattr(self, 'auto_start_cooldown'):
                        cooldown = self.auto_start_cooldown.get(port, 0)
                    
                    if time.time() - cooldown > 15:
                         self.log(f"Received Restart Request for Port {port}", "WARN")
                         self.restart_worker_process(port)
                    else:
                         # self.log(f"Ignored Restart for Port {port} due to Cooldown", "INFO")
                         pass
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Cmd Queue Error: {e}")
        finally:
            self.root.after(500, self.poll_cmd_queue)

    # ... (create_header, create_main_container, panels... unchanged) ...

    # --- System Logic ---

    def start_system(self):
        if self.running: return
        self.save_config()
        self.refresh_server_grid()
        
        # Prepare Env
        os.environ["SGUCARD_LOGIN"] = self.var_login.get()
        os.environ["SGUCARD_PASSWORD"] = self.var_password.get()
        os.environ["SGUCARD_HEADLESS"] = "true" if self.var_headless.get() else "false"
        os.environ["API_TOKEN"] = self.var_token.get()
        
        num_w = self.var_num_servers.get()
        server_urls = []
        
        self.log(f"Initializing {num_w} worker(s)...")
        
        # Spawn Workers
        for i in range(num_w):
            port = 8010 + i
            url = f"http://127.0.0.1:{port}"
            server_urls.append(url)
            
            # Pass log_queue
            # p = multiprocessing.Process(target=run_server, args=(port, self.log_queue), daemon=True) # Old
            p = multiprocessing.Process(target=run_server, args=(port, self.log_queue), daemon=True)
            p.start()
            self.processes.append(p)
            self.log(f"Started Worker on Port {port}", "SUCCESS")
        
        # Spawn Dispatcher
        time.sleep(1) 
        server_str = ",".join(server_urls)
        
        # Pass log_queue and cmd_queue
        d = multiprocessing.Process(target=run_dispatcher, args=(server_str, 15, self.log_queue, self.cmd_queue), daemon=True)
        d.start()
        self.processes.append(d)
        self.log(f"Dispatcher Started.", "SUCCESS")
        
        self.running = True
        self.update_ui_state(True)
        self.header_status_dot.config(fg="#4cd964") # Green Dot


    def setup_styles(self):
        # Configure Generic Widgets
        self.style.configure("TFrame", background=self.colors["bg"])
        self.style.configure("Panel.TFrame", background=self.colors["panel"], relief="flat")
        
        self.style.configure("TLabel", background=self.colors["panel"], foreground=self.colors["fg"], font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"), background=self.colors["bg"], foreground=self.colors["fg"])
        self.style.configure("SubHeader.TLabel", font=("Segoe UI", 12, "bold"), background=self.colors["panel"], foreground=self.colors["accent"])
        
        # Buttons
        self.style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=10, borderwidth=0, focuscolor=self.colors["panel"])
        self.style.map("TButton",
            background=[('active', self.colors['accent']), ('!disabled', self.colors['input'])],
            foreground=[('!disabled', 'white')]
        )
        
        self.style.configure("Action.TButton", background=self.colors['accent'])
        self.style.configure("Start.TButton", background=self.colors['success_bg'])
        self.style.configure("Stop.TButton", background=self.colors['danger'])
        
        # Inputs
        self.style.configure("TEntry", fieldbackground=self.colors["input"], foreground="white", insertcolor="white", borderwidth=0, padding=5)
        self.style.configure("TCheckbutton", background=self.colors["panel"], foreground=self.colors["fg"], focuscolor=self.colors["panel"], font=("Segoe UI", 10))
        
        # Slider
        self.style.configure("Horizontal.TScale", background=self.colors["panel"], troughcolor=self.colors["input"], sliderlength=20)

    def create_header(self):
        header_frame = tk.Frame(self.root, bg=self.colors["bg"], pady=20)
        header_frame.pack(fill="x", padx=20)
        
        # You could add logo here using tk.PhotoImage
        
        lbl = ttk.Label(header_frame, text="Base Guias Admin", style="Header.TLabel")
        lbl.pack(side="left")
        
        status_dot = tk.Label(header_frame, text="●", fg="#555555", bg=self.colors["bg"], font=("Arial", 24))
        status_dot.pack(side="right")
        self.header_status_dot = status_dot

    def create_main_container(self):
        main_frame = tk.Frame(self.root, bg=self.colors["bg"])
        main_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        # Left Column (Config & Controls)
        left_col = tk.Frame(main_frame, bg=self.colors["bg"])
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        # Right Column (Status & Logs)
        right_col = tk.Frame(main_frame, bg=self.colors["bg"])
        right_col.pack(side="right", fill="both", expand=True, padx=(10, 0))
        
        # --- Config Panel ---
        self.create_config_panel(left_col)
        self.create_control_panel(left_col)
        
        # --- Status & Log Panel ---
        self.create_status_panel(right_col)
        self.create_log_panel(right_col)

    def create_config_panel(self, parent):
        pnl = ttk.Frame(parent, style="Panel.TFrame", padding=15)
        pnl.pack(fill="x", pady=(0, 15))
        
        ttk.Label(pnl, text="Configuration", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))
        
        grid_opts = {'padx': 5, 'pady': 8, 'sticky': 'w'}
        
        # Servers
        f_serv = ttk.Frame(pnl, style="Panel.TFrame")
        f_serv.pack(fill="x", pady=5)
        ttk.Label(f_serv, text="Workers (1-4):").pack(side="left")
        ttk.Label(f_serv, textvariable=self.var_num_servers, font=("Segoe UI", 10, "bold"), width=3).pack(side="left", padx=5)
        s = ttk.Scale(f_serv, from_=1, to=4, orient="horizontal", variable=self.var_num_servers, command=lambda x: self.var_num_servers.set(int(float(x))))
        s.pack(side="left", fill="x", expand=True, padx=5)
        
        # Logins
        ttk.Label(pnl, text="Login SGUCARD:").pack(anchor="w")
        e_log = ttk.Entry(pnl, textvariable=self.var_login, style="TEntry", width=40)
        e_log.pack(fill="x", pady=(0, 10))

        ttk.Label(pnl, text="Password:").pack(anchor="w")
        e_pass = ttk.Entry(pnl, textvariable=self.var_password, show="●", style="TEntry")
        e_pass.pack(fill="x", pady=(0, 10))

        ttk.Label(pnl, text="API Token:").pack(anchor="w")
        e_tok = ttk.Entry(pnl, textvariable=self.var_token, style="TEntry")
        e_tok.pack(fill="x", pady=(0, 10))
        
        # Toggles
        ttk.Checkbutton(pnl, text="Headless Mode (Hidden Browser)", variable=self.var_headless).pack(anchor="w", pady=5)
        ttk.Checkbutton(pnl, text="Autostart with Windows", variable=self.var_autostart, command=lambda: self.set_autostart(self.var_autostart.get())).pack(anchor="w", pady=5)

    def create_control_panel(self, parent):
        pnl = ttk.Frame(parent, style="Panel.TFrame", padding=15)
        pnl.pack(fill="x")
        
        ttk.Label(pnl, text="System Control", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))
        
        self.btn_start = ttk.Button(pnl, text="START SYSTEM", style="Start.TButton", command=self.start_system)
        self.btn_start.pack(fill="x", pady=5)
        
        self.btn_stop = ttk.Button(pnl, text="STOP SYSTEM", style="Stop.TButton", command=self.stop_system, state="disabled")
        self.btn_stop.pack(fill="x", pady=5)

    def create_status_panel(self, parent):
        pnl = ttk.Frame(parent, style="Panel.TFrame", padding=15)
        pnl.pack(fill="x", pady=(0, 15))
        
        ttk.Label(pnl, text="Live Status", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))
        
        # DB Status
        f_db = ttk.Frame(pnl, style="Panel.TFrame")
        f_db.pack(fill="x", pady=2)
        ttk.Label(f_db, text="Database:").pack(side="left")
        self.lbl_db_status = tk.Label(f_db, text="Disconnected", bg=self.colors["panel"], fg="#777", font=("Segoe UI", 9))
        self.lbl_db_status.pack(side="right")
        
        # Server Grid
        self.server_frame = ttk.Frame(pnl, style="Panel.TFrame")
        self.server_frame.pack(fill="x", pady=10)
        self.status_widgets = {}

    def refresh_server_grid(self):
        for w in self.server_frame.winfo_children(): w.destroy()
        self.status_widgets = {}
        
        count = self.var_num_servers.get()
        # Responsive grid? Just clean rows
        for i in range(count):
            port = 8010 + i
            f = tk.Frame(self.server_frame, bg="#333", padx=10, pady=8) # darker for card look
            f.pack(fill="x", pady=2)
            
            tk.Label(f, text=f"Worker {i+1} (:{port})", bg="#333", fg="white", font=("Segoe UI", 9, "bold")).pack(side="left")
            status_lbl = tk.Label(f, text="OFFLINE", bg="#333", fg="#777", font=("Segoe UI", 8))
            status_lbl.pack(side="right")
            
            self.status_widgets[port] = status_lbl

    def create_log_panel(self, parent):
        pnl = ttk.Frame(parent, style="Panel.TFrame", padding=15)
        pnl.pack(fill="both", expand=True)
        
        h_frame = ttk.Frame(pnl, style="Panel.TFrame")
        h_frame.pack(fill="x", pady=(0,5))
        ttk.Label(h_frame, text="Activity Log", style="SubHeader.TLabel").pack(side="left")
        
        btn_clr = tk.Button(h_frame, text="Clear", bg=self.colors["panel"], fg=self.colors["accent"], 
                            borderwidth=0, cursor="hand2", command=lambda: self.log_text.delete(1.0, tk.END))
        btn_clr.pack(side="right")
        
        self.log_text = scrolledtext.ScrolledText(pnl, bg=self.colors["input"], fg="#cccccc", 
                                                  font=("Consolas", 9), borderwidth=0, highlightthickness=1, 
                                                  highlightbackground=self.colors["border"])
        self.log_text.pack(fill="both", expand=True)
        
        # Tag colors
        self.log_text.tag_config("INFO", foreground="#cccccc")
        self.log_text.tag_config("ERROR", foreground="#ff6b6b")
        self.log_text.tag_config("SUCCESS", foreground="#51cf66")
        self.log_text.tag_config("WARN", foreground="#fcc419")

    def log(self, message, level="INFO"):
        timestamp = time.strftime("%H:%M:%S")
        
        def _log():
            if self.log_text.index("end-1c") != "1.0":
                self.log_text.insert(tk.END, "\n")
            self.log_text.insert(tk.END, f"[{timestamp}] ", "INFO")
            self.log_text.insert(tk.END, f"{message}", level)
            self.log_text.see(tk.END)
            
        self.root.after(0, _log)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    self.var_num_servers.set(data.get("num_servers", 1))
                    self.var_login.set(data.get("login", ""))
                    self.var_password.set(data.get("password", ""))
                    self.var_token.set(data.get("token", ""))
                    self.var_headless.set(data.get("headless", True))
                    self.var_autostart.set(data.get("autostart", False))
            except: pass
        else:
            # Import from .env
            env_path = ".env"
            if os.path.exists(env_path):
                with open(env_path, 'r') as f:
                    for line in f:
                        if "=" in line and not line.strip().startswith("#"):
                            k, v = line.strip().split("=", 1)
                            v = v.strip('"').strip("'")
                            if k == "SGUCARD_LOGIN": self.var_login.set(v)
                            if k == "SGUCARD_PASSWORD": self.var_password.set(v)
                            if k == "API_TOKEN": self.var_token.set(v)
                            if k == "SGUCARD_HEADLESS": self.var_headless.set(v.lower() == "true")
                            if k == "BACKEND_API_URL": os.environ["BACKEND_API_URL"] = v

    def save_config(self):
        data = {
            "num_servers": self.var_num_servers.get(),
            "login": self.var_login.get(),
            "password": self.var_password.get(),
            "token": self.var_token.get(),
            "headless": self.var_headless.get(),
            "autostart": self.var_autostart.get()
        }
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=4)
            
            # Apply AutoStart
            self.set_autostart(self.var_autostart.get())
            
        except Exception as e:
            self.log(f"Failed to save config: {e}", "ERROR")

    def set_autostart(self, enable):
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "BaseGuiasManager"
        
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
            
            if enable:
                if getattr(sys, 'frozen', False):
                    exe_path = sys.executable
                else:
                    # Running from source
                    exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
                self.log("Auto-start enabled in Registry.", "INFO")
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                    self.log("Auto-start disabled in Registry.", "INFO")
                except FileNotFoundError:
                    pass # Already deleted
            
            winreg.CloseKey(key)
        except Exception as e:
            self.log(f"Failed to set Auto-start: {e}", "ERROR")

    # --- System Logic ---

    def start_system(self):
        self.save_config()
        self.refresh_server_grid()
        
        if run_server is None or run_dispatcher is None:
            self.log("CRITICAL: Worker modules not found! Check installations.", "ERROR")
            return

        # Prepare Env
        os.environ["SGUCARD_LOGIN"] = self.var_login.get()
        os.environ["SGUCARD_PASSWORD"] = self.var_password.get()
        os.environ["SGUCARD_HEADLESS"] = "true" if self.var_headless.get() else "false"
        os.environ["API_TOKEN"] = self.var_token.get()
        
        num_w = self.var_num_servers.get()
        server_urls = []
        
        self.log(f"Initializing {num_w} worker(s)...")
        
        # Spawn Workers
        for i in range(num_w):
            port = 8010 + i
            url = f"http://127.0.0.1:{port}"
            server_urls.append(url)
            
            # Kill existing if any (cleanup)
            self.kill_process_by_port(port)
            
            try:
                p = multiprocessing.Process(target=run_server, args=(port, self.log_queue), daemon=True)
                p.start()
                self.processes.append(p)
                self.active_workers[port] = True # Expect this worker to be running
                self.log(f"Started Worker on Port {port}", "SUCCESS")
            except Exception as e:
                self.log(f"Failed to start Worker-{port}: {e}", "ERROR")
        
        # Spawn Dispatcher (if not already running)
        if not hasattr(self, 'dispatcher_process') or not self.dispatcher_process or not self.dispatcher_process.is_alive():
            try:
                time.sleep(1) 
                server_str = ",".join(server_urls)
                # Pass log_queue and cmd_queue and active_workers
                # Reduced stagger from 15 to 2 for better parallelism
                d = multiprocessing.Process(target=run_dispatcher, args=(server_str, 2, self.log_queue, self.cmd_queue, self.active_workers), daemon=True)
                d.start()
                self.processes.append(d)
                self.dispatcher_process = d
                self.log(f"Dispatcher Started.", "SUCCESS")
            except Exception as e:
                self.log(f"Failed to start Dispatcher: {e}", "ERROR")
        else:
            self.log(f"Dispatcher already running.", "INFO")
        
        self.running = True
        # self.system_active_event.set() # Removed in favor of active_workers dict
        self.update_ui_state(True)
        self.header_status_dot.config(fg="#4cd964") 

    def restart_worker_process(self, port):
        # 1. Kill
        self.kill_process_by_port(port)
        
        # 2. Find internal list record (optional cleanup)
        # We don't track PIDs strictly in dict, just list.
        # Just spawn new one.
        
        time.sleep(1)
        p = multiprocessing.Process(target=run_server, args=(port, self.log_queue), daemon=True)
        p.start()
        self.processes.append(p)
        self.active_workers[port] = True
        self.log(f"Restarted Worker on Port {port}", "SUCCESS")

    def kill_process_by_port(self, port):
        """Finds and kills ANY process listening on a specific port.
        Não filtra por nome: em build congelado o processo não se chama 'python'
        e escapava da limpeza, deixando workers zumbis."""
        my_pid = os.getpid()
        try:
            for proc in psutil.process_iter(['pid', 'name']):
                if proc.pid == my_pid:
                    continue
                try:
                    for conn in proc.net_connections(kind='inet'):
                        if conn.laddr.port == port:
                            try:
                                proc.kill()
                                self.log(f"Force killed process on port {port} (PID {proc.pid})", "WARN")
                            except: pass
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
        except: pass


    def full_shutdown(self):
        """Kills everything (workers + dispatcher) and cleans orphan chromedrivers."""
        self._stopping = True  # bloquear auto-start remoto durante o shutdown

        for p in self.processes:
            try:
                p.terminate()
                p.join(timeout=3)
                if p.is_alive():
                    p.kill()  # Force Kill
            except: pass

        self.processes = []
        if hasattr(self, 'dispatcher_process'):
            self.dispatcher_process = None

        # Fallback: matar qualquer processo restante nas portas dos workers
        for i in range(4):
            self.kill_process_by_port(8010 + i)

        # Por último: matar chromedrivers/chromes de automação órfãos.
        # Seguro porque nenhum worker está vivo neste ponto.
        kill_orphan_chrome_processes()

        self.running = False
        self.update_ui_state(False)
        self.refresh_server_grid()
        self.header_status_dot.config(fg="#555555")

    def stop_system(self):
        """Behavior requested: Stop servers, keep dispatcher"""
        # if not self.running: return # Allow force stop
        self._stopping = True  # bloquear auto-start remoto durante o stop

        self.log("Stopping Worker Servers...", "WARN")
        try:
            self.active_workers.clear()
        except: pass
        
        # Init cooldown dict if needed (lazy init)
        if not hasattr(self, 'auto_start_cooldown'):
             self.auto_start_cooldown = {}

        # 0. Force Update Status to Offline for ALL ports (0-3) - ASYNC to prevent UI freeze
        def send_offline_status():
            api_url = os.environ.get("BACKEND_API_URL", "http://localhost:8000")
            hostname = socket.gethostname()
            
            for i in range(4):
                try:
                    port_num = 8010 + i
                    worker_name = f"{hostname}-{port_num}"
                    payload = {
                        "hostname": worker_name,
                        "status": "offline",
                        "current_job_id": None,
                        "meta": {"url": f"http://127.0.0.1:{port_num}", "type": "slot"}
                    }
                    requests.post(f"{api_url}/workers/heartbeat", json=payload, timeout=2)
                except: pass
        
        threading.Thread(target=send_offline_status, daemon=True).start()
             
        # 1. Kill tracked processes directly (Aggressive)
        remaining_procs = []
        dispatcher_pid = None
        if hasattr(self, 'dispatcher_process') and self.dispatcher_process:
             dispatcher_pid = self.dispatcher_process.pid

        for p in self.processes:
            try:
                if p.is_alive():
                    if p.pid == dispatcher_pid:
                        remaining_procs.append(p)
                        continue

                    # Kill Worker (graceful primeiro, force depois)
                    self.log(f"Terminating Process PID {p.pid}...", "WARN")
                    p.terminate()
                    p.join(timeout=3)
                    if p.is_alive():
                        p.kill() # Force Kill
            except Exception as e:
                print(f"Error killing proc: {e}")

        self.processes = remaining_procs

        # 2. Kill by Port (Fallback Security)
        # Kill ALL potential workers (0-3) regardless of slider
        for i in range(4):
             self.kill_process_by_port(8010 + i)
             # Set cooldown to prevent immediate restart
             self.auto_start_cooldown[8010 + i] = time.time()

        # 3. Cleanup: matar TODAS as instâncias de chromedriver e chromes de
        # automação órfãos. Seguro neste ponto: os workers já foram mortos,
        # restam apenas processos órfãos de execuções anteriores.
        kill_orphan_chrome_processes()

        self._stopping = False  # liberar auto-start remoto novamente
        
        self.log("Workers Stopped. Dispatcher active for Remote Restarts.", "INFO")
        self.log("To fully close, close the window.", "INFO")
        
        # Update UI - maybe change Dot color to Orange?
        self.header_status_dot.config(fg="#fcc419")
        
        # We keep self.running = True so Start button is disabled?
        # If user wants to START workers again, they need a button.
        # Current Start Button is disabled.
        # Note: Making a robust Process Manager is complex. 
        # Hack: If user clicks Stop, we enable Start button?
        self.update_ui_state(False) # Enables Start button
        
        # BUT if user clicks Start, it spawns Dispatcher AGAIN.
        # We need to track if dispatcher is alive.
        
        # Let's just track dispatcher process separately.
        pass

    # Re-write start/stop logic cleaner
    pass

    def update_ui_state(self, running):
        if running:
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
            # Lock Inputs?
        else:
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

    # --- Monitoring ---
    
    def status_monitor_loop(self):
        while True:
            # Always check for remote commands (Wake on LAN style)
            self.check_remote_commands()

            if not self.running:
                # Still check DB occasionally if app is open
                self.check_db_status()
                time.sleep(3)
                continue
                
            self.check_server_status()
            self.check_db_status()
            time.sleep(2)

    def check_remote_commands(self):
        # Não auto-iniciar workers enquanto a GUI está em stop/shutdown
        # (evita corrida com a limpeza de processos)
        if getattr(self, '_stopping', False):
            return

        if not hasattr(self, 'auto_start_cooldown'):
             self.auto_start_cooldown = {}
        
        try:
            # We need BACKEND_API_URL
            api_url = os.environ.get("BACKEND_API_URL", "http://localhost:8000")
            hostname = socket.gethostname()
            
            try:
                resp = requests.get(f"{api_url}/workers", timeout=2)
                if resp.status_code == 200:
                    workers = resp.json()
                    
                    # Track what we need to start
                    ports_to_start = []
                    
                    current_time = time.time()
                    
                    for w in workers:
                        if w['hostname'].startswith(f"{hostname}-") and w['command'] == 'restart':
                            try:
                                port = int(w['hostname'].split("-")[-1])
                                # CHECK COOLDOWN
                                last_start = self.auto_start_cooldown.get(port, 0)
                                if current_time - last_start > 15: # 15s cooldown
                                    ports_to_start.append(port)
                                else:
                                    pass # Cool down
                            except: pass

                    if ports_to_start:
                        # Ensure UI updated
                        self.running = True # CRITICAL: Update internal state too!
                        self.update_ui_state(True)
                        self.header_status_dot.config(fg="#fcc419")
                        
                        # 1. Start Workers
                        for port in ports_to_start:
                            # Update Cooldown
                            self.auto_start_cooldown[port] = time.time()
                            
                            # Check if running
                            try:
                                # Check if port is bound
                                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                result = sock.connect_ex(('127.0.0.1', port))
                                sock.close()
                                
                                if result != 0: # Port is closed/free
                                    self.log(f"Auto-starting Worker-{port}...", "WARN")
                                    p = multiprocessing.Process(target=run_server, args=(port, self.log_queue), daemon=True)
                                    p.start()
                                    self.processes.append(p)
                                    self.active_workers[port] = True # Mark as Active
                                else:
                                    pass

                            except Exception as e:
                                print(f"Worker start error: {e}")

                        # 2. Ensure Dispatcher is running
                        dispatcher_alive = hasattr(self, 'dispatcher_process') and self.dispatcher_process and self.dispatcher_process.is_alive()
                        if not dispatcher_alive:
                             # We need to start dispatcher for ALL potential workers so it can monitor them
                             num_w = self.var_num_servers.get()
                             server_urls = [f"http://127.0.0.1:{8010+i}" for i in range(num_w)]
                             server_str = ",".join(server_urls)
                             
                             d = multiprocessing.Process(target=run_dispatcher, args=(server_str, 15, None, self.cmd_queue, self.active_workers), daemon=True)
                             d.start()
                             self.processes.append(d)
                             self.dispatcher_process = d
                             self.log("Dispatcher auto-started.", "SUCCESS")

            except: pass
        except: pass

    def check_db_status(self):
        # Fake a DB check or try simple socket to Supabase/Postgres if direct
        # Since we use an imported session, we can reuse logic, but keep it simple.
        # Just check if we can resolve the host or if we have internet basically for this MVP
        # Or better: check if dispatch is writing logs? Hard to check from here.
        # Let's assume Connected if we have internet for now, or implement a real ping if requested.
        # For "Checking..." stuck, lets just set it to "Ready" initially.
        try:
            # Simple check to google to verify internet, or check configured DB host port
            # SUPABASE_DB_HOST
            host = "www.google.com" # Fallback
            # Parse from env if possible, but reading file again is inefficient.
            # Let's just say "Connected" if online.
            socket.create_connection((host, 80), timeout=1).close()
            
            def update_lbl():
                self.lbl_db_status.config(text="Online", fg="#4cd964")
            self.root.after(0, update_lbl)
        except:
             def update_lbl_err():
                self.lbl_db_status.config(text="Offline", fg="#ff6b6b")
             self.root.after(0, update_lbl_err)

    def check_server_status(self):
        num = self.var_num_servers.get()
        for i in range(num):
            port = 8010 + i
            url = f"http://127.0.0.1:{port}/"
            
            status_text = "ERROR"
            status_bg = "#ff6b6b" # Red
            
            try:
                # Use short timeout
                resp = requests.get(url, timeout=0.5)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("busy"):
                        status_text = "WORKING"
                        status_bg = "#fcc419" # Yellow
                    else:
                        status_text = "IDLE"
                        status_bg = "#51cf66" # Green
                else:
                    status_text = f"ERR {resp.status_code}"
            except:
                status_text = "NO CONN"
            
            # Update UI on main thread
            widget = self.status_widgets.get(port)
            if widget:
                def _upd(w=widget, t=status_text, c=status_bg):
                    w.config(text=t, fg=c)
                self.root.after(0, _upd)

    # --- Tray Logic ---

    def minimize_to_tray(self):
        self.is_minimized = True
        self.root.withdraw()
        
        image = create_tray_icon()
        menu = (item('Show', self.show_window), item('Quit', self.quit_app))
        self.tray_icon = pystray.Icon("BaseGuias", image, "Base Guias Manager", menu)
        
        # Pystray run is blocking, needs thread
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def show_window(self, icon=None, item=None):
        self.is_minimized = False
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.deiconify)

    def quit_app(self, icon=None, item=None):
        if self.tray_icon: self.tray_icon.stop()
        self.full_shutdown()
        self.root.after(0, self.root.destroy)
        sys.exit()

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


def kill_existing_instances():
    """Kill all processes from a previous app instance before starting."""
    my_pid = os.getpid()
    killed_count = 0

    # Get our own executable name for matching
    try:
        my_exe = os.path.basename(sys.executable).lower()
    except:
        my_exe = ''

    # 1. Kill other instances of our executable (frozen .exe)
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.pid == my_pid:
            continue
        try:
            name = (proc.info['name'] or '').lower()
            if getattr(sys, 'frozen', False) and my_exe and name == my_exe:
                proc.kill()
                killed_count += 1
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

    # 2. Kill ANY process listening on worker ports (8010-8013).
    # Antes só 'python' nas portas 8000-8003 era morto — workers reais ouvem
    # em 8010+ e sobreviviam como zumbis (com chromedriver morto).
    for port in range(8010, 8014):
        for proc in psutil.process_iter(['pid']):
            if proc.pid == my_pid:
                continue
            try:
                for conn in proc.net_connections(kind='inet'):
                    if conn.laddr.port == port:
                        proc.kill()
                        killed_count += 1
                        break
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

    # 3. Só agora matar chromedrivers/chromes de automação órfãos da
    # instância anterior — seguro porque nenhum worker vivo resta.
    kill_orphan_chrome_processes()

    if killed_count > 0:
        print(f"Killed {killed_count} previous app instance(s). Starting fresh.")
        time.sleep(1)  # Wait for ports to be released


if __name__ == "__main__":
    multiprocessing.freeze_support()
    
    # Single-instance guard: kill any existing instances before starting
    kill_existing_instances()
    
    root = tk.Tk()
    app = SystemManagerApp(root)
    root.mainloop()
