"""Renderiza o relatório em Markdown como página HTML, sem dependência nova.

O relatório tem formato fixo (markdown() em app.py e audit.markdown): título `##`, um parágrafo de
resumo e, por achado, `### <severidade> · <veredito> — `<caminho:linhas>`` seguido do texto e de
`_Verifier:_ ...`. Este módulo entende esses blocos; o resto cai em parágrafo, lista ou bloco de
código. Tudo que vem do relatório é escapado antes de virar HTML.
"""
import html
import re

SEVERIDADES = ("critical", "major", "minor")
VEREDITOS = ("confirmed", "refuted", "uncertain")
CABECALHO_ACHADO = re.compile(r"^(critical|major|minor)\s*·\s*(confirmed|refuted|uncertain)\s*—\s*(.+)$")

CSS = """
:root {
  --fundo: #ffffff; --texto: #14130f; --fraco: #5b5852; --linha: #e3e0d9; --cartao: #faf9f6;
  --codigo: #f2f0ea; --acento: #0b6bcb;
  --critico: #b42318; --maior: #b54708; --menor: #57534e; --ok: #116329;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fundo: #14140f; --texto: #f1efe8; --fraco: #a8a49a; --linha: #2e2c26; --cartao: #1b1a15;
    --codigo: #232219; --acento: #7cb2f0;
    --critico: #f27a6d; --maior: #f0a35a; --menor: #b8b2a6; --ok: #5fce7c;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--fundo); color: var(--texto);
  font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: min(1500px, 96vw); margin: 0 auto; padding: 2.5rem clamp(1rem, 2vw, 2.5rem) 4rem; }
a { color: var(--acento); }
header { border-bottom: 1px solid var(--linha); padding-bottom: 1.25rem; margin-bottom: 1.75rem; }
header h1 { margin: 0 0 .35rem; font-size: 1.6rem; letter-spacing: -.01em; }
header .meta { color: var(--fraco); font-size: .9rem; display: flex; flex-wrap: wrap; gap: .35rem 1.1rem; align-items: center; }
.intro { max-width: 90ch; }
.achados { display: grid; grid-template-columns: repeat(auto-fill, minmax(540px, 1fr)); gap: 1rem; align-items: start; }
.achados .grupo { grid-column: 1 / -1; margin: .5rem 0 -.25rem; font-size: .8rem; letter-spacing: .06em;
  text-transform: uppercase; color: var(--fraco); }
.selo { display: inline-flex; align-items: center; gap: .35rem; border-radius: 999px; padding: .1rem .6rem;
  font-size: .78rem; font-weight: 600; border: 1px solid currentColor; }
.selo.failure { color: var(--critico); } .selo.success { color: var(--ok); } .selo.pending, .selo.error { color: var(--maior); }
article { border: 1px solid var(--linha); border-left: 3px solid var(--menor); border-radius: 10px;
  padding: 1rem 1.15rem; margin: 0; background: var(--cartao); min-width: 0; }
article.critical { border-left-color: var(--critico); }
article.major { border-left-color: var(--maior); }
article h2 { font-size: 1rem; margin: 0 0 .5rem; display: flex; flex-wrap: wrap; gap: .5rem; align-items: baseline; }
article .caminho { font: .8rem ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: var(--fraco); word-break: break-all; }
.chip { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; font-weight: 700; }
.chip.refuted { color: var(--ok); } .chip.confirmed { color: var(--critico); } .chip.uncertain { color: var(--fraco); }
article p { margin: .5rem 0 0; }
.verificador { color: var(--fraco); font-size: .9rem; border-top: 1px dashed var(--linha); padding-top: .6rem; margin-top: .75rem; }
pre { background: var(--codigo); padding: .8rem 1rem; border-radius: 8px; overflow-x: auto;
  font: .82rem/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
code { background: var(--codigo); padding: .1em .3em; border-radius: 4px;
  font: .86em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre code { background: none; padding: 0; font-size: 1em; }
ul { padding-left: 1.2rem; }
hr { border: 0; border-top: 1px solid var(--linha); margin: 1.5rem 0; }
footer { color: var(--fraco); font-size: .8rem; margin-top: 2.5rem; }
"""


def _inline(texto: str) -> str:
    """Escapa e aplica código, negrito, itálico e link — na ordem que evita casar dentro do código."""
    partes = re.split(r"(`[^`]+`)", texto)
    saida = []
    for parte in partes:
        if parte.startswith("`") and parte.endswith("`") and len(parte) > 2:
            saida.append(f"<code>{html.escape(parte[1:-1])}</code>")
            continue
        t = html.escape(parte)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<![\w_])_([^_]+)_(?![\w_])", r"<em>\1</em>", t)
        t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', t)
        saida.append(t)
    return "".join(saida)


