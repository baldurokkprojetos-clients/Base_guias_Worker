"""Valida _read_terapia_fields/_detect_blocked_message contra DOMs simulados
da página de detalhe (Chrome headless efêmero, sem servidor local)."""
import os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Worker'))
os.environ.setdefault("SGUCARD_HEADLESS", "true")

from ImportBaseGuias import UnimedScraper

# ── Case A: página normal, tabela no div[13] (XPath antigo funcionaria) ──
HTML_A = """<html><body><div id="wrap">
""" + "".join(f"<div id='d{i}'></div>" for i in range(1, 13)) + """
<div id='d13'><div><table><tbody>
<tr><td>h1</td><td>h2</td><td>Codigo</td><td>h4</td><td>h5</td><td>h6</td></tr>
<tr><td>x</td><td>y</td><td><input type='text' value='2250005103'></td><td>d</td><td> 12 </td><td> 12 </td></tr>
</tbody></table></div></div>
</div></body></html>"""

# ── Case B: div extra inserido (banner de bloqueio) empurrou a tabela p/ div[14] ──
HTML_B = """<html><body><div id="wrap">
""" + "".join(f"<div id='d{i}'></div>" for i in range(1, 13)) + """
<div id='d13'><span id='label_error_redeAtendPrestEspec'>Prestador principal nao possui itens liberados para execucao.</span></div>
<div id='d14'><div><table><tbody>
<tr><td>h1</td><td>h2</td><td>Codigo</td><td>h4</td><td>h5</td><td>h6</td></tr>
<tr><td>x</td><td>y</td><td><input type='text' value='10101012'></td><td>d</td><td> 1 </td><td> 1 </td></tr>
</tbody></table></div></div>
</div></body></html>"""

# ── Case C: guia bloqueada SEM tabela de terapias (cenário real dos jobs 162421/162419) ──
HTML_C = """<html><body><div id="wrap">
<div id="conteudo-submenu"><form><table><tbody>
<tr><td>Carteirinha</td><td>0064.0621.015931.30-8</td></tr>
<tr><td>x</td><td>y</td></tr>
<tr><td>Guia</td><td>75063971</td></tr>
<tr><td>a</td><td>b</td><td>c</td><td>14/10/2026</td></tr>
<tr><td>Senha</td><td>123456</td></tr>
</tbody></table></form></div>
<div id='msg'><div class='error-message'>Prestador principal nao possui itens liberados para execucao.</div></div>
<input type='button' id='Button_Voltar' value='Voltar'>
</div></body></html>"""


def run():
    s = UnimedScraper()
    s.start_driver()
    try:
        tmp = tempfile.mkdtemp()
        cases = [("A normal (div13)", HTML_A), ("B tabela deslocada (div14)", HTML_B), ("C sem tabela (bloqueada)", HTML_C)]
        ok = True
        for name, html in cases:
            path = os.path.join(tmp, f"{name[0]}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            s.driver.get(f"file:///{path.replace(os.sep, '/')}")
            cod, solic, aut = s._read_terapia_fields()
            msg = s._detect_blocked_message()
            print(f"[{name}] cod={cod!r} solic={solic!r} aut={aut!r} block_msg={msg!r}")
            if name.startswith("A"):
                ok &= cod == "2250005103" and solic == "12" and aut == "12"
            elif name.startswith("B"):
                ok &= cod == "10101012" and solic == "1" and aut == "1"
            else:
                ok &= cod is None and msg is not None and "itens liberados" in msg
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        s.close_driver()


if __name__ == "__main__":
    sys.exit(run())
