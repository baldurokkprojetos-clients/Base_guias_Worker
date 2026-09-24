"""
CLMFScraper — Web scraper para o portal da Clínica Larissa Martins Ferreira.
Portal: https://abalarissamartinsferreira.com.br

Operações:
  OP0 — Login (com reúso de sessão)
  OP1 — Gravar Relatório Clínico Mensal (RC) e baixar PDF
  OP2 — ImprimirEvolucao: concilia horários do job com atendimentos do portal
        (aba_atividade_single, fallback evolution_single), grava data/horas via
        AJAX e baixa o PDF para C:\\EVOLUCOES. Ver docs/OP2_IMPRIMIR_EVOLUCAO.md.

Segue o mesmo padrão de interface do UnimedScraper para compatibilidade
com o dispatcher e server existentes.
"""

import os
import re
import time
import logging
import requests
from datetime import datetime
from html import unescape
from urllib.parse import quote, urlencode, urljoin

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

# ─── Constantes OP2: ImprimirEvolucao (rotina clmf_imprimir_evolucao) ────────
ANSWERS_TREATMENT_URL_TEMPLATE = f"{BASE_URL}/reports/answers_treatment/{{id_paciente}}"
EVOLUTION_PACIENT_URL_TEMPLATE = f"{BASE_URL}/evolution/pacient/{{id_paciente}}"
AJAX_ABA_URL = f"{BASE_URL}/_ajax/Aba.ajax.php"
AJAX_EVOLUTION_URL = f"{BASE_URL}/_ajax/Evolution.ajax.php"
AJAX_REPORTS_PDF_URL = f"{BASE_URL}/_ajax/ReportsPDF.ajax.php"
EVOLUCOES_DIR = r"C:\EVOLUCOES"

# Timeouts longos: o modal do portal é lento (spec) — a resposta HTTP é da
# própria requisição, isolando o processamento de outras requisições.
FILTER_TIMEOUT = 120        # POST do filtro de atendimentos
AJAX_TIMEOUT = 60           # gravarNovaData / gravarHorario
PDF_TIMEOUT = 120           # geração do PDF
PDF_DOWNLOAD_TIMEOUT = 180  # download do PDF
OP2_RETRIES = 3
OP2_BACKOFF_S = 5

FLUXO_ABA = "aba"
FLUXO_EVOLUTION = "evolution"
ROW_CLASS_BY_FLUXO = {FLUXO_ABA: "aba_atividade_single", FLUXO_EVOLUTION: "evolution_single"}

CALLBACK_ABA = "Aba"
CALLBACK_EVOLUTION = "Evolution"
CALLBACK_REPORTS_PDF = "ReportsPDF"
ACTION_GRAVAR_NOVA_DATA = "gravarNovaData"
ACTION_GRAVAR_HORARIO = "gravarHorario"
ACTION_FILLED_PDF = "filled_pdf"
ACTION_FILLED_PDF_MONTH_EVOLUTION = "filled_pdf_month_evolution"

# Afinidade por paciente: o server reivindica jobs irmãos pendentes do mesmo
# idPaciente e os processa no mesmo request HTTP (reaproveita filtros carregados).
AFINIDADE_ENABLED = os.getenv("EVOLUCAO_AFINIDADE", "true").lower() != "false"
AFINIDADE_K = int(os.getenv("EVOLUCAO_AFINIDADE_K", "15"))
AFINIDADE_BUDGET_S = 240  # < timeout=300s do dispatcher

# Cache dos selects profissionais/profissões (listas globais do portal)
SELECTS_TTL_S = 1800

USER_AGENT_EVOLUCAO = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Sessão expirada: portal devolve o form de login
RE_LOGIN_FORM = re.compile(r'name=["\']user_email["\']', re.I)
RE_SELECT_NAMED = re.compile(
    r'<select[^>]*name=["\'](profissional_id|profissao_id)["\'][^>]*>(.*?)</select>', re.I | re.S
)
RE_OPTION = re.compile(r'<option[^>]*value=["\'](\d+)["\'][^>]*>(.*?)</option>', re.I | re.S)
RE_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
RE_PDF_LINK = re.compile(r"(?:https?://[^\s\"'<>]+|/[^\s\"'<>]+?\.pdf)", re.I)


def _norm_texto(s: str) -> str:
    """Normaliza nomes para comparação: strip + upper + colapsa espaços."""
    return " ".join(str(s or "").upper().split())


def _strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


