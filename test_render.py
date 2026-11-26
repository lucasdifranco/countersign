import os

os.environ.update(WEBHOOK_SECRET="s", GITHUB_TOKEN="t", LLM_API_KEY="k", REPOS="o/r")
import render

RELATORIO = """## Automated review — `abc1234` (modelo)

3 confirmed, 4 finding(s) total. Tokens: 1.000 · Cost: $0.01.

### minor · uncertain — `apps/a.py:3-3`

Texto do achado com `código` e **negrito**.

_Verifier:_ O trecho confirma a linha.

### major · confirmed — `apps/b.py:9-10`

Outro achado.

```
valor = 1 < 2
```
"""


def test_cabecalho_e_achados():
    corpo = render.corpo(RELATORIO)
    assert "<h2>Automated review" in corpo
    assert 'class="minor"' in corpo and 'class="major"' in corpo
    assert 'class="chip uncertain"' in corpo and 'class="chip confirmed"' in corpo
    assert "apps/a.py:3-3" in corpo


def test_verificador_separado():
    corpo = render.corpo(RELATORIO)
    assert 'class="verificador"' in corpo
    assert "O trecho confirma a linha." in corpo


def test_codigo_escapado():
    corpo = render.corpo(RELATORIO)
    assert "1 &lt; 2" in corpo and "<pre><code>" in corpo


def test_html_do_relatorio_e_escapado():
    corpo = render.corpo("### minor · confirmed — `x<y`\n\n<script>alert(1)</script>\n")
    assert "<script>" not in corpo and "&lt;script&gt;" in corpo


def test_pagina_tem_titulo_e_estado():
    pagina = render.pagina("org/repo@abc1234", RELATORIO, {"state": "failure", "usage": {"total_tokens": 1000}})
    assert "<title>org/repo@abc1234</title>" in pagina
    assert 'class="selo failure"' in pagina
    assert "1.000 tokens" in pagina


def test_paragrafo_e_lista():
    corpo = render.corpo("Texto solto.\n\n- um\n- dois\n")
    assert "<p>Texto solto.</p>" in corpo
    assert "<ul><li>um</li><li>dois</li></ul>" in corpo
