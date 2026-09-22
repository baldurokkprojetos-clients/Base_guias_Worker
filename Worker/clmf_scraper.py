"""
CLMFScraper — Web scraper para o portal da Clínica Larissa Martins Ferreira.
Portal: https://abalarissamartinsferreira.com.br

Operações:
  OP0 — Login (com reúso de sessão)
  OP1 — Gravar Relatório Clínico Mensal (RC) e baixar PDF

Segue o mesmo padrão de interface do UnimedScraper para compatibilidade
com o dispatcher e server existentes.
"""

import os
import re
import time
import logging
import requests
from datetime import datetime
from urllib.parse import quote, urlencode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

import psutil
import urllib3

logger = logging.getLogger(__name__)

# ─── Constantes de URL ───────────────────────────────────────────────────────
BASE_URL = "https://abalarissamartinsferreira.com.br"
LOGIN_URL = f"{BASE_URL}/"
AJAX_RC_URL = f"{BASE_URL}/_ajax/RelatorioMensalIpasgo.ajax.php"
PRONTUARIO_URL = f"{BASE_URL}/prontuarios/prontuario/{{id_paciente}}"
PDF_URL_TEMPLATE = f"{BASE_URL}/uploads/PDFs/RC-{{carteirinha}}-{{nome_paciente}}-{{AbrevEsp}}.pdf"

# Seletor que indica login bem-sucedido
LOGIN_READY_TITLE = 'Ver CLIENTES'
LOGIN_READY_SELECTOR = (By.XPATH, f'//*[@title="{LOGIN_READY_TITLE}"]')

# Tempo máximo de espera (segundos)
WAIT_TIMEOUT = 30