def _input_value(html: str, input_id: str) -> str:
    """Extrai o value do input pelo id (ex.: novaData_1059571)."""
    m = re.search(rf'<input[^>]*id=["\']{re.escape(input_id)}["\'][^>]*>', html, re.I)
    if not m:
        return ""
    vm = re.search(r'value=["\']([^"\']*)["\']', m.group(0), re.I)
    return (vm.group(1) if vm else "").strip()


def parse_select_options(select_html: str) -> dict:
    """'<option value="1643">&raquo; NOME</option>…' → {1643: 'NOME'} (espaços colapsados)."""
    return {
        int(value): " ".join(_strip_tags(label).lstrip("»").split())
        for value, label in RE_OPTION.findall(select_html)
    }


def parse_candidatos(html: str, row_class: str) -> list[dict]:
    """Extrai linhas `tr.<row_class>` do HTML de resposta do filtro.

    Cada candidato: id (tr), data dd/MM/yyyy, hora, terapia, profissional e os
    valores atuais de novaData/horaInicial/horaFinal (campos vazios = prioridade).
    """
    row_re = re.compile(
        rf'<tr[^>]*class=["\'][^"\']*{re.escape(row_class)}[^"\']*["\'][^>]*id=["\'](\d+)["\'][^>]*>(.*?)</tr>',
        re.I | re.S,
    )
    candidatos = []
    for m in row_re.finditer(html):
        row_id, body = int(m.group(1)), m.group(2)
        tds = RE_TD.findall(body)
        if len(tds) < 8:
            continue
        data_txt = _strip_tags(tds[1])
        try:
            data_iso = datetime.strptime(data_txt, "%d/%m/%Y").date().isoformat()
        except ValueError:
            data_iso = ""
        nova_data = _input_value(body, f"novaData_{row_id}")
        hora_ini = _input_value(body, f"horaInicial_{row_id}")
        hora_fim = _input_value(body, f"horaFinal_{row_id}")
        candidatos.append({
            "id": row_id,
            "data": data_txt,
            "data_iso": data_iso,
            "hora": _strip_tags(tds[2]),
            "novaData": nova_data,
            "horaInicial": hora_ini,
            "horaFinal": hora_fim,
            "vazio": not (nova_data or hora_ini or hora_fim),
            "terapia": _strip_tags(tds[6]),
            "profissional": _strip_tags(tds[7]),
            "terapia_norm": _norm_texto(_strip_tags(tds[6])),
            "profissional_norm": _norm_texto(_strip_tags(tds[7])),
        })
    return candidatos


