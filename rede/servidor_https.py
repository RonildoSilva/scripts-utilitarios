#!/usr/bin/env python3
"""
Servidor HTTPS simples que serve os arquivos de uma pasta, como o
`python3 -m http.server`, só que com TLS. Útil para testar coisas que o
navegador só libera em HTTPS (câmera, microfone, service worker, PWA) a partir
do celular ou de outra máquina da rede.

USO
    python3 servidor_https.py                       # pasta atual, porta 4443
    python3 servidor_https.py ~/site -p 8443
    python3 servidor_https.py --rede                # aceita conexões da rede local
    python3 servidor_https.py --cert c.pem --chave k.pem

    Sem --cert, gera um certificado autoassinado (openssl) em
    ~/.cache/servidor_https/ e reaproveita nas próximas vezes. O navegador vai
    reclamar do certificado: é só aceitar o aviso.

    Por padrão só escuta em 127.0.0.1. Com --rede escuta em 0.0.0.0 e mostra
    o IP para abrir do celular. Não use para nada exposto na internet.
"""

import argparse
import functools
import http.server
import socket
import ssl
import subprocess
import sys
from pathlib import Path

CACHE = Path.home() / ".cache" / "servidor_https"


def gerar_certificado() -> tuple[Path, Path]:
    cert, chave = CACHE / "cert.pem", CACHE / "chave.pem"
    if cert.exists() and chave.exists():
        return cert, chave
    CACHE.mkdir(parents=True, exist_ok=True)
    print("gerando certificado autoassinado...", file=sys.stderr)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(chave), "-out", str(cert), "-days", "3650",
             "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True,
        )
    except FileNotFoundError:
        sys.exit("erro: openssl não encontrado (sudo apt install openssl)")
    except subprocess.CalledProcessError as e:
        sys.exit(f"erro ao gerar certificado:\n{e.stderr.decode()}")
    chave.chmod(0o600)
    return cert, chave


def ip_local() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # não envia nada, só escolhe a interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="Serve uma pasta por HTTPS.")
    ap.add_argument("pasta", nargs="?", default=".", type=Path)
    ap.add_argument("-p", "--porta", type=int, default=4443)
    ap.add_argument("--rede", action="store_true", help="escuta em 0.0.0.0")
    ap.add_argument("--cert", type=Path, help="certificado PEM")
    ap.add_argument("--chave", type=Path, help="chave PEM (se separada do cert)")
    a = ap.parse_args()

    if not a.pasta.is_dir():
        sys.exit(f"erro: {a.pasta} não é uma pasta")
    host = "0.0.0.0" if a.rede else "127.0.0.1"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(a.pasta.resolve()))
    try:
        httpd = http.server.ThreadingHTTPServer((host, a.porta), handler)
    except OSError as e:
        sys.exit(f"erro: não deu para abrir a porta {a.porta}: {e.strerror}")
    if a.cert:
        cert, chave = a.cert, a.chave
    else:
        cert, chave = gerar_certificado()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(cert, chave)
    except (OSError, ssl.SSLError) as e:
        sys.exit(f"erro: certificado/chave inválidos: {e}")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(f"servindo {a.pasta.resolve()}")
    print(f"  https://localhost:{a.porta}/")
    if a.rede:
        print(f"  https://{ip_local()}:{a.porta}/")
    print("Ctrl+C para parar", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