def _blocos(md: str):
    """Agrupa o Markdown em blocos: ('titulo'|'achado'|'codigo'|'lista'|'paragrafo', dado)."""
    linhas = md.splitlines()
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        if linha.startswith("```"):
            codigo, i = [], i + 1
            while i < len(linhas) and not linhas[i].startswith("```"):
                codigo.append(linhas[i])
                i += 1
            yield "codigo", "\n".join(codigo)
            i += 1
            continue
        if linha.startswith("### "):
            achado = CABECALHO_ACHADO.match(linha[4:].strip())
            if achado:
                corpo, i = [], i + 1
                while i < len(linhas) and not linhas[i].startswith(("##", "###", "```")):
                    corpo.append(linhas[i])
                    i += 1
                yield "achado", (achado.group(1), achado.group(2), achado.group(3), "\n".join(corpo))
                continue
            yield "titulo", (3, linha[4:].strip())
            i += 1
            continue
        if linha.startswith("## "):
            yield "titulo", (2, linha[3:].strip())
            i += 1
            continue
        if re.match(r"^[-*] ", linha):
            itens = []
            while i < len(linhas) and re.match(r"^[-*] ", linhas[i]):
                itens.append(linhas[i][2:].strip())
                i += 1
            yield "lista", itens
            continue
        if not linha.strip():
            i += 1
            continue
        paragrafo, i = [], i
        while i < len(linhas) and linhas[i].strip() and not linhas[i].startswith(("##", "###", "```", "- ", "* ")):
            paragrafo.append(linhas[i].strip())
            i += 1
        yield "paragrafo", " ".join(paragrafo)


def _corpo_achado(texto: str) -> str:
    """Texto do achado: parágrafos, lista e código; o `_Verifier:_` vira nota à parte."""
    pedacos, verificador = [], []
    atual, em_codigo = [], None
    for linha in texto.splitlines():
        if linha.startswith("```"):
            if em_codigo is None:
                em_codigo = []
            else:
                pedacos.append("<pre><code>" + html.escape("\n".join(em_codigo)) + "</code></pre>")
                em_codigo = None
            continue
        if em_codigo is not None:
            em_codigo.append(linha)
            continue
        if linha.startswith("_Verifier:_"):
            verificador.append(linha[len("_Verifier:_"):].strip())
            continue
        if not linha.strip():
            if atual:
                pedacos.append("<p>" + _inline(" ".join(atual)) + "</p>")
                atual = []
            continue
        atual.append(linha.strip())
    if atual:
        pedacos.append("<p>" + _inline(" ".join(atual)) + "</p>")
    if verificador:
        pedacos.append('<p class="verificador"><strong>Verificador:</strong> ' + _inline(" ".join(verificador)) + "</p>")
    return "".join(pedacos)


ORDEM_VEREDITO = {"confirmed": 0, "uncertain": 1, "refuted": 2}
ROTULO_VEREDITO = {"confirmed": "Confirmados", "uncertain": "Inconclusivos", "refuted": "Refutados"}


def _cartao(severidade: str, veredito: str, caminho: str, texto: str) -> str:
    return (
        f'<article class="{severidade}">'
        f'<h2><span class="chip {veredito}">{veredito}</span>'
        f'<span class="caminho">{_inline(caminho)}</span></h2>{_corpo_achado(texto)}</article>'
    )


def corpo(md: str) -> str:
    """Markdown do relatório como HTML: texto na ordem, achados em grade por veredito
    (confirmados primeiro, depois inconclusivos, refutados por último)."""
    saida, achados = [], []
    for tipo, dado in _blocos(md):
        if tipo == "titulo":
            nivel, texto = dado
            saida.append(f"<h{nivel}>{_inline(texto)}</h{nivel}>")
        elif tipo == "codigo":
            saida.append("<pre><code>" + html.escape(dado) + "</code></pre>")
        elif tipo == "lista":
            saida.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in dado) + "</ul>")
        elif tipo == "paragrafo":
            saida.append('<p class="intro">' + _inline(dado) + "</p>")
        else:
            achados.append(dado)
    if achados:
        achados.sort(key=lambda a: ORDEM_VEREDITO.get(a[1], 9))  # sort estável: ordem interna preservada
        blocos, grupo = [], None
        for severidade, veredito, caminho, texto in achados:
            if veredito != grupo:
                blocos.append(f'<p class="grupo">{ROTULO_VEREDITO.get(veredito, veredito)} '
                              f'({sum(1 for a in achados if a[1] == veredito)})</p>')
                grupo = veredito
            blocos.append(_cartao(severidade, veredito, caminho, texto))
        saida.append('<section class="achados">' + "".join(blocos) + "</section>")
    return "".join(saida)


def pagina(titulo: str, md: str, resumo: dict | None = None, commit_url: str | None = None) -> str:
    """Página completa. `resumo` é o JSON do relatório (estado, uso, contagem) quando existe."""
    estado = (resumo or {}).get("state", "")
    uso = (resumo or {}).get("usage") or {}
    achados = len((resumo or {}).get("findings") or [])
    meta = []
    if estado:
        meta.append(f'<span class="selo {html.escape(estado)}">{html.escape(estado)}</span>')
    if achados:
        meta.append(f"{achados} achado(s)")
    if uso.get("total_tokens"):
        meta.append(f"{uso['total_tokens']:,} tokens".replace(",", "."))
    if uso.get("cost_text"):
        meta.append(html.escape(uso["cost_text"]))
    if commit_url:
        meta.append(f'<a href="{html.escape(commit_url)}">commit no GitHub</a>')
    return (
        "<!doctype html><html lang=pt-BR><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{html.escape(titulo)}</title><style>{CSS}</style><main>"
        f"<header><h1>{html.escape(titulo)}</h1><p class=meta>{' '.join(meta)}</p></header>"
        f"{corpo(md)}"
        "<footer>countersign: cada achado passou por um verificador; o veredito é do mesmo modelo.</footer>"
        "</main></html>"
    )
