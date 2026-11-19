#!/usr/bin/env python3
"""
Converte um CSV numa tabela LaTeX (tabular com bordas, cabeçalho em negrito).
Opcionalmente destaca uma linha com fundo cinza e compila um PDF com pdflatex.

USO
    python3 csv_para_latex.py dados.csv                     # imprime a tabela
    python3 csv_para_latex.py dados.csv -o tabela.tex       # salva só a tabela
    python3 csv_para_latex.py dados.csv -o doc.tex --documento --pdf
    python3 csv_para_latex.py dados.csv --destacar 3        # 3ª linha de dados
    python3 csv_para_latex.py dados.csv -d ';'              # CSV com ponto e vírgula

    Sem --documento, sai só o bloco \\begin{table}...\\end{table}, pronto para
    colar num .tex existente (precisa de \\usepackage{graphicx} e
    \\usepackage[table]{xcolor} no preâmbulo). Com --documento, sai um .tex
    completo que compila sozinho.

    Caracteres especiais do LaTeX (& % $ # _ { } ~ ^ \\) são escapados.
    O separador é detectado sozinho (vírgula, ponto e vírgula, tab) se não
    for passado com -d.

    --pdf precisa do pdflatex (sudo apt install texlive-latex-base
    texlive-latex-recommended) e implica --documento.
"""

import argparse
import csv
import subprocess
import sys
from pathlib import Path

ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escapar(texto: str) -> str:
    return "".join(ESCAPES.get(c, c) for c in texto)


def ler_csv(caminho: Path, separador: str | None) -> list[list[str]]:
    with caminho.open(newline="", encoding="utf-8-sig") as f:
        amostra = f.read(8192)
        f.seek(0)
        if separador is None:
            try:
                separador = csv.Sniffer().sniff(amostra, delimiters=",;\t|").delimiter
            except csv.Error:
                # sniffer falha com linhas de tamanhos diferentes; usa o
                # candidato que mais aparece na primeira linha
                primeira = amostra.splitlines()[0] if amostra else ""
                separador = max(",;\t|", key=primeira.count)
        linhas = [linha for linha in csv.reader(f, delimiter=separador) if linha]
    if not linhas:
        sys.exit(f"erro: {caminho} está vazio")
    largura = max(len(l) for l in linhas)
    return [l + [""] * (largura - len(l)) for l in linhas]


def gerar_tabela(linhas, cabecalho=True, destacar=None, legenda=None, ajustar=True):
    ncol = len(linhas[0])
    out = [r"\begin{table}[ht]", r"\centering"]
    if ajustar:
        out.append(r"\resizebox{\textwidth}{!}{%")
    out += [r"\begin{tabular}{|" + "l|" * ncol + "}", r"\hline"]
    for i, linha in enumerate(linhas):
        celulas = [escapar(c.strip()) for c in linha]
        prefixo = ""
        if cabecalho and i == 0:
            celulas = [rf"\textbf{{{c}}}" for c in celulas]
        n_dado = i if cabecalho else i + 1  # número da linha de dados (1 = primeira)
        if destacar is not None and n_dado == destacar and not (cabecalho and i == 0):
            prefixo = r"\rowcolor[HTML]{EFEFEF} "
            celulas = [rf"\textit{{{c}}}" if c else c for c in celulas]
        out.append(prefixo + " & ".join(celulas) + r" \\ \hline")
    out.append(r"\end{tabular}")
    if ajustar:
        out.append("}")
    if legenda:
        out.append(rf"\caption{{{escapar(legenda)}}}")
    out.append(r"\end{table}")
    return "\n".join(out) + "\n"


def envolver_documento(tabela: str) -> str:
    return (
        "\\documentclass[12pt]{article}\n"
        "\\usepackage[utf8]{inputenc}\n"
        "\\usepackage[T1]{fontenc}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage[table]{xcolor}\n"
        "\\begin{document}\n"
        f"{tabela}"
        "\\end{document}\n"
    )


def main():
    ap = argparse.ArgumentParser(description="Converte CSV em tabela LaTeX.")
    ap.add_argument("csv", type=Path)
    ap.add_argument("-o", "--saida", type=Path, help="arquivo .tex (padrão: imprime na tela)")
    ap.add_argument("-d", "--separador", help="separador do CSV (padrão: detecta)")
    ap.add_argument("--sem-cabecalho", action="store_true",
                    help="a primeira linha é dado, não cabeçalho")
    ap.add_argument("--destacar", type=int, metavar="N",
                    help="destaca a N-ésima linha de dados (1 = primeira)")
    ap.add_argument("--legenda", help="texto do \\caption")
    ap.add_argument("--sem-ajuste", action="store_true",
                    help="não encolhe a tabela para caber na largura da página")
    ap.add_argument("--documento", action="store_true", help="gera um .tex completo")
    ap.add_argument("--pdf", action="store_true", help="compila com pdflatex (exige -o)")
    a = ap.parse_args()

    if not a.csv.is_file():
        sys.exit(f"erro: {a.csv} não existe")
    if a.pdf and not a.saida:
        sys.exit("erro: --pdf precisa de -o arquivo.tex")

    linhas = ler_csv(a.csv, a.separador)
    tex = gerar_tabela(linhas, cabecalho=not a.sem_cabecalho, destacar=a.destacar,
                       legenda=a.legenda, ajustar=not a.sem_ajuste)
    if a.documento or a.pdf:
        tex = envolver_documento(tex)

    if not a.saida:
        sys.stdout.write(tex)
        return
    a.saida.write_text(tex, encoding="utf-8")
    print(f"gravado: {a.saida}", file=sys.stderr)

    if a.pdf:
        pasta = a.saida.resolve().parent
        r = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", a.saida.name],
            cwd=pasta, capture_output=True, text=True,
        )
        if r.returncode != 0:
            sys.stderr.write(r.stdout[-3000:])
            sys.exit("erro: pdflatex falhou (log acima)")
        for ext in (".aux", ".log"):
            (pasta / (a.saida.stem + ext)).unlink(missing_ok=True)
        print(f"gravado: {a.saida.with_suffix('.pdf')}", file=sys.stderr)


if __name__ == "__main__":
    main()