class CLMFScraper:
    """
    Scraper para o portal abalarissamartinsferreira.com.br.

    Ciclo de vida:
        scraper = CLMFScraper(login, senha)
        scraper.start_driver()
        scraper.login()                    # OP0
        result = scraper.atualizar_rc({}) # OP1
        scraper.close_driver()
    """

    def __init__(self, login: str = None, senha: str = None, headless: bool = True):
        self.login_user = login or os.getenv("CLMF_LOGIN", "")
        self.senha = senha or os.getenv("CLMF_PASSWORD", "")
        self.headless = headless
        self.driver: webdriver.Chrome | None = None
        self._owned_pids: list[int] = []
        self._session_cookies: dict = {}

    # ─── Driver lifecycle ───────────────────────────────────────────────────

    def start_driver(self):
        """Inicializa o Chrome WebDriver."""
        import urllib3

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-features=PasswordLeakDetection")
        options.add_argument("--incognito")
        options.add_argument("--disable-extensions")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # Desativar pop-up "Mude sua senha" do gerenciador de senhas do Google
        options.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False
        })

        options.add_argument("--window-size=1280,900")

        try:
            service = Service()
            self.driver = webdriver.Chrome(service=service, options=options, keep_alive=False)

            # ── Fail-fast: zerar retries internos do Selenium ────────────────
            # Sem isso, quando ChromeDriver morre, urllib3 retenta 3x (~30s perdidos)
            self.driver.command_executor._conn = urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=10, read=120),
                retries=urllib3.util.Retry(total=0),
            )
            self.driver.set_page_load_timeout(120)
            self.driver.set_script_timeout(60)
            self._track_driver_processes()

            logger.info("CLMFScraper: driver iniciado.")
        except Exception as e:
            logger.error(f"CLMFScraper: falha ao iniciar driver: {e}")
            raise

    def _track_driver_processes(self):
        """Rastreia os PIDs do chromedriver e dos chromes filhos criados por este driver."""
        self._owned_pids = []
        try:
            chromedriver_pid = self.driver.service.process.pid
            self._owned_pids.append(chromedriver_pid)
            for child in psutil.Process(chromedriver_pid).children(recursive=True):
                self._owned_pids.append(child.pid)
        except Exception:
            pass

    def kill_owned_processes(self):
        """Mata APENAS os processos (chromedriver/chrome) criados por este scraper.
        Escopo restrito aos PIDs rastreados — nunca toca em processos de outros workers."""
        for pid in self._owned_pids or []:
            try:
                proc = psutil.Process(pid)
                name = (proc.name() or "").lower()
                # Validar nome antes de matar: PID pode ter sido reciclado
                if "chromedriver" in name or "chrome" in name:
                    proc.kill()
            except psutil.NoSuchProcess:
                pass
            except Exception:
                pass
        self._owned_pids = []

    def _chromedriver_pid_alive(self) -> bool:
        """Checagem local (sem HTTP) se o processo chromedriver rastreado existe.
        Sem rastreamento (driver legado), assume vivo e deixa o health-check HTTP decidir."""
        if not self._owned_pids:
            return True
        try:
            return psutil.Process(self._owned_pids[0]).is_running()
        except psutil.NoSuchProcess:
            return False
        except Exception:
            return True

    def close_driver(self):
        """Fecha o WebDriver de forma segura."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None
        # Garantia: mata chromedriver/chrome que sobreviveram ao quit() travado
        # (escopo restrito aos PIDs deste scraper).
        self.kill_owned_processes()
        logger.info("CLMFScraper: driver encerrado.")

    def is_driver_alive(self) -> bool:
        """Verifica se o ChromeDriver ainda responde.
        Checa primeiro o PID localmente (instantâneo, sem HTTP) e só faz o
        health-check HTTP se o processo ainda existir."""
        if not self.driver:
            return False
        if not self._chromedriver_pid_alive():
            return False
        try:
            self.driver.window_handles
            return True
        except Exception:
            return False

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """True se a exceção indica que o processo chromedriver morreu
        (conexão recusada na porta local do driver)."""
        if isinstance(exc, (ConnectionError, urllib3.exceptions.MaxRetryError)):
            return True
        msg = str(exc)
        return ("Max retries exceeded" in msg
                or "WinError 10061" in msg
                or "Connection refused" in msg)

    # ─── OP0: Login ─────────────────────────────────────────────────────────

    def is_logged_in(self) -> bool:
        """Verifica se a sessão atual ainda está autenticada."""
        if not self.driver:
            return False
        try:
            wait = WebDriverWait(self.driver, 3)
            wait.until(EC.presence_of_element_located(LOGIN_READY_SELECTOR))
            return True
        except TimeoutException:
            return False

    def login(self) -> bool:
        """
        OP0 — Autentica no portal CLMF.
        Retorna True em caso de sucesso, False caso contrário.
        Reutiliza sessão se o elemento de confirmação já estiver visível.
        """
        if self.is_logged_in():
            logger.info("CLMFScraper: sessão reutilizada — login desnecessário.")
            return True

        logger.info("CLMFScraper: iniciando OP0 — Login.")
        try:
            self.driver.get(LOGIN_URL)
            wait = WebDriverWait(self.driver, WAIT_TIMEOUT)

            # Preencher e-mail
            email_field = wait.until(EC.presence_of_element_located((By.NAME, "user_email")))
            email_field.clear()
            email_field.send_keys(self.login_user)

            # Preencher senha
            password_field = self.driver.find_element(By.NAME, "user_password")
            password_field.clear()
            password_field.send_keys(self.senha)

            # Clicar em Entrar
            self.driver.find_element(By.XPATH, "/html/body/div[1]/div/form/button").click()

            # Aguardar elemento de confirmação de login
            wait.until(EC.presence_of_element_located(LOGIN_READY_SELECTOR))

            # Capturar cookies de sessão para reúso em requisições HTTP diretas
            self._session_cookies = {
                c["name"]: c["value"] for c in self.driver.get_cookies()
            }

            logger.info("CLMFScraper: OP0 — Login realizado com sucesso.")
            return True

        except TimeoutException:
            logger.error("CLMFScraper: OP0 — Timeout durante login. Verifique credenciais.")
            return False
        except Exception as e:
            logger.error(f"CLMFScraper: OP0 — Erro inesperado: {e}")
            return False

    # ─── OP1: Atualizar RC ──────────────────────────────────────────────────

    def atualizar_rc(self, params: dict) -> dict:
        """
        OP1 — Grava o Relatório Clínico Mensal e faz download do PDF.

        Parâmetros esperados em `params`:
            id_paciente      (int|str)
            id_profissional  (int|str)
            AbrevEsp         (str) — ex: "PSI", "FONO", "TO"
            id_especialidade (int|str)
            data_RC          (str) — formato "dd/MM/yyyy"
            caminho_pasta    (str) — pasta local de destino
            nome_padrao      (str) — nome final do arquivo PDF
            carteira         (str) — número da carteirinha

        Retorna dict com status e mensagem.
        """
        logger.info(
            f"CLMFScraper: OP1 — AtualizarRC | "
            f"id_paciente={params.get('id_paciente')} | "
            f"AbrevEsp={params.get('AbrevEsp')} | "
            f"data_RC={params.get('data_RC')}"
        )

        # Garantir login ativo
        if not self.is_logged_in():
            ok = self.login()
            if not ok:
                return {"status": "error", "message": "Falha no login antes de OP1"}

        id_paciente = str(params["id_paciente"])
        id_profissional = str(params["id_profissional"])
        id_especialidade = str(params["id_especialidade"])
        abrev_esp = str(params["AbrevEsp"]).strip()
        data_rc_input = str(params["data_RC"]).strip()       # dd/MM/yyyy
        caminho_pasta = str(params["caminho_pasta"]).strip()
        nome_padrao = str(params["nome_padrao"]).strip()

        # Converter data_RC de dd/MM/yyyy → yyyy-MM-dd
        try:
            data_rc_iso = datetime.strptime(data_rc_input, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            logger.error(f"CLMFScraper: formato de data inválido: {data_rc_input}")
            return {"status": "error", "message": f"Formato de data inválido: {data_rc_input}"}

        # PASSO 1 — Navegar ao prontuário do paciente
        prontuario_url = PRONTUARIO_URL.format(id_paciente=id_paciente)
        logger.info(f"  → Navegando para {prontuario_url}")
        self.driver.get(prontuario_url)

        wait = WebDriverWait(self.driver, WAIT_TIMEOUT)

        try:
            # PASSO 2 — Extrair nome exato do paciente no portal (para montar a URL do PDF corretamente)
            nome_paciente_raw = ""
            try:
                # Tenta extrair da label de Nome (mais seguro que os crumbs)
                nome_el = self.driver.find_element(By.XPATH, "//span[contains(text(), 'Nome:')]/following-sibling::strong")
                nome_paciente_raw = nome_el.text.strip()
            except NoSuchElementException:
                # Tenta pelo 5º crumb se a label falhar
                try:
                    crumbs = self.driver.find_elements(By.CSS_SELECTOR, "span.crumb")
                    # O nome costuma ser o texto solto após o último crumb '/' no header
                    # Usamos regex na page_source como fallback absoluto
                    match = re.search(r'<span class="legend">Nome:</span>\s*<strong>(.*?)</strong>', self.driver.page_source, re.IGNORECASE)
                    if match:
                        nome_paciente_raw = match.group(1).strip()
                except Exception:
                    pass
            
            # Fallback seguro caso a extração DOM falhe completamente
            if not nome_paciente_raw:
                nome_paciente_raw = params.get("paciente", "").strip()
                logger.warning(f"  → Não foi possível extrair nome do DOM, usando fallback do DB: '{nome_paciente_raw}'")

            # Garantir que removemos espaços duplos e trailing spaces
            nome_paciente_raw = " ".join(nome_paciente_raw.split())
            nome_paciente_encoded = quote(nome_paciente_raw, safe="")
            logger.info(f"  → nome_paciente_portal: '{nome_paciente_raw}' → '{nome_paciente_encoded}'")

            # PASSO 2b — Extrair carteirinha limpa (remove espaços, pontos e traços)
            carteirinha_el = self.driver.find_element(By.ID, "amil_client_carteirinha")
            carteirinha_raw = carteirinha_el.get_attribute("value") or ""
            carteirinha_clean = re.sub(r"[\s.\-]", "", carteirinha_raw)
            logger.info(f"  → carteirinha_clean: '{carteirinha_clean}'")

            # PASSO 2c — Extrair justificativa
            justificativa = ""
            try:
                justificativa = self.driver.execute_script("return document.getElementById('ipasgo_justificativa_periodo_tratamento').value;")
                if not justificativa:
                    justificativa = self.driver.execute_script("return document.getElementById('ipasgo_justificativa_periodo_tratamento').textContent;")
            except Exception:
                pass
            if not justificativa:
                match = re.search(r'<textarea[^>]*id=["\']ipasgo_justificativa_periodo_tratamento["\'][^>]*>(.*?)</textarea>', self.driver.page_source, re.IGNORECASE | re.DOTALL)
                if match:
                    justificativa = match.group(1)
            justificativa = (justificativa or "").strip()

            if not justificativa:
                logger.warning("  → Campo justificativa não encontrado ou vazio no DOM.")

            # PASSO 2d — Extrair evolução
            evolucao_ipasgo = ""
            try:
                evolucao_ipasgo = self.driver.execute_script("return document.getElementById('ipasgo_evolucao_paciente').value;")
                if not evolucao_ipasgo:
                    evolucao_ipasgo = self.driver.execute_script("return document.getElementById('ipasgo_evolucao_paciente').textContent;")
            except Exception:
                pass
            if not evolucao_ipasgo:
                match = re.search(r'<textarea[^>]*id=["\']ipasgo_evolucao_paciente["\'][^>]*>(.*?)</textarea>', self.driver.page_source, re.IGNORECASE | re.DOTALL)
                if match:
                    evolucao_ipasgo = match.group(1)
            evolucao_ipasgo = (evolucao_ipasgo or "").strip()

            if not evolucao_ipasgo:
                logger.warning("  → Campo evolucao_ipasgo não encontrado ou vazio no DOM.")

            # PASSO 2e — Extrair ipasgo_id (id interno do registro de RC)
            ipasgo_id = "2"
            try:
                # Tenta várias formas que o portal pode estar renderizando o ID interno
                extracted_id = self.driver.execute_script(
                    "return document.querySelector('input[name=\"arr_relatorio[0][ipasgo_id]\"]')?.value || "
                    "document.getElementById('ipasgo_id')?.value || "
                    "document.querySelector('input[name=\"ipasgo_id\"]')?.value;"
                )
                if extracted_id:
                    ipasgo_id = str(extracted_id).strip()
            except Exception:
                pass
            logger.info(f"  → ipasgo_id extraído: {ipasgo_id}")

            # Proteção contra wipe-out
            if not justificativa and not evolucao_ipasgo:
                msg = "Atenção: Justificativa e Evolução estão VAZIAS no portal. Abortando POST para não sobrescrever com vazio."
                logger.error(f"  → {msg}")
                return {"status": "error", "message": msg}

        except TimeoutException:
            msg = f"Timeout ao carregar prontuário do paciente {id_paciente}"
            logger.error(f"  → {msg}")
            return {"status": "error", "message": msg}
        except Exception as e:
            msg = f"Erro ao extrair dados do prontuário: {e}"
            logger.error(f"  → {msg}")
            return {"status": "error", "message": msg}

        # PASSO 3 — POST AJAX para gravar RC
        logger.info(f"  → Enviando POST AJAX para gravar RC...")
        ajax_result = self._post_gravar_rc(
            id_paciente=id_paciente,
            id_profissional=id_profissional,
            id_especialidade=id_especialidade,
            justificativa=justificativa,
            evolucao_ipasgo=evolucao_ipasgo,
            data_rc_iso=data_rc_iso,
            ipasgo_id=ipasgo_id,
        )
        if ajax_result.get("status") == "error":
            return ajax_result

        # PASSO 4 — Gerar PDF fisicamente e capturar o caminho real
        logger.info(f"  → Enviando POST AJAX para gerar o Relatório PDF...")
        gerar_result = self._post_gerar_pdf(ipasgo_id=ipasgo_id)
        if gerar_result.get("status") == "error":
            return gerar_result
            
        caminho_sufix = gerar_result.get("caminho", "")
        # A API pode retornar com espaços, que precisam ser url-encoded no GET.
        # Substitui espaços por %20, mas preserva a barra e os query parameters.
        caminho_sufix_encoded = caminho_sufix.replace(" ", "%20")
        pdf_url = BASE_URL + caminho_sufix_encoded
        
        logger.info(f"  → PDF URL (Retornada pelo backend): {pdf_url}")

        # PASSO 5 — Baixar PDF
        download_result = self._download_pdf(pdf_url, caminho_pasta, nome_padrao)
        if download_result.get("status") == "error":
            return download_result

        logger.info(f"  ✓ OP1 concluída com sucesso. Arquivo: {nome_padrao}")
        return {
            "status": "success",
            "message": f"RC gravado e PDF baixado: {nome_padrao}",
            "pdf_path": download_result.get("path"),
        }

    # ─── Helpers privados ───────────────────────────────────────────────────

    def _post_gravar_rc(
        self,
        id_paciente: str,
        id_profissional: str,
        id_especialidade: str,
        justificativa: str,
        evolucao_ipasgo: str,
        data_rc_iso: str,
        ipasgo_id: str,
    ) -> dict:
        """Envia o formulário AJAX de gravação do RC via requests (usando cookies da sessão Selenium)."""
        session = requests.Session()
        for name, value in self._session_cookies.items():
            session.cookies.set(name, value)

        payload = {
            "callback": "RelatorioMensalIpasgo",
            "callback_action": "gravar",
            "arr_relatorio[0][ipasgo_id]": ipasgo_id,
            "arr_relatorio[0][ipasgo_profissional_atendimento]": id_especialidade,
            "arr_relatorio[0][ipasgo_justificativa_periodo_tratamento]": justificativa,
            "arr_relatorio[0][ipasgo_evolucao_paciente]": evolucao_ipasgo,
            "arr_relatorio[0][ipasgo_data]": data_rc_iso,
            "arr_relatorio[0][client_id]": id_paciente,
            "arr_relatorio[0][user_id]": id_profissional,
        }

        # Converte explicitamente para URL Encoded string e loga para debug conforme pedido
        encoded_payload = urlencode(payload)
        logger.info(f"  [DEBUG URL ENCODED PAYLOAD] URL: {AJAX_RC_URL}")
        logger.info(f"  [DEBUG URL ENCODED PAYLOAD] DATA: {encoded_payload}")

        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
            resp = session.post(AJAX_RC_URL, data=encoded_payload, headers=headers, timeout=30)
            resp.raise_for_status()
            logger.info(f"  → AJAX RC response status: {resp.status_code}")
            return {"status": "success", "http_status": resp.status_code}
        except requests.RequestException as e:
            msg = f"Falha no POST AJAX do RC: {e}"
            logger.error(f"  → {msg}")
            return {"status": "error", "message": msg}

    def _post_gerar_pdf(self, ipasgo_id: str) -> dict:
        """Envia requisição AJAX para compilar e gerar o PDF atualizado no servidor."""
        session = requests.Session()
        for name, value in self._session_cookies.items():
            session.cookies.set(name, value)

        payload = {
            "callback": "RelatorioMensalIpasgo",
            "callback_action": "gerarRelatorio",
            "ipasgo_id": ipasgo_id
        }
        
        encoded_payload = urlencode(payload)
        logger.info(f"  [DEBUG URL ENCODED GERAR PDF] DATA: {encoded_payload}")

        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
            resp = session.post(AJAX_RC_URL, data=encoded_payload, headers=headers, timeout=30)
            resp.raise_for_status()
            
            resp_text = resp.text
            logger.info(f"  → AJAX Gerar PDF response: {resp_text}")
            
            data = resp.json()
            if "caminho" in data:
                return {"status": "success", "caminho": data["caminho"]}
            else:
                return {"status": "error", "message": f"Resposta sem 'caminho': {resp_text}"}
        except Exception as e:
            msg = f"Falha ao gerar o PDF no servidor: {e}"
            logger.error(f"  → {msg}")
            return {"status": "error", "message": msg}

    def _download_pdf(self, url: str, caminho_pasta: str, nome_padrao: str) -> dict:
        """Baixa o PDF e salva no caminho_pasta com o nome_padrao."""
        os.makedirs(caminho_pasta, exist_ok=True)
        destino = os.path.join(caminho_pasta, nome_padrao)

        # Apagar arquivo existente (mesmo nome) antes de baixar
        if os.path.exists(destino):
            os.remove(destino)
            logger.info(f"  → Arquivo existente removido: {destino}")

        session = requests.Session()
        for name, value in self._session_cookies.items():
            session.cookies.set(name, value)

        try:
            resp = session.get(url, timeout=60, stream=True)
            resp.raise_for_status()

            with open(destino, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"  → PDF salvo em: {destino}")
            return {"status": "success", "path": destino}

        except requests.RequestException as e:
            msg = f"Falha ao baixar PDF ({url}): {e}"
            logger.error(f"  → {msg}")
            return {"status": "error", "message": msg}
