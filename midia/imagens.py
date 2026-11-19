#!/usr/bin/env python3
"""
Operações rápidas em imagens, uma por subcomando. Precisa do Pillow
(sudo apt install python3-pil).

USO
    # redimensionar mantendo a proporção (dê largura OU altura)
    python3 imagens.py redimensionar foto.jpg --largura 800
    python3 imagens.py redimensionar *.png --altura 300 -o miniaturas/

    # cortar em grade (padrão 2x2 = quatro quadrantes)
    python3 imagens.py dividir mapa.png
    python3 imagens.py dividir mapa.png --linhas 3 --colunas 4 -o pedacos/

    # juntar lado a lado ou empilhadas
    python3 imagens.py juntar a.jpg b.jpg c.jpg -o junto.jpg
    python3 imagens.py juntar a.jpg b.jpg -o junto.jpg --vertical

    Nunca sobrescreve o original. Sem -o, os arquivos novos vão para a mesma
    pasta da imagem, com sufixo no nome (foto_800x600.jpg, mapa_l1c2.png).
    Arquivos já existentes no destino só são substituídos com --forcar.
"""

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("erro: Pillow não instalado (sudo apt install python3-pil)")


def abrir(caminho: Path) -> Image.Image:
    try:
        im = Image.open(caminho)
        return ImageOps.exif_transpose(im) or im  # respeita a rotação das fotos de celular
    except (OSError, ValueError) as e:
        sys.exit(f"erro: não deu para abrir {caminho}: {e}")


def salvar(im: Image.Image, destino: Path, forcar: bool):
    if destino.exists() and not forcar:
        print(f"pulado (já existe, use --forcar): {destino}", file=sys.stderr)
        return
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.suffix.lower() in (".jpg", ".jpeg") and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(destino, quality=92) if destino.suffix.lower() in (".jpg", ".jpeg") else im.save(destino)
    print(destino)


def destino_para(origem: Path, saida: Path | None, sufixo: str) -> Path:
    pasta = saida if saida else origem.parent
    return pasta / f"{origem.stem}_{sufixo}{origem.suffix}"


def cmd_redimensionar(a):
    if bool(a.largura) == bool(a.altura):
        sys.exit("erro: passe --largura OU --altura (só um)")
    for arq in a.imagens:
        im = abrir(arq)
        w, h = im.size
        if a.largura:
            nw, nh = a.largura, max(1, round(h * a.largura / w))
        else:
            nw, nh = max(1, round(w * a.altura / h)), a.altura
        novo = im.resize((nw, nh), Image.Resampling.LANCZOS)
        salvar(novo, destino_para(arq, a.saida, f"{nw}x{nh}"), a.forcar)


def cmd_dividir(a):
    if a.linhas < 1 or a.colunas < 1:
        sys.exit("erro: --linhas e --colunas precisam ser >= 1")
    for arq in a.imagens:
        im = abrir(arq)
        w, h = im.size
        if a.colunas > w or a.linhas > h:
            sys.exit(f"erro: {arq} ({w}x{h}) é pequena demais para essa grade")
        # bordas calculadas por proporção: sobras de pixel vão para os últimos pedaços
        xs = [w * c // a.colunas for c in range(a.colunas + 1)]
        ys = [h * l // a.linhas for l in range(a.linhas + 1)]
        for l in range(a.linhas):
            for c in range(a.colunas):
                pedaco = im.crop((xs[c], ys[l], xs[c + 1], ys[l + 1]))
                salvar(pedaco, destino_para(arq, a.saida, f"l{l+1}c{c+1}"), a.forcar)


def cmd_juntar(a):
    if len(a.imagens) < 2:
        sys.exit("erro: passe pelo menos duas imagens")
    ims = [abrir(p).convert("RGBA") for p in a.imagens]
    if a.vertical:
        total = (max(i.width for i in ims), sum(i.height for i in ims))
    else:
        total = (sum(i.width for i in ims), max(i.height for i in ims))
    fundo = (0, 0, 0, 0) if a.saida.suffix.lower() == ".png" else (255, 255, 255, 255)
    tela = Image.new("RGBA", total, fundo)
    pos = 0
    for i in ims:
        tela.paste(i, (0, pos) if a.vertical else (pos, 0), i)
        pos += i.height if a.vertical else i.width
    salvar(tela, a.saida, a.forcar)


def main():
    ap = argparse.ArgumentParser(description="Operações rápidas em imagens.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("redimensionar", help="muda o tamanho mantendo a proporção")
    p.add_argument("imagens", nargs="+", type=Path)
    p.add_argument("--largura", type=int)
    p.add_argument("--altura", type=int)
    p.add_argument("-o", "--saida", type=Path, help="pasta de saída")
    p.add_argument("--forcar", action="store_true")
    p.set_defaults(func=cmd_redimensionar)

    p = sub.add_parser("dividir", help="corta em grade (padrão 2x2)")
    p.add_argument("imagens", nargs="+", type=Path)
    p.add_argument("--linhas", type=int, default=2)
    p.add_argument("--colunas", type=int, default=2)
    p.add_argument("-o", "--saida", type=Path, help="pasta de saída")
    p.add_argument("--forcar", action="store_true")
    p.set_defaults(func=cmd_dividir)

    p = sub.add_parser("juntar", help="junta lado a lado (ou empilhadas)")
    p.add_argument("imagens", nargs="+", type=Path)
    p.add_argument("-o", "--saida", type=Path, required=True, help="arquivo de saída")
    p.add_argument("--vertical", action="store_true")
    p.add_argument("--forcar", action="store_true")
    p.set_defaults(func=cmd_juntar)

    a = ap.parse_args()
    for arq in a.imagens:
        if not arq.is_file():
            sys.exit(f"erro: {arq} não existe")
    a.func(a)


if __name__ == "__main__":
    main()
