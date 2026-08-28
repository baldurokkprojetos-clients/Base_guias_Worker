import os
import time
import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, UnexpectedAlertPresentException

from bs4 import BeautifulSoup
import html

from sqlalchemy.orm import Session
from database import SessionLocal
from models import Log

# Class to handle Scraping
class UnimedScraper:
    def __init__(self, db: Session = None):
        self.driver = None
        self.username = os.environ.get("SGUCARD_LOGIN", "REC2209525")
        self.password = os.environ.get("SGUCARD_PASSWORD", "Unimed2026@")
        self.headless = os.environ.get("SGUCARD_HEADLESS", "false").lower() == "true"
        # Removed long-lived self.db session to avoid stale transactions
        
    def log(self, message, level="INFO", job_id=None, carteirinha_id=None):
        print(f"[{level}] {message}")
        # Use a fresh session for each log to avoid transaction issues
        db = SessionLocal()
        try:
            log_entry = Log(
                job_id=job_id,
                carteirinha_id=carteirinha_id,
                level=level,
                message=message
            )
            db.add(log_entry)
            db.commit()
        except Exception as e:
            # If Job ID is missing (deleted from DB), this will fail. We log locally and move on.
            print(f"Failed to write log to DB: {e}")
            try: db.rollback()
            except: pass
        finally:
            db.close()

    def funccarteira(self, carteirinha):
        # carteirinha format example: 0064.8000.400948.00-5
        # Remove punctuation for processing if needed, or split by generic delimiters
        # Based on usage: x1, x2, x3, x4, x5 = self.funccarteira(carteirinha)
        # And usage in form filling:
        # cartCompleto = x1 + x2 + x3 + x4 + x5
        # cartaoParcial = x2 + x3 + x4 + x5
        
        # Make it robust to separators
        import re
        parts = re.split(r'[.-]', carteirinha)
        if len(parts) == 5:
            return parts[0], parts[1], parts[2], parts[3], parts[4]
        else:
            # Fallback for raw number string if punctuation missing?
            # Assuming strict format was enforced upstream
            return parts[0], parts[1], parts[2], parts[3], parts[4]

    def start_driver(self):
        chrome_options = Options()
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-infobars")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-setuid-sandbox")
        chrome_options.add_argument("--incognito")
        chrome_options.add_argument("--no-first-run")
        
        # Desativar pop-up "Mude sua senha" do gerenciador de senhas do Google
        chrome_options.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False
        })
        
        if self.headless:
            chrome_options.add_argument("--headless")
        
        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.maximize_window()

    def close_driver(self):
        if self.driver:
            self.driver.quit()

    def login(self):
        if not self.driver:
            self.start_driver()
            
        try:
            self.driver.get("https://sgucard.unimedgoiania.coop.br/cmagnet/Login.do")
            
            WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.ID, "passwordTemp")))
            
            login_elem = self.driver.find_element(By.ID, "login")
            passwordTemp = self.driver.find_element(By.ID, "passwordTemp")
            Button_DoLogin = self.driver.find_element(By.ID, "Button_DoLogin")
            
            login_elem.clear()
            login_elem.send_keys(self.username)
            time.sleep(1)
            passwordTemp.clear()
            passwordTemp.send_keys(self.password)
            Button_DoLogin.click()
            time.sleep(4)
            self.log("Login performed")
        except Exception as e:
            self.log(f"Login failed: {e}", level="ERROR")
            raise e
        
    # (Since I cannot easily insert methods without replacing large chunks, I will replace process_carteirinha fully)

    def process_carteirinha(self, carteirinha, job_id=None, carteirinha_db_id=None):
        # Returns list of guias dicts
        self.log(f"Processing carteirinha: {carteirinha}", job_id=job_id, carteirinha_id=carteirinha_db_id)
        
        handles = self.driver.window_handles
        if len(handles) > 1:
            self.driver.switch_to.window(handles[0])
        
        try:
            # Check if we need to login again or navigate?
            # Assuming we are at the logged in state.
            
            # Helper to check element presence
            def is_element_present(by, value):
                try:
                    self.driver.find_element(by, value)
                    return True
                except NoSuchElementException:
                    return False

            # Sort by Date (click header twice)
            self.log("Sorting table by date (Clicking header twice)...", job_id=job_id, carteirinha_id=carteirinha_db_id)
            try:
                # Based on original script: //*[@id="conteudo-submenu"]/table[2]/tbody/tr[1]/td[1]/a
                header_xpath = '//*[@id="conteudo-submenu"]/table[2]/tbody/tr[1]/td[1]/a'
                if is_element_present(By.XPATH, header_xpath):
                    # First Click
                    self.driver.find_element(By.XPATH, header_xpath).click()
                    self.log("Clicked header once. Waiting 4s...", job_id=job_id, carteirinha_id=carteirinha_db_id)
                    time.sleep(4)
                    
                    # Re-find element to avoid stale reference
                    self.driver.find_element(By.XPATH, header_xpath).click()
                    self.log("Clicked header twice. Waiting 2s...", job_id=job_id, carteirinha_id=carteirinha_db_id)
                    time.sleep(2)
                else:
                    self.log("Sort header not found. Proceeding without explicit sort.", level="WARNING", job_id=job_id, carteirinha_id=carteirinha_db_id)
            except Exception as sort_e:
                self.log(f"Error while sorting table: {sort_e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)

            self.log("Starting scraping loop...", job_id=job_id, carteirinha_id=carteirinha_db_id)
            try:
                # Update XPath or try multiple?
                # User says: "não foi clicado no elemento new_exame"
                WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.XPATH, '//*[@id="cadastro_biometria"]/div/div[2]/span')))
                new_exame = self.driver.find_element(By.XPATH, '//*[@id="cadastro_biometria"]/div/div[2]/span')
                new_exame.click()
                self.log("Clicked 'new_exame'", job_id=job_id, carteirinha_id=carteirinha_db_id)
            except Exception as e:
                self.log(f"Failed to find/click 'new_exame': {str(e)}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                raise e

            time.sleep(3)
            
            if len(self.driver.window_handles) > 1:
                self.driver.switch_to.window(self.driver.window_handles[-1])
                self.driver.maximize_window()
                self.log("Switched to popup window", job_id=job_id, carteirinha_id=carteirinha_db_id)
            else:
                self.log("Popup window did not open!", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                raise Exception("Popup window not found")
            
            x1, x2, x3, x4, x5 = self.funccarteira(carteirinha)
            cartCompleto = x1 + x2 + x3 + x4 + x5      
            cartaoParcial = x2 + x3 + x4 + x5
            
            self.log("Filling form...", job_id=job_id, carteirinha_id=carteirinha_db_id)
            # Form Filling
            element7 = self.driver.find_element(By.NAME, 'nr_via')
            element6 = self.driver.find_element(By.NAME, 'DS_CARTAO')
            element3 = self.driver.find_element(By.NAME, 'CD_DEPENDENCIA')
            
            self.driver.execute_script("arguments[0].setAttribute('type', 'text');", element7)
            element7.clear()
            element7.send_keys(cartCompleto)
            
            self.driver.execute_script("arguments[0].setAttribute('type', 'text');", element6)
            element6.clear()
            element6.send_keys(cartaoParcial)
            
            self.driver.execute_script("arguments[0].setAttribute('type', 'text');", element3)
            element3.clear()
            element3.send_keys(x3)
            
            if x1 != "0064":
                 self.log(f"Carteirinha prefix {x1} != 0064. Checking Validade...", job_id=job_id, carteirinha_id=carteirinha_db_id)
                 if len(self.driver.find_elements(By.XPATH, '//*[@id="Button_Consulta"]')) > 0:
                      self.driver.find_element(By.XPATH, '//*[@id="Button_Consulta"]').click()
                      time.sleep(2)
            
            # Wait for results table
            self.log("Waiting for Results Table...", job_id=job_id, carteirinha_id=carteirinha_db_id)
            try:
                WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.XPATH, '//*[@id="s_NR_GUIA"]')))
            except TimeoutException:
                 self.log("Timeout waiting for results table. Maybe no guias or connection error.", level="WARNING", job_id=job_id, carteirinha_id=carteirinha_db_id)
                 # Close popup and return empty
                 self.driver.close()
                 self.driver.switch_to.window(self.driver.window_handles[0])
                 self.log("Retorno guias", job_id=job_id, carteirinha_id=carteirinha_db_id)
                 return {"valida_prestador": {"tipo_json": None, "guias": None}, "guias_scraped": []}

            collected_data = [] 
            valida_guias = {}
            
            # Helper to check element presence
            def is_element_present(by, value):
                try:
                    self.driver.find_element(by, value)
                    return True
                except NoSuchElementException:
                    return False

            # Sort by Date (click header twice)
            self.log("Sorting table by date (Clicking header twice)...", job_id=job_id, carteirinha_id=carteirinha_db_id)
            try:
                # Based on original script: //*[@id="conteudo-submenu"]/table[2]/tbody/tr[1]/td[1]/a
                header_xpath = '//*[@id="conteudo-submenu"]/table[2]/tbody/tr[1]/td[1]/a'
                if is_element_present(By.XPATH, header_xpath):
                    # First Click
                    self.driver.find_element(By.XPATH, header_xpath).click()
                    self.log("Clicked header once. Waiting 4s...", job_id=job_id, carteirinha_id=carteirinha_db_id)
                    time.sleep(4)
                    
                    # Re-find element to avoid stale reference
                    self.driver.find_element(By.XPATH, header_xpath).click()
                    self.log("Clicked header twice. Waiting 2s...", job_id=job_id, carteirinha_id=carteirinha_db_id)
                    time.sleep(2)
                else:
                    self.log("Sort header not found. Proceeding without explicit sort.", level="WARNING", job_id=job_id, carteirinha_id=carteirinha_db_id)
            except Exception as sort_e:
                self.log(f"Error while sorting table: {sort_e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)

            self.log("Starting scraping loop...", job_id=job_id, carteirinha_id=carteirinha_db_id)
            
            while True:
                try:
                    # Re-find table elements on each iteration/page
                    DataTable = self.driver.find_element(By.XPATH, '//*[@id="conteudo-submenu"]/table[2]')
                    linhas = DataTable.find_elements(By.TAG_NAME, "tr")
                    # Skip header and maybe footer? Original skipped [1:-1]
                    # Original loop: range(1, x_count - 1) => skipping first (header) and last?
                    # Let's inspect rows to be safe. Usually tr[0] is header.
                    
                    rows_to_process = len(linhas)
                    self.log(f"Found {rows_to_process} rows on page.", job_id=job_id, carteirinha_id=carteirinha_db_id)
                    
                    # Iterate rows
                    # Note: accessing by index is fragile if DOM changes, but following original logic
                    for idx in range(1, rows_to_process - 1):
                        try:
                            # Re-find element to avoid stale reference
                            row_xpath = f'//*[@id="conteudo-submenu"]/table[2]/tbody/tr[{idx+1}]'
                            status_span = self.driver.find_element(By.XPATH, f'{row_xpath}/td[6]/span')
                            
                            # Scroll into view
                            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", status_span)
                            
                            if status_span.text == "Autorizado":
                                date_element = self.driver.find_element(By.XPATH, f'{row_xpath}/td[1]')
                                date_text = date_element.text.strip()
                                
                                try:
                                    guia_date = datetime.datetime.strptime(date_text, "%d/%m/%Y").date()
                                except:
                                    guia_date = datetime.datetime.now().date()
                                
                                # Date Filter (Old guides)
                                cutoff_date = datetime.datetime.now().date() - datetime.timedelta(days=270) # Using 270 as in original
                                if guia_date < cutoff_date:
                                    self.log(f"Guia date {date_text} is older than limit. Stopping.", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                    # Close popup and return what we have
                                    self.driver.close()
                                    self.driver.switch_to.window(self.driver.window_handles[0])
                                    
                                    tipo_json = "All Sucess"
                                    if any(val.get("Vinculo_prestador") != "Guia Válida" for val in valida_guias.values()):
                                        tipo_json = "Thered"
                                    
                                    if not valida_guias:
                                        tipo_json = None
                                        valida_guias_out = {}
                                    else:
                                        tipo_json = "All Sucess"
                                        if any(val.get("Vinculo_prestador") != "Guia Válida" for val in valida_guias.values()):
                                            tipo_json = "Thered"
                                        valida_guias_out = valida_guias
                                    return {
                                        "valida_prestador": {"tipo_json": tipo_json, "guias": valida_guias_out},
                                        "guias_scraped": collected_data
                                    }

                                # Click to details
                                link_element = self.driver.find_element(By.XPATH, f'{row_xpath}/td[4]/a')
                                
                                try:
                                    input_hidden = self.driver.find_element(By.XPATH, f'{row_xpath}//input[@name="HTML_HINT_ITEM"]')
                                    raw_html = input_hidden.get_attribute("value")
                                    decoded_html = html.unescape(raw_html)
                                    soup = BeautifulSoup(decoded_html, "html.parser")
                                    guide_codigo = soup.find("td", class_="hint-td-cd-item").get_text(strip=True)
                                    ds_el = soup.find("td", class_="hint-td-ds-item")
                                    guide_descricao = ds_el.get_text(strip=True).lstrip("- ").strip() if ds_el else ""
                                except Exception as e:
                                    guide_codigo = ""
                                    guide_descricao = ""
                                    self.log(f"Failed to extract guide_codigo/descricao: {str(e)}", level="WARNING", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                
                                try:
                                    guide_numero = link_element.text.strip()
                                except:
                                    guide_numero = ""

                                link_element.click()
                                time.sleep(3)
                                
                                # Extract Details
                                try:
                                    try:
                                        err_el = self.driver.find_element(By.ID, "label_error_redeAtendPrestEspec")
                                        if err_el.is_displayed() or "block" in (err_el.get_attribute("style") or ""):
                                            self.log(f"Row {idx} access denied by system, skipping.", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                            # Using the text from the error div if present, otherwise default
                                            err_msg = err_el.text.strip()
                                            if not err_msg:
                                                err_msg = "Acesso negado pelo sistema restrito ao prestador"
                                                
                                            valida_guias[guide_numero] = {
                                                "codigo_procedimento": guide_codigo,
                                                "descricao_procedimento": guide_descricao,
                                                "Vinculo_prestador": err_msg
                                            }
                                            # Also add to collected_data
                                            collected_data.append({
                                                "numero_guia": guide_numero,
                                                "data_autorizacao": None,
                                                "senha": None,
                                                "validade_senha": None,
                                                "codigo_procedimento": guide_codigo,
                                                "descricao_procedimento": guide_descricao,
                                                "qtde_solicitada": 0,
                                                "qtde_autorizada": 0
                                            })
                                            try:
                                                self.driver.execute_script("window.history.go(-1)")
                                                time.sleep(1)
                                            except: pass
                                            continue
                                    except NoSuchElementException:
                                        pass
                                    
                                    # --- LAYER 2: API Verification (getErrosSapia) ---
                                    try:
                                        current_url = self.driver.current_url
                                        if "CD_GUIA=" in current_url:
                                            # Extract cdGuia
                                            cdGuia = current_url.split("CD_GUIA=")[1].split("&")[0]
                                            urlApi = f"https://sgucard.unimedgoiania.coop.br/cmagnet/servlet/getErrosSapia?cdGuia={cdGuia}"
                                            
                                            self.log(f"Layer 2 check for Guia {guide_numero} (cdGuia={cdGuia})", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                            
                                            import requests
                                            session = requests.Session()
                                            for cookie in self.driver.get_cookies():
                                                session.cookies.set(cookie['name'], cookie['value'])
                                            
                                            api_resp = session.get(urlApi, timeout=10)
                                            if api_resp.status_code == 200:
                                                try:
                                                    api_data = api_resp.json()
                                                    # Example: {"erros":[{"msg":"PRESTADOR INFORMADO NÃO PERTENCE A REDE DO BENEFICIÁRIO"}]}
                                                    # Success: {"erros":[]}
                                                    if api_data.get("erros") and len(api_data["erros"]) > 0:
                                                        err_msg = api_data["erros"][0].get("msg", "Erro de rede identificado")
                                                        self.log(f"Layer 2 Block: {err_msg}", level="WARNING", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                                        
                                                        valida_guias[guide_numero] = {
                                                            "codigo_procedimento": guide_codigo,
                                                            "descricao_procedimento": guide_descricao,
                                                            "Vinculo_prestador": err_msg
                                                        }
                                                        
                                                        # Also add to guias_scraped so dispatcher can sync it even if details were blocked
                                                        collected_data.append({
                                                            "numero_guia": guide_numero,
                                                            "data_autorizacao": None,
                                                            "senha": None,
                                                            "validade_senha": None,
                                                            "codigo_procedimento": guide_codigo,
                                                            "descricao_procedimento": guide_descricao,
                                                            "qtde_solicitada": 0,
                                                            "qtde_autorizada": 0
                                                        })
                                                        
                                                        # Go back to table
                                                        try:
                                                            self.driver.execute_script("window.history.go(-1)")
                                                            time.sleep(1)
                                                        except: pass
                                                        continue
                                                except Exception as json_e:
                                                    self.log(f"Failed to parse API JSON: {json_e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                            else:
                                                self.log(f"API Error {api_resp.status_code}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                    except Exception as api_e:
                                        self.log(f"Layer 2 Error: {api_e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                    # ------------------------------------------------
                                    
                                    # Wait for detail view
                                    if is_element_present(By.XPATH, '//*[@id="Button_Voltar"]'):
                                        # Scrape details
                                        # Using XPaths from original
                                        new_num_guia = self.driver.find_element(By.XPATH, '//*[@id="conteudo-submenu"]/form/table/tbody/tr[3]/td[2]').text
                                        data_auth = self.driver.find_element(By.XPATH, '//*[@id="conteudo-submenu"]/form/table/tbody/tr[4]/td[4]').text
                                        senha = self.driver.find_element(By.XPATH, '//*[@id="conteudo-submenu"]/form/table/tbody/tr[5]/td[2]').text
                                        data_valid = self.driver.find_element(By.XPATH, '//*[@id="CampoValidadeSenha"]').text
                                        cod_terapia = self.driver.find_element(By.XPATH, '/html/body/div[1]/div[13]/div/table/tbody/tr[2]/td[3]/input').get_attribute("value")
                                        qtde_solic = self.driver.find_element(By.XPATH, '/html/body/div[1]/div[13]/div/table/tbody/tr[2]/td[5]').text.strip()
                                        qtde_aut = self.driver.find_element(By.XPATH, '/html/body/div[1]/div[13]/div/table/tbody/tr[2]/td[6]').text.strip()
                                        
                                        guia_data = {
                                            "numero_guia": new_num_guia,
                                            "data_autorizacao": data_auth,
                                            "senha": senha,
                                            "validade_senha": data_valid,
                                            "codigo_procedimento": cod_terapia,
                                            "descricao_procedimento": guide_descricao,
                                            "qtde_solicitada": qtde_solic,
                                            "qtde_autorizada": qtde_aut,
                                            "status": "Autorizado"
                                        }
                                        collected_data.append(guia_data)
                                        numero_chave = guide_numero or new_num_guia
                                        valida_guias[numero_chave] = {
                                            "codigo_procedimento": guide_codigo or cod_terapia,
                                            "descricao_procedimento": guide_descricao,
                                            "Vinculo_prestador": "Guia Válida"
                                        }
                                        self.log(f"Scraped Guia {new_num_guia}", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                        
                                        # Go Back
                                        self.driver.find_element(By.XPATH, '//*[@id="Button_Voltar"]').click()
                                        time.sleep(1)
                                    else:
                                         self.log("Detail view not loaded correctly.", level="WARNING", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                         self.driver.back() # Try browser back? or just loop
                                except Exception as inner_e:
                                    self.log(f"Error extracting details: {inner_e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                                    # Try to recover navigation
                                    try:
                                        self.driver.execute_script("window.history.go(-1)")
                                    except: pass

                        except Exception as row_e:
                            self.log(f"Error processing row {idx}: {row_e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                            continue

                    # Pagination
                    try:
                         next_link = self.driver.find_element(By.LINK_TEXT, "Próxima")
                         self.log("Navigating to next page...", job_id=job_id, carteirinha_id=carteirinha_db_id)
                         next_link.click()
                         time.sleep(2)
                    except NoSuchElementException:
                        self.log("No more pages.", job_id=job_id, carteirinha_id=carteirinha_db_id)
                        break

                except Exception as table_e:
                    self.log(f"Error validating table loop: {table_e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
                    break
            
            self.driver.close()
            self.driver.switch_to.window(self.driver.window_handles[0])
            
            tipo_json = "All Sucess"
            if any(val.get("Vinculo_prestador") != "Guia Válida" for val in valida_guias.values()):
                tipo_json = "Thered"
            
            # Using dict mapping instead of array to map to arbitrary valid JSON struct
            self.log("Retorno guias", job_id=job_id, carteirinha_id=carteirinha_db_id)
            if not valida_guias:
                tipo_json = None
                valida_guias_out = {}
            else:
                tipo_json = "All Sucess"
                if any(val.get("Vinculo_prestador") != "Guia Válida" for val in valida_guias.values()):
                    tipo_json = "Thered"
                valida_guias_out = valida_guias
            return {
                "valida_prestador": {"tipo_json": tipo_json, "guias": valida_guias_out},
                "guias_scraped": collected_data
            }

        except Exception as e:
            self.log(f"Error processing carteirinha: {e}", level="ERROR", job_id=job_id, carteirinha_id=carteirinha_db_id)
            if len(self.driver.window_handles) > 1:
                self.driver.close()
                self.driver.switch_to.window(self.driver.window_handles[0])
            raise e

# Main execution if run directly
if __name__ == "__main__":
    s = UnimedScraper()
    s.login()
    # s.process_carteirinha("...")