def selecionar_candidatos(candidatos: list[dict], usados: set, data_iso: str,
                          terapia_norm: str, prof_norm: str | None, n: int) -> list[dict]:
    """Seleção em camadas (função pura, sem I/O):

    1. data == dataExec E terapia == terapia E profissional == canônico(ID_prof)
    2. data == dataExec E terapia == terapia (outros profissionais)
    3. data == dataExec (outras terapias)
    4. quaisquer outras datas
    Dentro de cada camada: primeiro os de campos vazios (novaData/horaInicial/
    horaFinal), depois os demais; ordem estável por id.
    """
    def sort_key(c):
        mesma_data = c["data_iso"] == data_iso
        mesma_terapia = c["terapia_norm"] == terapia_norm
        mesmo_prof = prof_norm is not None and c["profissional_norm"] == prof_norm
        if mesma_data and mesma_terapia and mesmo_prof:
            tier = 0
        elif mesma_data and mesma_terapia:
            tier = 1
        elif mesma_data:
            tier = 2
        else:
            tier = 3
        return (tier, 0 if c["vazio"] else 1, c["id"])

    pool = [c for c in candidatos if c["id"] not in usados]
    pool.sort(key=sort_key)
    return pool[:n]


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

        # OP2 — sessão HTTP persistente e caches do portal (selects/candidatos)
        self._evolucao_sessao: requests.Session | None = None
        self._selects_cache: dict | None = None
        self._cand_cache: dict = {}

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

    # ─── OP2: ImprimirEvolucao ────────────────────────────────────────────────
    #
    # Fluxo (rotina 'clmf_imprimir_evolucao', params ver plano OP2 §3.1):
    #   login OP0 → GET modal (selects, cache TTL) → POST filtro (±30d, cache por
    #   paciente com união de janelas) → por item[guia/ID_prof](horas): seleção em
    #   camadas + reserva atômica (evolucao_claims) → gravarNovaData/gravarHorario
    #   I/F → filled_pdf → download C:\EVOLUCOES\{guia}-{dd-mm-aaaa}-{nome}-{profissao_id}.pdf
    #   Fallback: /evolution/pacient (evolution_single, Evolution.ajax.php,
    #   filled_pdf_month_evolution). Status por item em evolucao_itens (self-persist).

    def imprimir_evolucao(self, params: dict) -> dict:
        """
        OP2 — Concilia horários do job com atendimentos do portal e baixa os PDFs.

        Parâmetros esperados em `params` (nível raiz):
            job_id       (int)    — injetado pelo server
            server_url   (str)    — URL deste server (afinidade; opcional)
            idPaciente   (int)    — client_id no portal
            nomePaciente (str)
            dataExec     (str)    — "YYYY-MM-DD"
            dataInicial  (str)    — dataExec − 30d (pré-calculado no upload)
            dataFinal    (str)    — dataExec + 30d
            login/senha  (str)    — override opcional das credenciais
            itens        (list)   — [{guia, ID_prof, terapia, horas:["0700",…]}]

        Retorna dict com resumo por item; status por item é persistido em
        evolucao_itens (fonte da verdade do export).
        """
        logger.info(
            f"CLMFScraper: OP2 — ImprimirEvolucao | job={params.get('job_id')} | "
            f"paciente={params.get('idPaciente')} | dataExec={params.get('dataExec')} | "
            f"itens={len(params.get('itens') or [])}"
        )

        # Override de credenciais do job (D15: params → env)
        if params.get("login"):
            self.login_user = str(params["login"])
        if params.get("senha"):
            self.senha = str(params["senha"])

        if not self.is_logged_in():
            if not self.login():
                return {"status": "error", "message": "Falha no login antes de OP2", "op": "clmf_imprimir_evolucao"}

        from database import SessionLocal

        db = SessionLocal()
        try:
            resultado = self._processar_job_evolucao(db, params)
            resultado["afinidade"] = {"jobs_irmaos_processados": 0, "liberados": 0}
            if AFINIDADE_ENABLED:
                try:
                    resultado["afinidade"] = self._processar_irmaos_afinidade(db, params)
                except Exception as e:
                    logger.warning(f"  [OP2] Afinidade falhou (seguindo sem ela): {e}")
            resultado["self_persisted"] = True
            return resultado
        finally:
            db.close()

    # ─── OP2: HTTP com retry ─────────────────────────────────────────────────

    def _evolucao_http(self, force: bool = False) -> requests.Session:
        """Sessão requests persistente (keep-alive) com cookies da sessão Selenium."""
        if force or self._evolucao_sessao is None or not self._evolucao_sessao.cookies:
            session = requests.Session()
            for name, value in self._session_cookies.items():
                session.cookies.set(name, value)
            session.headers.update({
                "User-Agent": USER_AGENT_EVOLUCAO,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
            })
            self._evolucao_sessao = session
        return self._evolucao_sessao

    def _request_com_retry(self, method: str, url: str, data=None, timeout: int = AJAX_TIMEOUT) -> requests.Response:
        """Request com retry/backoff e re-login automático em sessão expirada."""
        last_exc: Exception | None = None
        for attempt in range(OP2_RETRIES):
            try:
                session = self._evolucao_http()
                resp = session.request(method, url, data=data, timeout=timeout)
                texto = resp.text or ""
                if RE_LOGIN_FORM.search(texto):
                    logger.warning(f"  [OP2] Sessão expirada em {url} — re-executando OP0 login.")
                    self._evolucao_sessao = None
                    if not self.login():
                        raise RuntimeError("Re-login (OP0) falhou durante OP2")
                    self._evolucao_sessao = None  # reconstrói com os novos cookies
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_exc = e
                logger.warning(f"  [OP2] {method} {url} tentativa {attempt + 1}/{OP2_RETRIES} falhou: {e}")
                if attempt < OP2_RETRIES - 1:
                    time.sleep(OP2_BACKOFF_S)
        raise RuntimeError(f"Falha HTTP {method} {url} após {OP2_RETRIES} tentativas: {last_exc}")

    # ─── OP2: etapa 0 (selects) e filtro de candidatos ───────────────────────

    def _carregar_selects(self, id_paciente: int) -> dict:
        """Extrai os selects profissional_id (id→nome) e profissao_id (terapia→id).

        As listas são globais do portal — cache com TTL evita reabrir o modal
        lento a cada job.
        """
        if self._selects_cache and (time.time() - self._selects_cache["ts"]) < SELECTS_TTL_S:
            return self._selects_cache

        url = ANSWERS_TREATMENT_URL_TEMPLATE.format(id_paciente=id_paciente)
        html = self._request_com_retry("GET", url, timeout=FILTER_TIMEOUT).text

        profissionais, profissoes = {}, {}
        for m in RE_SELECT_NAMED.finditer(html):
            nome_select, corpo = m.group(1).lower(), m.group(2)
            if nome_select == "profissional_id":
                profissionais = parse_select_options(corpo)
            else:
                profissoes = {_norm_texto(nome): int(v) for v, nome in parse_select_options(corpo).items()}

        if not profissionais:
            raise RuntimeError("Select profissional_id não encontrado em answers_treatment "
                               "(layout mudou ou sessão inválida) — ver premissa (b) do plano OP2")

        self._selects_cache = {
            "profissionais": profissionais,   # {1643: 'DANIELLA SANDES PEREIRA', …}
            "profissoes": profissoes,         # {'PSICOLOGIA': 2, …}
            "ts": time.time(),
        }
        logger.info(f"  [OP2] Selects carregados: {len(profissionais)} profissionais, "
                    f"{len(profissoes)} profissões.")
        return self._selects_cache

    def _candidatos_fluxo(self, fluxo: str, id_paciente: int, data_inicial: str, data_final: str) -> list[dict]:
        """POST do filtro por fluxo, com cache por paciente (união de janelas).

        Com afinidade, jobs irmãos do mesmo paciente reutilizam a listagem — a
        janela cobrida é ampliada por união (fetch único pelo par mais largo).
        """
        url = (ANSWERS_TREATMENT_URL_TEMPLATE if fluxo == FLUXO_ABA else EVOLUTION_PACIENT_URL_TEMPLATE
               ).format(id_paciente=id_paciente)

        cache = self._cand_cache.get(fluxo)
        if cache and cache["pid"] == id_paciente:
            ini_c, fim_c = cache["win"]
            if ini_c <= data_inicial and data_final <= fim_c:
                return cache["lista"]
            data_inicial = min(data_inicial, ini_c)
            data_final = max(data_final, fim_c)

        payload = {
            "profissional_id": "",
            "profissao_id": "",
            "data_inicial": data_inicial,
            "data_final": data_final,
            "public": "",
        }
        logger.info(f"  [OP2] Filtro {fluxo} paciente {id_paciente}: {data_inicial} → {data_final}")
        html = self._request_com_retry("POST", url, data=payload, timeout=FILTER_TIMEOUT).text
        lista = parse_candidatos(html, ROW_CLASS_BY_FLUXO[fluxo])
        logger.info(f"  [OP2] Filtro {fluxo}: {len(lista)} candidatos.")
        self._cand_cache[fluxo] = {"pid": id_paciente, "win": (data_inicial, data_final), "lista": lista}
        return lista

    # ─── OP2: reserva atômica de candidatos (evolucao_claims) ────────────────

    def _reservar_claim(self, db, job_id: int, fluxo: str, portal_item_id: int) -> bool:
        """True se reservado (ou já pertence a este job — retry)."""
        from sqlalchemy.exc import IntegrityError
        from models import EvolucaoClaim

        try:
            db.add(EvolucaoClaim(fluxo=fluxo, portal_item_id=portal_item_id, job_id=job_id))
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            existente = db.query(EvolucaoClaim).filter_by(
                fluxo=fluxo, portal_item_id=portal_item_id).first()
            return bool(existente and existente.job_id == job_id)

    def _liberar_claims(self, db, job_id: int, fluxo: str, portal_item_ids: list[int]):
        """Libera reservas parciais próprias (item não fechou N candidatos)."""
        from models import EvolucaoClaim
        if not portal_item_ids:
            return
        db.query(EvolucaoClaim).filter(
            EvolucaoClaim.job_id == job_id,
            EvolucaoClaim.fluxo == fluxo,
            EvolucaoClaim.portal_item_id.in_(portal_item_ids),
        ).delete(synchronize_session=False)
        db.commit()

    def _selecionar_e_reservar(self, db, job_id: int, fluxo: str, candidatos: list[dict],
                               usados: set, data_iso: str, terapia_norm: str,
                               prof_norm: str | None, n: int) -> list[dict]:
        """Seleção em camadas + reserva atômica. Só retorna com N completos;
        reservas parciais são liberadas (nenhuma gravação é enviada sem fechar N)."""
        reservados: list[dict] = []
        local_usados = set(usados)
        while len(reservados) < n:
            falta = n - len(reservados)
            sugeridos = selecionar_candidatos(candidatos, local_usados, data_iso,
                                              terapia_norm, prof_norm, falta)
            if not sugeridos:
                break
            progresso = False
            for c in sugeridos:
                if self._reservar_claim(db, job_id, fluxo, c["id"]):
                    reservados.append(c)
                    progresso = True
                local_usados.add(c["id"])
            if not progresso:
                break
        if len(reservados) < n:
            self._liberar_claims(db, job_id, fluxo, [c["id"] for c in reservados])
            return []
        usados.update(c["id"] for c in reservados)
        return reservados

    # ─── OP2: gravações e PDF ────────────────────────────────────────────────

    @staticmethod
    def _hora_hhmm_para_request(hora_hhmm: str) -> tuple[str, str]:
        """"0700" → ("07:00", "08:00")  (inicial, inicial+1h). Edge 23:00 → 00:00 WARN."""
        hh, mm = int(hora_hhmm[:2]), int(hora_hhmm[2:])
        inicial = f"{hh:02d}:{mm:02d}"
        hf = (hh + 1) % 24
        if hh == 23:
            logger.warning("  [OP2] HoraInicial 23:00 — horaFinal virou 00:00 (edge +1h).")
        return inicial, f"{hf:02d}:{mm:02d}"

    def _gravar_candidato(self, fluxo: str, portal_id: int, data_iso: str,
                          hora_ini: str, hora_fim: str):
        """gravarNovaData + gravarHorario(I) + gravarHorario(F) para um candidato."""
        url = AJAX_ABA_URL if fluxo == FLUXO_ABA else AJAX_EVOLUTION_URL
        callback = CALLBACK_ABA if fluxo == FLUXO_ABA else CALLBACK_EVOLUTION
        requisicoes = [
            {"callback": callback, "callback_action": ACTION_GRAVAR_NOVA_DATA,
             "data": data_iso, "pergunta": portal_id},
            {"callback": callback, "callback_action": ACTION_GRAVAR_HORARIO,
             "hora": hora_ini, "pergunta": portal_id, "tipo": "I"},
            {"callback": callback, "callback_action": ACTION_GRAVAR_HORARIO,
             "hora": hora_fim, "pergunta": portal_id, "tipo": "F"},
        ]
        for payload in requisicoes:
            resp = self._request_com_retry("POST", url, data=payload, timeout=AJAX_TIMEOUT)
            logger.info(f"  [OP2] {callback}.{payload['callback_action']} pergunta={portal_id}"
                        f"{' hora=' + payload['hora'] if 'hora' in payload else ''} → {resp.text[:120]}")

    def _gerar_pdf_evolucao(self, fluxo: str, ids: list[int], client_id: int,
                            profissional_id, profissao_id) -> str | None:
        """Dispara a geração do PDF e retorna a URL absoluta (None se sem link)."""
        data = [("callback", CALLBACK_REPORTS_PDF)]
        if fluxo == FLUXO_ABA:
            data.append(("callback_action", ACTION_FILLED_PDF))
            data += [("atendimento_id[]", i) for i in ids]
            data.append(("profissao_id", profissao_id))
        else:
            data.append(("callback_action", ACTION_FILLED_PDF_MONTH_EVOLUTION))
            data += [("registro_id[]", i) for i in ids]
        data += [("client_id", client_id), ("js", "true"),
                 ("profissional_id", profissional_id), ("tipoArquivo", "pdf")]

        resp = self._request_com_retry("POST", AJAX_REPORTS_PDF_URL, data=data, timeout=PDF_TIMEOUT)
        texto = resp.text or ""

        caminho = None
        try:
            corpo = resp.json()
            if isinstance(corpo, dict):
                caminho = next((corpo[k] for k in ("caminho", "url", "path", "arquivo", "file")
                                if corpo.get(k)), None)
        except ValueError:
            pass
        if caminho:
            return urljoin(BASE_URL + "/", str(caminho))
        m = RE_PDF_LINK.search(texto)
        if m:
            return urljoin(BASE_URL + "/", m.group(0))
        logger.warning(f"  [OP2] filled_pdf sem link na resposta: {texto[:200]}")
        return None

    def _download_pdf_evolucao(self, pdf_url: str, nome_arquivo: str) -> str:
        """Baixa o PDF para EVOLUCOES_DIR e retorna o caminho salvo."""
        os.makedirs(EVOLUCOES_DIR, exist_ok=True)
        destino = os.path.join(EVOLUCOES_DIR, nome_arquivo)
        if os.path.exists(destino):
            os.remove(destino)

        resp = self._evolucao_http().get(pdf_url, timeout=PDF_DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(destino, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"  [OP2] PDF salvo: {destino}")
        return destino

    # ─── OP2: persistência (evolucao_itens) ──────────────────────────────────

    def _buscar_itens_job(self, db, job_id: int) -> dict:
        """Itens do job indexados por (guia, data_exec, profissional_id, hora_inicial)."""
        from models import EvolucaoItem
        rows = db.query(EvolucaoItem).filter(EvolucaoItem.job_id == job_id).all()
        return {(r.guia, str(r.data_exec), r.profissional_id, r.hora_inicial): r for r in rows}

    def _persistir_item_status(self, db, job_id: int, id_paciente: int, nome_paciente: str,
                               guia: str, data_exec: str, profissional_id: int, terapia: str,
                               hora: str, status: str, motivo: str | None = None,
                               profissao_id: int | None = None, ids_conciliados=None,
                               pdf_path: str | None = None, lote: str | None = None):
        from models import EvolucaoItem
        from datetime import date as _date

        data_exec_date = (data_exec if isinstance(data_exec, _date)
                          else _date.fromisoformat(str(data_exec)[:10]))
        row = db.query(EvolucaoItem).filter(
            EvolucaoItem.job_id == job_id,
            EvolucaoItem.guia == guia,
            EvolucaoItem.data_exec == data_exec_date,
            EvolucaoItem.profissional_id == profissional_id,
            EvolucaoItem.hora_inicial == hora,
        ).first()
        if row:
            row.status = status
            row.motivo = motivo
            row.profissao_id = profissao_id if profissao_id is not None else row.profissao_id
            row.ids_conciliados = ids_conciliados if ids_conciliados is not None else row.ids_conciliados
            row.pdf_path = pdf_path if pdf_path is not None else row.pdf_path
            row.updated_at = datetime.utcnow()
        else:
            db.add(EvolucaoItem(
                job_id=job_id, lote=lote, id_paciente=id_paciente,
                nome_paciente=nome_paciente, guia=guia, data_exec=data_exec_date,
                profissional_id=profissional_id, terapia=terapia, hora_inicial=hora,
                status=status, motivo=motivo, profissao_id=profissao_id,
                ids_conciliados=ids_conciliados, pdf_path=pdf_path,
            ))
        db.commit()

    # ─── OP2: processamento de um job (principal ou irmão de afinidade) ──────

    def _processar_job_evolucao(self, db, params: dict) -> dict:
        job_id = int(params.get("job_id") or 0)
        id_paciente = int(params["idPaciente"])
        nome_paciente = str(params.get("nomePaciente") or "")
        data_exec = str(params["dataExec"])[:10]           # "YYYY-MM-DD"
        data_ini = str(params.get("dataInicial") or data_exec)[:10]
        data_fim = str(params.get("dataFinal") or data_exec)[:10]
        itens = params.get("itens") or []

        resumo = {"itens": 0, "ok": 0, "pendente": 0, "erro": 0}
        detalhe: list[dict] = []

        if not itens:
            return {"status": "success", "op": "clmf_imprimir_evolucao", "job_id": job_id,
                    "idPaciente": id_paciente, "dataExec": data_exec,
                    "resumo": resumo, "itens": detalhe}

        selects = self._carregar_selects(id_paciente)
        profissionais: dict = selects["profissionais"]
        profissoes: dict = selects["profissoes"]

        itens_db = self._buscar_itens_job(db, job_id)
        cand_evo: list[dict] | None = None   # fallback lazy (1 fetch por job)
        usados: set[int] = set()             # ids consumidos neste job (claims cobrem cross-job)

        for item in itens:
            guia = str(item["guia"])
            id_prof = int(item["ID_prof"])
            terapia = str(item.get("terapia") or "")
            horas = sorted(item.get("horas") or [])
            n = len(horas)
            resumo["itens"] += n

            # Retomada de retry: pula item já concluído
            todas_ok = all(
                (r := itens_db.get((guia, data_exec, id_prof, h))) is not None and r.status == "OK"
                for h in horas
            )
            if todas_ok:
                resumo["ok"] += n
                continue

            primeira_row = itens_db.get((guia, data_exec, id_prof, horas[0])) if horas else None
            lote_row = primeira_row.lote if primeira_row else None

            def registrar(status, motivo=None, prof_id=None, ids=None, pdf=None):
                for h in horas:
                    self._persistir_item_status(
                        db, job_id, id_paciente, nome_paciente, guia, data_exec,
                        id_prof, terapia, h, status, motivo=motivo,
                        profissao_id=prof_id, ids_conciliados=ids, pdf_path=pdf,
                        lote=lote_row,
                    )

            try:
                terapia_norm = _norm_texto(terapia)
                profissao_id = profissoes.get(terapia_norm)
                if profissao_id is None:
                    motivo = f"Terapia '{terapia}' sem profissao_id no select do portal"
                    registrar("ERRO", motivo=motivo)
                    resumo["erro"] += n
                    detalhe.append({"guia": guia, "ID_prof": id_prof, "status": "ERRO", "motivo": motivo})
                    continue

                nome_canonico = profissionais.get(id_prof)
                prof_norm = _norm_texto(nome_canonico) if nome_canonico else None
                if nome_canonico is None:
                    logger.warning(f"  [OP2] ID_prof {id_prof} ausente no select — "
                                   f"camada 1 (profissional) ficará sem match para guia {guia}.")

                # Fluxo primário (aba) → seleção + reserva atômica
                cand_aba = self._candidatos_fluxo(FLUXO_ABA, id_paciente, data_ini, data_fim)
                sel = self._selecionar_e_reservar(db, job_id, FLUXO_ABA, cand_aba, usados,
                                                  data_exec, terapia_norm, prof_norm, n)
                fluxo = FLUXO_ABA
                if len(sel) < n:
                    if cand_evo is None:
                        cand_evo = self._candidatos_fluxo(FLUXO_EVOLUTION, id_paciente, data_ini, data_fim)
                    sel_evo = self._selecionar_e_reservar(db, job_id, FLUXO_EVOLUTION, cand_evo,
                                                          usados, data_exec, terapia_norm,
                                                          prof_norm, n)
                    if len(sel_evo) == n:
                        sel, fluxo = sel_evo, FLUXO_EVOLUTION
                    else:
                        motivo = (f"candidatos insuficientes "
                                  f"(aba {len(sel)}/{n}, evolution {len(sel_evo)}/{n})")
                        registrar("PENDENTE", motivo=motivo, prof_id=profissao_id)
                        resumo["pendente"] += n
                        detalhe.append({"guia": guia, "ID_prof": id_prof,
                                        "status": "PENDENTE", "motivo": motivo})
                        continue

                # Gravações: cada candidato recebe uma hora do item (par ordenado)
                registrar_status = "PROCESSANDO"
                for h in horas:
                    self._persistir_item_status(db, job_id, id_paciente, nome_paciente, guia,
                                                data_exec, id_prof, terapia, h, registrar_status,
                                                profissao_id=profissao_id)
                for candidato, hora in zip(sel, horas):
                    hora_ini, hora_fim = self._hora_hhmm_para_request(hora)
                    self._gravar_candidato(fluxo, candidato["id"], data_exec, hora_ini, hora_fim)
                    # invalidação local do cache (reflete o que acabou de ser gravado)
                    candidato["novaData"] = data_exec
                    candidato["horaInicial"] = hora_ini
                    candidato["horaFinal"] = hora_fim
                    candidato["vazio"] = False

                # Geração + download do PDF — nome: {guia}-{dd-mm-aaaa}-{nomePaciente}-{profissional_id}
                pdf_url = self._gerar_pdf_evolucao(
                    fluxo, [c["id"] for c in sel], id_paciente, id_prof, profissao_id)
                if not pdf_url:
                    motivo = "PDF sem link na resposta do portal — seguindo para o próximo item"
                    registrar("ERRO", motivo=motivo, prof_id=profissao_id,
                              ids=[c["id"] for c in sel])
                    resumo["erro"] += n
                    detalhe.append({"guia": guia, "ID_prof": id_prof, "status": "ERRO", "motivo": motivo})
                    continue

                data_fmt = datetime.strptime(data_exec, "%Y-%m-%d").strftime("%d-%m-%Y")
                nome_pdf = re.sub(r'[\\/:*?"<>|]', "",
                                  f"{guia}-{data_fmt}-{nome_paciente}-{id_prof}.pdf")
                pdf_path = self._download_pdf_evolucao(pdf_url, nome_pdf)

                registrar("OK", prof_id=profissao_id, ids=[c["id"] for c in sel], pdf=pdf_path)
                resumo["ok"] += n
                detalhe.append({"guia": guia, "ID_prof": id_prof, "status": "OK",
                                "pdf_path": pdf_path, "ids_conciliados": [c["id"] for c in sel]})

            except Exception as e:
                logger.error(f"  [OP2] Erro no item guia={guia} prof={id_prof}: {e}")
                try:
                    registrar("ERRO", motivo=str(e)[:500])
                except Exception:
                    pass
                resumo["erro"] += n
                detalhe.append({"guia": guia, "ID_prof": id_prof, "status": "ERRO", "motivo": str(e)[:500]})

        return {
            "status": "success",
            "op": "clmf_imprimir_evolucao",
            "job_id": job_id,
            "idPaciente": id_paciente,
            "dataExec": data_exec,
            "resumo": resumo,
            "itens": detalhe,
        }

    # ─── OP2: afinidade por paciente (D14) ───────────────────────────────────

    def _reivindicar_irmaos(self, db, id_paciente: int, job_id_atual: int,
                            locked_by: str, limite: int) -> list:
        """Claim atômico (SKIP LOCKED) de jobs irmãos pendentes do mesmo paciente."""
        from sqlalchemy import text
        sql = text("""
            UPDATE jobs SET status = 'processing', locked_by = :lb,
                    attempts = attempts + 1, updated_at = NOW()
            WHERE id IN (
                SELECT id FROM jobs
                WHERE rotina = 'clmf_imprimir_evolucao'
                  AND status = 'pending'
                  AND (locked_by IS NULL OR locked_by = '')
                  AND params->>'idPaciente' = :pid
                  AND id <> :cur
                ORDER BY id ASC
                LIMIT :k
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, params
        """)
        rows = db.execute(sql, {"lb": locked_by, "pid": str(id_paciente),
                                "cur": job_id_atual, "k": limite}).fetchall()
        db.commit()
        if rows:
            logger.info(f"  [OP2] Afinidade: {len(rows)} job(s) irmão(s) de paciente "
                        f"{id_paciente} reivindicados por {locked_by}.")
        return rows

    def _finalizar_job(self, db, job_id: int, status: str, mensagem: str | None = None):
        """Job de irmão finalizado pelo próprio server (padrão self-persisted)."""
        from models import Log, Job as WorkerJob
        db.query(WorkerJob).filter(WorkerJob.id == job_id).update({
            "status": status, "locked_by": None, "updated_at": datetime.utcnow(),
        }, synchronize_session=False)
        if mensagem:
            db.add(Log(job_id=job_id, level="ERROR" if status == "error" else "INFO",
                       message=f"[OP2 afinidade] {mensagem}"))
        db.commit()

    def _devolver_pendentes(self, db, job_ids: list[int], locked_by: str) -> int:
        """Devolve irmãos não processados (estouro de orçamento) para a fila."""
        from sqlalchemy import text
        if not job_ids:
            return 0
        sql = text("""
            UPDATE jobs SET status = 'pending', locked_by = NULL, updated_at = NOW()
            WHERE id = ANY(:ids) AND locked_by = :lb AND status = 'processing'
        """)
        result = db.execute(sql, {"ids": job_ids, "lb": locked_by})
        db.commit()
        return result.rowcount

    def _processar_irmaos_afinidade(self, db, params: dict) -> dict:
        """Processa jobs irmãos do mesmo idPaciente reaproveitando filtros/selects.

        Orçamento AFINIDADE_BUDGET_S (< timeout 300s do dispatcher): ao estourar,
        os não processados voltam a pending e serão despachados normalmente.
        """
        id_paciente = int(params["idPaciente"])
        job_id_atual = int(params.get("job_id") or 0)
        server_url = str(params.get("server_url") or "")
        if not server_url:
            return {"jobs_irmaos_processados": 0, "liberados": 0}

        inicio = time.time()
        irmaos = self._reivindicar_irmaos(db, id_paciente, job_id_atual, server_url, AFINIDADE_K)
        processados, liberados = 0, 0

        restantes = [r.id for r in irmaos]
        for row in irmaos:
            if time.time() - inicio > AFINIDADE_BUDGET_S:
                liberados = self._devolver_pendentes(db, restantes, server_url)
                logger.info(f"  [OP2] Afinidade: orçamento {AFINIDADE_BUDGET_S}s — "
                            f"{liberados} irmão(s) devolvido(s) à fila.")
                break
            restantes.remove(row.id)
            sib_params = dict(row.params or {})
            sib_params["job_id"] = row.id
            try:
                self._processar_job_evolucao(db, sib_params)
                self._finalizar_job(db, row.id, "success")
                processados += 1
            except Exception as e:
                logger.error(f"  [OP2] Afinidade: irmão {row.id} falhou: {e}")
                self._finalizar_job(db, row.id, "error", str(e))
                processados += 1

        return {"jobs_irmaos_processados": processados, "liberados": liberados}
