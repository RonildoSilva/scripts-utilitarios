#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup_celular_samsung.py — cópia completa de um celular Android (Samsung A13) montado via MTP/gvfs.

Feito para o cenário chato de verdade: MTP é lento, travador e cai no meio do caminho.
Por isso o script é:

  * priorizado    — trabalha em grupos, do mais importante pro menos: figurinhas e mídias
                    de WhatsApp/Telegram/Instagram/Facebook/X/Reddit primeiro, depois
                    fotos, vídeos, música, documentos, e o cache genérico de apps no fim.
                    Cada grupo é varrido e copiado antes do próximo, então os arquivos
                    que importam começam a cair no disco em segundos — não depois de
                    meia hora catalogando o aparelho inteiro.
  * retomável     — manifesto SQLite; rodar de novo pula o que já veio íntegro
  * resistente    — retenta com backoff e mata leitura travada com watchdog (SIGALRM)
  * atômico       — grava em .part e só renomeia no fim (nunca deixa arquivo pela metade)

Uso:
    python3 backup_celular_samsung.py                  # copia tudo
    python3 backup_celular_samsung.py --apps           # SÓ mídias/figurinhas de apps de conversa e redes sociais
    python3 backup_celular_samsung.py --dry-run        # lista o que faria, sem copiar
    python3 backup_celular_samsung.py --only DCIM Music
    python3 backup_celular_samsung.py --no-android-data   # pula cache de apps (menos os das redes sociais)
    python3 backup_celular_samsung.py --verify md5     # confere hash (mais seguro, ~2x mais lento)
    python3 backup_celular_samsung.py --retry-failed   # reprocessa só o que falhou antes
    python3 backup_celular_samsung.py --organizar --dry-run   # mostra a arrumação da Câmera em AAAA-MM
    python3 backup_celular_samsung.py --sem-por-mes    # copia a Câmera plana, espelho fiel do celular

A Câmera (DCIM/Camera) cai no destino separada por mês: DCIM/Camera/AAAA-MM/arquivo.
O mês vem do nome (20240602_142159.jpg, lv_0_20250113120411.mp4) e, sem data no nome,
da data de modificação. O manifesto grava o caminho organizado, então retomada e
--conferir continuam valendo. --organizar arruma o que já está no disco do jeito antigo.
"""

from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# O gvfs fala MTP de verdade: lista um diretório inteiro (nome+tamanho+data) numa só
# ida ao aparelho e copia pelo backend nativo. Medido neste A13: varredura 36x mais
# rápida que stat() pelo FUSE (2600 vs 76 arq/s) e cópia ~2x mais rápida.
# Sem PyGObject o script continua funcionando pelo ponto de montagem, só mais devagar.
try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    HAVE_GIO = True
except (ImportError, ValueError):
    HAVE_GIO = False

# ─────────────────────────────── configuração padrão ───────────────────────────────

DEST_ROOT = Path("/media/ronildo/DELL_ARCH7/SmartSwitch/backup")   # vira .../backup/Armazenamento_interno
# Raiz do aparelho. Se não estiver montada (outro celular, outra porta), o script
# cai na detecção automática do gvfs.
SOURCE_ROOT = Path(f"/run/user/{os.getuid()}/gvfs/"
                   "mtp:host=SAMSUNG_SAMSUNG_Android_RQ8TB0196ZH/Armazenamento interno")
GVFS_DIR = Path(f"/run/user/{os.getuid()}/gvfs")

# Onde cada app guarda mídia e figurinha. Caminho que não existe é ignorado em
# silêncio, então dá pra listar variações à vontade (o Android muda isso a cada versão).
APPS: dict[str, list[str]] = {
    "WhatsApp": [
        "Android/media/com.whatsapp",            # Media/WhatsApp Stickers, .Statuses, Databases, Backups
        "Android/media/com.whatsapp.w4b",        # WhatsApp Business
        "Android/data/com.whatsapp",             # Cached Voice Notes
        "WhatsApp", "DCIM/WhatsApp", "Movies/WhatsApp", "Pictures/WhatsApp",
    ],
    "Telegram": [
        "Android/media/org.telegram.messenger",  # Telegram Images / Telegram Video
        "Android/media/org.telegram.messenger.web",
        "Android/data/org.telegram.messenger",   # cache: *.tgs = figurinhas animadas
        "Telegram", "Movies/Telegram", "Pictures/Telegram", "DCIM/Telegram",
    ],
    "Instagram": [
        "Android/media/com.instagram.android",
        "Android/data/com.instagram.android",
        "Movies/Instagram", "Pictures/Instagram", "DCIM/Instagram",
    ],
    "Facebook": [
        "Android/media/com.facebook.katana", "Android/data/com.facebook.katana",
        "Android/media/com.facebook.orca", "Android/data/com.facebook.orca",  # Messenger
        "DCIM/Facebook", "Movies/Facebook", "Pictures/Facebook",
    ],
    "Twitter/X": [
        "Android/data/com.twitter.android",      # files/media-repo + tmp-*.jpg
        "Android/media/com.twitter.android",
        "Android/data/com.x.android", "Android/media/com.x.android",
        "DCIM/Twitter", "Movies/Twitter", "Pictures/Twitter", "Twitter",
    ],
    "Reddit": [
        "Android/data/com.reddit.frontpage",     # cache/reddit-video/*.exo (fragmentos de vídeo)
        "Android/media/com.reddit.frontpage",
        "Pictures/Reddit", "DCIM/Reddit",
    ],
    "BuzzCast": [
        # BuzzCast/FaceShow: o cache guarda as imagens das conversas e das lives.
        # Glide_cache/*.0 são JPEG e PNG inteiros, só com nome opaco — o --renomear
        # (ligado por padrão) descobre o tipo e acrescenta a extensão certa.
        "Android/data/com.guochao.faceshow",
        "Android/media/com.guochao.faceshow",
        "Android/obb/com.guochao.faceshow",
        "DCIM/BuzzCast", "Movies/BuzzCast", "Pictures/BuzzCast", "BuzzCast",
    ],
    "TikTok/Kwai": [
        "Android/media/com.zhiliaoapp.musically", "Android/data/com.zhiliaoapp.musically",
        "Android/media/com.ss.android.ugc.trill", "Android/data/com.ss.android.ugc.trill",
        "Android/media/com.kwai.video", "Android/data/com.kwai.video",
        "DCIM/TikTok", "Movies/TikTok", "Pictures/Kwai",
    ],
    "Discord/Signal": [
        "Android/media/com.discord", "Android/data/com.discord",
        "Android/media/org.thoughtcrime.securesms", "Android/data/org.thoughtcrime.securesms",
        "Signal",
    ],
    "Status savers": [
        "Movies/StatusSaver", "Pictures/StatusSaver", "DCIM/StatusSaver",
        "Android/media/com.cxinventor.file.explorer",
    ],
}

APP_PREFIXES: list[str] = [p for paths in APPS.values() for p in paths]

# Ordem de trabalho. Prefixo mais específico primeiro: o primeiro que casar define o grupo.
# Se a cópia for interrompida, o que ficou de fora é cache de app, não figurinha nem foto.
PRIORITY: list[str] = APP_PREFIXES + [
    "DCIM",               # câmera e screenshots
    "Pictures",
    "Movies",
    "Music",
    "Recordings",
    "Documents",
    "Download",
    "__OUTROS__",         # raiz e qualquer pasta não citada (ReadEra, .SHAREit, lixeira…)
    "Android/media",      # apps de mídia fora da lista acima
    "Android/obb",
    "Android/data",       # cache genérico de app: enorme e quase sempre descartável
]
OUTROS = PRIORITY.index("__OUTROS__")

# Extensão SUSPEITA de ser pedaço de download ou índice de player. Suspeita, não
# sentença: app de conversa grava vídeo e imagem com extensão qualquer, então antes
# de descartar o script lê a assinatura do arquivo no celular. Se o conteúdo for
# reconhecível, ele vem (e ganha a extensão certa); se não for, cai fora e vai
# listado em _logs/descartados.txt. --fragmentos copia tudo sem perguntar.
FRAG_EXT = {
    ".exo", ".uid",                                   # cache do ExoPlayer (Reddit): pedaços de vídeo
    ".temp", ".preload", ".pt",                       # Telegram: download incompleto / índice de preview
    ".part", ".partial", ".crdownload", ".download", ".tmp",   # download interrompido
}
FRAG_NOME = {".nomedia", "thumbs.db", ".ds_store", "desktop.ini", ".trashed"}
FRAG_PREFIXO = (".thumbdata",)                        # base de miniaturas da galeria

# Apps cuja pasta vale inteira: nada de peneira, vem tudo como está no celular.
# O BuzzCast guarda mídia de conversa e de live com extensão imprevisível, e a
# estrutura do cache é o que dá sentido aos arquivos — separar seria perder contexto.
APPS_INTEGRAIS = {"BuzzCast"}

# Extensão que não diz nada sobre o conteúdo. Cache de app usa isso à vontade:
# o Glide (BuzzCast, Instagram, Reddit) grava JPEG e PNG inteiros como "<hash>.0".
# Nesses casos o tipo real é descoberto pela assinatura e a extensão é ACRESCENTADA
# (fica "<hash>.0.jpg"), preservando o nome original e o caminho de origem no CSV.
OPACO = {"", ".dat", ".bin", ".cnt", ".cache", ".blob", ".img", ".file"}

# Família de cada extensão conhecida. Serve pra pegar o caso que o BuzzCast tem:
# arquivo com extensão de imagem que na verdade é vídeo (e vice-versa). Quando a
# família da extensão briga com a do conteúdo, a extensão real é acrescentada.
FAMILIA = {
    ".jpg": "imagem", ".jpeg": "imagem", ".png": "imagem", ".gif": "imagem",
    ".webp": "imagem", ".bmp": "imagem", ".heic": "imagem", ".heif": "imagem",
    ".mp4": "video", ".mkv": "video", ".webm": "video", ".3gp": "video",
    ".mov": "video", ".avi": "video", ".m4v": "video",
    ".mp3": "audio", ".ogg": "audio", ".opus": "audio", ".m4a": "audio",
    ".wav": "audio", ".flac": "audio", ".aac": "audio",
    ".pdf": "documento", ".zip": "pacote", ".gz": "pacote", ".sqlite": "banco",
}

ASSINATURAS: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"%PDF", ".pdf"),
    (b"ID3", ".mp3"), (b"\xff\xfb", ".mp3"), (b"\xff\xf3", ".mp3"),
    (b"OggS", ".ogg"),
    (b"fLaC", ".flac"),
    (b"\x1aE\xdf\xa3", ".webm"),                       # Matroska: webm ou mkv
    (b"PK\x03\x04", ".zip"),
    (b"\x1f\x8b", ".gz"),                              # .tgs do Telegram cai aqui
    (b"SQLite format 3\x00", ".sqlite"),
]

# Piso pra vídeo e áudio de verdade. O cache do Reddit guarda o segmento de
# inicialização do mp4: 1 KB, o ffprobe lê como vídeo válido de 55 segundos e não
# tem um único frame dentro. Container reconhecido menor que isso é cabeçalho solto,
# não mídia. Imagem não entra nessa regra — ícone de 500 bytes é legítimo e abre.
MIN_MEDIA_BYTES = 8 * 1024

CHUNK = 4 * 1024 * 1024          # 4 MiB: MTP rende mais com bloco grande
CHUNK_TIMEOUT = 180              # s sem retorno numa leitura => considera travado
LIST_TIMEOUT = 120               # s para listar um diretório
MAX_ATTEMPTS = 4
MTP_RECUSA = "Could not get file from device"

# Pendência que NÃO impede formatar: cache, temporário de navegador, status do
# WhatsApp (somem em 24 h). Qualquer outra pendência é tratada como importante.
DESCARTAVEL_SE_FALTAR = (
    "/cache/", "/Glide_cache/", "/.Statuses/", "/.com.google.Chrome.", "/tombstones/",
    "/.Shared/", "/.trash/", "/.Trash/", "/files/Download/.com.google.Chrome",
)
RESERVE_BYTES = 1 * 1024**3      # deixa 1 GiB livre na partição de destino
MAX_NAME_BYTES = 250             # ext4 limita nome de arquivo a 255 bytes

# Pastas do celular cujos arquivos (os que estão direto nelas, não em subpasta) vão
# para <pasta>/AAAA-MM/ no destino. --sem-por-mes esvazia isto.
POR_MES: tuple[str, ...] = ("DCIM/Camera",)
# Data no começo do nome ou logo após um "_": 20240602_142159.jpg, lv_0_20250113120411.mp4.
# Ancorado assim para não pescar "2024..." no meio de um nome-hash hexadecimal.
DATA_NO_NOME = re.compile(r"(?:^|_)(20\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")
PASTA_MES = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

RETRYABLE = {errno.EIO, errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT,
             errno.ENODEV, errno.EINTR, errno.EPROTO, errno.ECONNRESET}


# ─────────────────────────────────── utilidades ───────────────────────────────────

def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def hms(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def norm(path: str) -> str:
    return path.replace(os.sep, "/").strip("/")


def under(path: str, prefix: str) -> bool:
    """path é prefix ou está dentro dele."""
    return path == prefix or path.startswith(prefix + "/")


class Stalled(Exception):
    """O MTP não respondeu no tempo — tratado como erro retentável."""


class Timeout:
    """Watchdog por operação via SIGALRM: single-thread, sem thread pendurada."""

    def __init__(self, seconds: int):
        self.seconds = seconds

    def _fire(self, signum, frame):
        raise Stalled(f"sem resposta do dispositivo por {self.seconds}s")

    def __enter__(self):
        self.previous = signal.signal(signal.SIGALRM, self._fire)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, *exc):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous)
        return False


class Watchdog:
    """Cancela uma operação Gio que parou de progredir.

    Precisa ser thread, não SIGALRM: chamada Gio bloqueia dentro do C e o handler
    de sinal do Python só rodaria depois dela voltar — justamente o que não acontece
    quando o MTP trava. Gio.Cancellable.cancel() é thread-safe e faz a chamada
    bloqueada retornar com erro CANCELLED.
    """

    def __init__(self, cancellable, timeout: int):
        self.cancellable = cancellable
        self.timeout = timeout
        self.last = time.monotonic()
        self.done = threading.Event()
        self.fired = False
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def touch(self) -> None:
        self.last = time.monotonic()

    def _watch(self) -> None:
        while not self.done.wait(1.0):
            if time.monotonic() - self.last > self.timeout:
                self.fired = True
                self.cancellable.cancel()
                return

    def __enter__(self) -> "Watchdog":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.done.set()
        return False


def gio_error(exc, wd: Watchdog | None, what: str):
    """Traduz GLib.Error para as exceções que o laço de retentativa entende."""
    if wd is not None and wd.fired:
        return Stalled(f"{what}: sem progresso por {wd.timeout}s (cancelado)")
    if HAVE_GIO and exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
        return FileNotFoundError(errno.ENOENT, f"{what}: não existe mais")
    return OSError(errno.EIO, f"{what}: {exc.message}")


class Barra:
    """Barra de progresso numa linha só, redesenhada no lugar (\r).

    Só aparece em terminal de verdade: com a saída redirecionada pra arquivo ela
    some, e o log de 5 em 5 segundos continua como antes. Avança também DENTRO de
    um arquivo grande (vídeo de 2 GiB não deixa a barra parada por minutos)."""

    def __init__(self) -> None:
        self.on = sys.stderr.isatty()
        self.ativa = False
        self.nome = ""
        self.total_bytes = 0
        self.total_arqs = 0
        self.feitos_bytes = 0      # bytes de arquivos concluídos neste grupo
        self.atual = 0             # bytes já lidos do arquivo em andamento
        self.idx = 0
        self.t0 = 0.0
        self.copiados_bytes = 0    # só o que veio de fato do celular (pra velocidade)
        self.tot: Totals | None = None
        self._ultimo = 0.0
        self._lock = threading.Lock()

    def inicio(self, nome: str, total_arqs: int, total_bytes: int, tot: "Totals") -> None:
        self.nome, self.total_arqs, self.total_bytes, self.tot = nome, total_arqs, total_bytes, tot
        self.feitos_bytes = self.atual = self.idx = self.copiados_bytes = 0
        self.t0 = time.time()
        self.ativa = self.on
        self.desenha(forcar=True)

    def arquivo(self, idx: int, feitos_bytes: int) -> None:
        self.idx, self.feitos_bytes, self.atual = idx, feitos_bytes, 0
        self.desenha()

    def baixou(self, nbytes: int) -> None:
        self.copiados_bytes += nbytes

    def parcial(self, atual: int) -> None:
        self.atual = atual
        self.desenha()

    def fim(self) -> None:
        if self.ativa:
            self.desenha(forcar=True)
            sys.stderr.write("\n")
            sys.stderr.flush()
        self.ativa = False

    def varrendo(self, nome: str, pastas: int, vistos: int, entram: int, t0: float) -> None:
        """Linha viva durante a varredura (que pode levar minutos se o MTP ficar lento)."""
        if not self.on:
            return
        now = time.time()
        if now - self._ultimo < 0.25:
            return
        self._ultimo = now
        dt = now - t0
        ritmo = f"  {vistos / dt:.0f} arq/s" if dt > 1 else ""
        linha = (f"🔍 varrendo {nome}: {pastas} pastas, {vistos} arquivos vistos, "
                 f"{entram} entram{ritmo}  ({hms(dt)})")
        cols = shutil.get_terminal_size((100, 20)).columns
        sys.stderr.write("\r\033[K" + linha[:cols - 1])
        sys.stderr.flush()

    def fim_varredura(self) -> None:
        if self.on:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    def limpa(self) -> None:
        if self.ativa:
            sys.stderr.write("\r\033[K")

    def desenha(self, forcar: bool = False) -> None:
        if not self.ativa:
            return
        now = time.time()
        if not forcar and now - self._ultimo < 0.25:
            return
        self._ultimo = now
        with self._lock:
            feito = min(self.feitos_bytes + self.atual, self.total_bytes)
            frac = feito / self.total_bytes if self.total_bytes else 1.0
            dt = now - self.t0
            veloc = (self.copiados_bytes + self.atual) / dt if dt > 0.3 else 0.0
            resta = (self.total_bytes - feito) / veloc if veloc > 0 else 0
            t = self.tot
            info = (f" {frac * 100:5.1f}%  {self.idx}/{self.total_arqs}  "
                    f"{human(feito)}/{human(self.total_bytes)}  {human(veloc)}/s  "
                    f"resta {hms(resta) if veloc else '?'}")
            if t is not None:
                info += f"  ✓{t.copied} ↷{t.skipped} ✗{t.failed}"
            cols = shutil.get_terminal_size((100, 20)).columns
            nome = self.nome if len(self.nome) <= 28 else self.nome[:27] + "…"
            largura = max(10, min(40, cols - len(info) - len(nome) - 5))
            cheio = int(frac * largura)
            barra = "█" * cheio + "░" * (largura - cheio)
            linha = f"{nome} [{barra}]{info}"
            sys.stderr.write("\r\033[K" + linha[:cols - 1])
            sys.stderr.flush()


BARRA = Barra()


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8", buffering=1)
        self.write(f"\n{'=' * 72}\nsessão iniciada {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'=' * 72}")

    def write(self, msg: str, echo: bool = True) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.fh.write(line + "\n")
        if echo:
            BARRA.limpa()                  # tira a barra, imprime, e ela volta embaixo
            print(line, flush=True)
            BARRA.desenha(forcar=True)

    def close(self) -> None:
        self.fh.close()


# ────────────────────────────── descoberta do celular ──────────────────────────────

def find_storages(explicit: str | None) -> list[Path]:
    """Raízes de armazenamento do aparelho (interno e cartão SD, se houver)."""
    if explicit:
        root = Path(explicit)
        if not root.is_dir():
            sys.exit(f"ERRO: '{root}' não existe ou não é um diretório.")
        return [root]

    if not GVFS_DIR.is_dir():
        sys.exit(f"ERRO: {GVFS_DIR} não existe. O gvfs está rodando? O celular está conectado?")

    mounts = sorted(p for p in GVFS_DIR.iterdir() if p.name.startswith("mtp:"))
    if not mounts:
        sys.exit(
            f"ERRO: nenhum dispositivo MTP montado em {GVFS_DIR}.\n"
            "  1) conecte o cabo USB\n"
            "  2) no celular escolha 'Transferência de arquivos (MTP)'\n"
            "  3) abra o dispositivo uma vez no gerenciador de arquivos"
        )

    storages: list[Path] = []
    for mount in mounts:
        try:
            children = [c for c in sorted(mount.iterdir()) if c.is_dir()]
        except OSError as exc:
            print(f"aviso: não consegui ler {mount}: {exc}", file=sys.stderr)
            continue
        storages.extend(children or [mount])
    if not storages:
        sys.exit("ERRO: dispositivo montado, mas sem armazenamento visível. Destrave a tela do celular.")
    return storages


def storage_label(storage: Path) -> str:
    """'Armazenamento interno' -> 'Armazenamento_interno' (nome de pasta seguro)."""
    label = storage.name.strip().replace("/", "_")
    for ch in '\0:*?"<>|':
        label = label.replace(ch, "_")
    return label.replace(" ", "_") or "armazenamento"


def alive(storage: Path) -> bool:
    try:
        with Timeout(30):
            os.listdir(storage)
        return True
    except (OSError, Stalled):
        return False


def wait_for_device(storage: Path, log: Log, tries: int = 30) -> bool:
    """O MTP caiu. Espera voltar — reconectar o cabo não perde progresso."""
    for i in range(1, tries + 1):
        if alive(storage):
            if i > 1:
                log.write("dispositivo respondendo de novo — continuando")
            return True
        log.write(f"dispositivo não responde ({i}/{tries}) — reconecte o cabo; nova tentativa em 20s")
        time.sleep(20)
    return False


# ─────────────────────────────────── manifesto ────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel        TEXT PRIMARY KEY,   -- <armazenamento>/caminho/relativo
    app        TEXT,               -- WhatsApp, Telegram, … ou '' se não for de app
    dest       TEXT,               -- caminho no destino (difere de rel se foi renomeado)
    size       INTEGER,
    mtime      REAL,
    status     TEXT,               -- pending | done | failed | skipped_nospace | missing
    bytes      INTEGER DEFAULT 0,
    digest     TEXT,
    attempts   INTEGER DEFAULT 0,
    error      TEXT,
    finished   REAL
);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);

-- Um registro por grupo já varrido: permite --fast-resume pular a varredura
-- (que no MTP custa caro) de grupos que já terminaram inteiros.
CREATE TABLE IF NOT EXISTS groups (
    key        TEXT PRIMARY KEY,   -- <armazenamento>|<prefixo>
    label      TEXT,
    files      INTEGER,
    bytes      INTEGER,
    status     TEXT,               -- complete | partial
    finished   REAL
);
"""


class Manifest:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        # Manifesto de versão anterior não tem a coluna dest: acrescenta sem perder nada.
        if "dest" not in {r[1] for r in self.db.execute("PRAGMA table_info(files)")}:
            self.db.execute("ALTER TABLE files ADD COLUMN dest TEXT")
        # O gargalo é o MTP, não o disco: durabilidade relaxada aqui é segura e bem mais rápida.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.commit()

    def status_of(self, rel: str) -> tuple[str, str | None] | None:
        return self.db.execute(
            "SELECT status, dest FROM files WHERE rel = ?", (rel,)).fetchone()

    def note(self, rel: str, app: str, size: int, mtime: float) -> None:
        self.db.execute(
            "INSERT INTO files (rel, app, size, mtime, status) VALUES (?, ?, ?, ?, 'pending') "
            "ON CONFLICT(rel) DO UPDATE SET app = excluded.app, size = excluded.size, "
            "mtime = excluded.mtime",
            (rel, app, size, mtime),
        )

    def finish(self, rel: str, status: str, nbytes: int = 0, digest: str | None = None,
               attempts: int = 0, error: str | None = None, dest: str | None = None) -> None:
        self.db.execute(
            "UPDATE files SET status = ?, bytes = ?, digest = ?, attempts = ?, error = ?, "
            "dest = ?, finished = ? WHERE rel = ?",
            (status, nbytes, digest, attempts, error, dest, time.time(), rel),
        )
        self.db.commit()

    def reset_failed(self) -> int:
        cur = self.db.execute(
            "UPDATE files SET status = 'pending', attempts = 0, error = NULL "
            "WHERE status IN ('failed', 'skipped_nospace')"
        )
        self.db.commit()
        return cur.rowcount

    def pending(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT rel FROM files WHERE status = 'pending'")}

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM files GROUP BY status"))

    def by_app(self):
        return self.db.execute(
            "SELECT COALESCE(NULLIF(app, ''), '(resto do celular)'), COUNT(*), COALESCE(SUM(bytes), 0) "
            "FROM files WHERE status = 'done' GROUP BY 1 ORDER BY 3 DESC"
        )

    def total_done(self) -> int:
        return self.db.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM files WHERE status = 'done'"
        ).fetchone()[0]

    def group_status(self, key: str) -> str | None:
        row = self.db.execute("SELECT status FROM groups WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def mark_group(self, key: str, label: str, files: int, nbytes: int, status: str) -> None:
        self.db.execute(
            "INSERT INTO groups (key, label, files, bytes, status, finished) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET label = excluded.label, files = excluded.files, "
            "bytes = excluded.bytes, status = excluded.status, finished = excluded.finished",
            (key, label, files, nbytes, status, time.time()),
        )
        self.db.commit()

    def group_rows(self):
        return self.db.execute(
            "SELECT label, files, bytes, status, finished FROM groups ORDER BY finished"
        )

    def group_pending(self, prefix: str) -> tuple[int, int]:
        """(arquivos, bytes) ainda não copiados sob um prefixo. Varre pela PK, sem LIKE."""
        return self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files "
            "WHERE rel >= ? AND rel < ? AND status NOT IN ('done', 'missing')",
            (prefix, prefix + "￿"),
        ).fetchone()

    def rows(self, status: str | None = None):
        sql = "SELECT rel, app, dest, size, bytes, status, digest, error FROM files"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        return self.db.execute(sql + " ORDER BY rel", args)

    def commit(self) -> None:
        self.db.commit()


# ─────────────────────────── varredura (um grupo por vez) ───────────────────────────

@dataclass
class Item:
    src: Path         # caminho pelo ponto de montagem (fallback)
    rel: str          # caminho relativo, já com o rótulo do armazenamento na frente
    inside: str       # caminho relativo dentro do armazenamento
    size: int
    mtime: float
    app: str
    gfile: object = None   # Gio.File do arquivo, quando o gvfs nativo está disponível
    suspeito: bool = False # extensão de fragmento: confirmar pelo conteúdo antes de copiar


@dataclass
class Filtro:
    """O que entra na cópia. Junto num objeto porque atravessa toda a varredura."""
    only: list[str] | None = None
    excluir: list[str] | None = None   # --excluir: pastas que não entram, nem são varridas
    apps_only: bool = False
    skip_data: bool = False
    keep_frag: bool = False      # --fragmentos: traz de volta o que não abre sozinho
    desde: float | None = None   # --desde: só arquivos modificados a partir deste instante


@dataclass
class Totals:
    files: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    renamed: int = 0
    rescued: int = 0
    discarded: int = 0
    discarded_bytes: int = 0
    bytes: int = 0
    started: float = field(default_factory=time.time)
    out_of_space: bool = False
    disconnected: bool = False


def rank_of(inside: str) -> int:
    for i, prefix in enumerate(PRIORITY):
        if prefix != "__OUTROS__" and under(inside, prefix):
            return i
    return OUTROS


def app_of(inside: str) -> str:
    for app, prefixes in APPS.items():
        if any(under(inside, p) for p in prefixes):
            return app
    return ""


def group_label(index: int) -> str:
    prefix = PRIORITY[index]
    if prefix == "__OUTROS__":
        return "resto da raiz"
    app = app_of(prefix)
    return f"{app}: {prefix}" if app else prefix


def descartavel(inside: str, size: int) -> str | None:
    """Motivo pra descartar já na varredura, ou None. Só o que é certeza sem abrir:
    arquivo de 0 byte e sobra de sistema. Nada de mídia sai por aqui."""
    # 0 byte sai sempre, mesmo em app integral: nao ha conteudo pra salvar, e o
    # libmtp nem consegue buscar objeto de tamanho zero — cada um custava 4
    # tentativas e ~14 s de falha. Medido no cache do BuzzCast: 589 de 633.
    if size == 0:
        return "vazio (0 byte)"
    if app_of(inside) in APPS_INTEGRAIS:
        return None
    low = inside.rsplit("/", 1)[-1].lower()
    if low in FRAG_NOME or low.startswith(FRAG_PREFIXO):
        return "sobra de sistema"
    return None


def suspeito_frag(inside: str) -> bool:
    """A extensão sugere pedaço de download. Só suspeita: a decisão sai depois,
    lendo a assinatura do arquivo — app de conversa grava mídia com extensão
    qualquer, e jogar fora vídeo de verdade seria muito pior que copiar um pedaço."""
    if app_of(inside) in APPS_INTEGRAIS:
        return False
    return os.path.splitext(inside.rsplit("/", 1)[-1].lower())[1] in FRAG_EXT


def wanted_file(inside: str, f: Filtro) -> bool:
    if f.excluir and any(under(inside, e) for e in f.excluir):
        return False
    if f.apps_only and not app_of(inside):
        return False
    if f.only and not any(under(inside, o) for o in f.only):
        return False
    if f.skip_data and under(inside, "Android/data") and not app_of(inside):
        return False
    return True


def wanted_dir(inside: str, group: int, f: Filtro) -> bool:
    """Vale descer nesta pasta enquanto processo `group`?"""
    if f.excluir and any(under(inside, e) for e in f.excluir):
        return False
    # Subtree que pertence a um grupo mais específico já foi (ou será) tratado nele.
    if rank_of(inside) != group:
        return False
    if f.apps_only and not (app_of(inside) or any(under(p, inside) for p in APP_PREFIXES)):
        return False
    if f.skip_data and under(inside, "Android/data") and not app_of(inside):
        return False
    if f.only and not any(under(inside, o) or under(o, inside) for o in f.only):
        return False
    return True


USE_GIO = True          # desligado por --no-gio
# Pastas do celular listadas com sucesso nesta sessão. Só dá para afirmar que um
# arquivo "sumiu" se a pasta dele foi de fato lida — celular bloqueado ou recém
# reiniciado devolve armazenamento vazio, e isso não pode virar "apagado".
PASTAS_LISTADAS: set[str] = set()


def storage_gfile(storage: Path):
    """Gio.File da raiz do armazenamento, ou None se não houver como falar direto
    com o gvfs (sem PyGObject, --no-gio, ou --source numa pasta comum)."""
    if not (HAVE_GIO and USE_GIO):
        return None
    mount = storage.parent.name                     # ex.: mtp:host=SAMSUNG_..._RQ8TB0196ZH
    if not mount.startswith("mtp:host="):
        return None
    return Gio.File.new_for_uri(f"mtp://{mount[len('mtp:host='):]}/").get_child(storage.name)


# Uma entrada de diretório, no formato comum às duas varreduras.
Entry = tuple[str, bool, int, float]                # nome, é_pasta, tamanho, mtime


def list_gio(gdir, inside_dir: str) -> list[Entry]:
    """Lista um diretório pelo gvfs nativo: nome, tipo, tamanho e data numa só
    ida ao aparelho (o FUSE gastaria um stat por arquivo)."""
    cancel = Gio.Cancellable()
    out: list[Entry] = []
    with Watchdog(cancel, LIST_TIMEOUT) as wd:
        try:
            children = gdir.enumerate_children(
                "standard::name,standard::type,standard::size,time::modified",
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, cancel)
            while True:
                info = children.next_file(cancel)
                if info is None:
                    break
                wd.touch()
                out.append((info.get_name(),
                            info.get_file_type() == Gio.FileType.DIRECTORY,
                            info.get_size(),
                            float(info.get_attribute_uint64("time::modified"))))
            children.close(None)
        except GLib.Error as exc:
            raise gio_error(exc, wd, f"listar {inside_dir or '/'}") from exc
    return out


def list_fuse(path: Path, inside_dir: str, log: Log) -> list[Entry]:
    """Fallback: lista pelo ponto de montagem. Um stat por arquivo, bem mais lento."""
    out: list[Entry] = []
    with Timeout(LIST_TIMEOUT):
        entries = list(os.scandir(path))
    for entry in entries:
        try:
            with Timeout(LIST_TIMEOUT):
                st = entry.stat(follow_symlinks=False)
                out.append((entry.name, entry.is_dir(follow_symlinks=False),
                            st.st_size, st.st_mtime))
        except (OSError, Stalled) as exc:
            log.write(f"  aviso: stat falhou em {inside_dir}/{entry.name}: {exc}")
    return out


def group_exists(root, storage: Path, prefix: str) -> bool:
    """A pasta do grupo existe no aparelho?

    Pelo gvfs nativo custa ~0,01 s; pelo FUSE cada teste custa dezenas de segundos
    (medido neste aparelho: 22 s, 30 s e 55 s), e a maioria dos 63 prefixos de app
    não existe — era meia hora de espera antes de copiar o primeiro arquivo."""
    if root is not None:
        gdir = root
        for part in prefix.split("/"):
            gdir = gdir.get_child(part)
        cancel = Gio.Cancellable()
        try:
            with Watchdog(cancel, LIST_TIMEOUT):
                info = gdir.query_info("standard::type",
                                       Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, cancel)
            return info.get_file_type() == Gio.FileType.DIRECTORY
        except GLib.Error:
            return False
    try:
        with Timeout(LIST_TIMEOUT):
            return (storage / prefix).is_dir()
    except (OSError, Stalled):
        return False


def collect_group(storage: Path, group: int, log: Log,
                  f: Filtro) -> tuple[list[Item], list[tuple[str, int, str]]]:
    """Varre só o subtree de um grupo, do mais raso pro mais fundo.

    Devolve (o que copiar, o que ficou de fora com motivo)."""
    prefix = PRIORITY[group]
    label = storage_label(storage)
    root = storage_gfile(storage)

    if prefix != "__OUTROS__":
        if f.only and not any(under(prefix, o) or under(o, prefix) for o in f.only):
            return [], []
        if not group_exists(root, storage, prefix):
            return [], []

    items: list[Item] = []
    fora: list[tuple[str, int, str]] = []
    stack = [("" if prefix == "__OUTROS__" else prefix)]
    nome = group_label(group)
    t0 = time.time()
    try:
        _varre(stack, storage, root, group, log, f, label, items, fora,
               lambda n: BARRA.varrendo(nome, *n, len(items), t0))
    finally:
        BARRA.fim_varredura()
    items.sort(key=lambda it: it.inside)
    return items, fora


def _varre(stack, storage, root, group, log, f, label, items, fora, mostra) -> None:
    pastas = vistos = 0
    while stack:
        inside_dir = stack.pop()
        pastas += 1
        path = storage / inside_dir if inside_dir else storage
        gdir = None
        if root is not None:
            gdir = root
            for part in inside_dir.split("/") if inside_dir else []:
                gdir = gdir.get_child(part)
        try:
            entries = (list_gio(gdir, inside_dir) if gdir is not None
                       else list_fuse(path, inside_dir, log))
        except (OSError, Stalled, FileNotFoundError) as exc:
            log.write(f"  aviso: não listei {inside_dir or '/'}: {exc}")
            continue

        PASTAS_LISTADAS.add(inside_dir)
        vistos += sum(1 for e in entries if not e[1])
        mostra((pastas, vistos))
        subdirs: list[str] = []
        for name, is_dir, size, mtime in sorted(entries):
            inside = f"{inside_dir}/{name}" if inside_dir else name
            if is_dir:
                if wanted_dir(inside, group, f):
                    subdirs.append(inside)
                continue
            if rank_of(inside) != group or not wanted_file(inside, f):
                continue
            if f.desde is not None and mtime < f.desde:
                continue
            motivo = None if f.keep_frag else descartavel(inside, size)
            if motivo:
                fora.append((inside, size, motivo))
                continue
            items.append(Item(src=path / name, rel=f"{label}/{inside}", inside=inside,
                              size=size, mtime=mtime, app=app_of(inside),
                              gfile=gdir.get_child(name) if gdir is not None else None,
                              suspeito=not f.keep_frag and suspeito_frag(inside)))
        stack.extend(reversed(subdirs))


# ─────────────────────────────────── cópia ───────────────────────────────────

def safe_dest(dest_root: Path, rel: str) -> Path:
    """Destino, encurtando componente longo demais para o ext4 (mantém a extensão)."""
    parts = []
    for part in rel.split("/"):
        raw = part.encode("utf-8", "surrogateescape")
        if len(raw) > MAX_NAME_BYTES:
            stem, dot, ext = part.rpartition(".")
            ext = f".{ext}" if dot and len(ext) <= 12 else ""
            keep = MAX_NAME_BYTES - len(ext.encode()) - 12
            trimmed = raw[:keep].decode("utf-8", "ignore")
            part = f"{trimmed}~{hashlib.sha1(raw).hexdigest()[:8]}{ext}"
        parts.append(part)
    return dest_root.joinpath(*parts)


def mes_de(nome: str, mtime: float) -> str:
    """AAAA-MM da foto: a data do nome é a da captura; o mtime só se o nome não tiver."""
    m = DATA_NO_NOME.search(nome)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return time.strftime("%Y-%m", time.localtime(mtime))


def organizar_rel(rel: str, mtime: float) -> str:
    """Caminho relativo no destino: <rótulo>/DCIM/Camera/x.jpg vira .../Camera/AAAA-MM/x.jpg.
    Fora das pastas de POR_MES (ou em subpasta delas) devolve o próprio rel."""
    label, _, inside = rel.partition("/")
    pasta, _, nome = inside.rpartition("/")
    if not nome or pasta not in POR_MES:
        return rel
    return f"{label}/{pasta}/{mes_de(nome, mtime)}/{nome}"


def already_there(dst: Path, item: Item) -> bool:
    try:
        return dst.stat().st_size == item.size
    except OSError:
        return False


def sniff_bytes(head: bytes) -> str | None:
    """Extensão de verdade a partir dos primeiros bytes. None = não reconheci."""
    if head[4:8] == b"ftyp":                       # MP4/MOV/3GP/HEIC
        return ".mp4"
    if head[:4] == b"RIFF":
        return ".webp" if head[8:12] == b"WEBP" else ".wav"
    for magic, ext in ASSINATURAS:
        if head.startswith(magic):
            return ext
    return None


def sniff_ext(path: Path) -> str | None:
    """Assinatura de um arquivo já baixado (leitura local, de graça)."""
    try:
        with open(path, "rb") as fh:
            return sniff_bytes(fh.read(16))
    except OSError:
        return None


def head_no_celular(item: Item, n: int = 16) -> bytes:
    """Primeiros bytes do arquivo ainda no celular. Uma ida ao aparelho, só pra
    decidir se vale baixar o resto — não dá pra confiar na extensão."""
    if item.gfile is not None:
        cancel = Gio.Cancellable()
        with Watchdog(cancel, LIST_TIMEOUT) as wd:
            try:
                stream = item.gfile.read(cancel)
                data = stream.read_bytes(n, cancel).get_data()
                stream.close(None)
                return data
            except GLib.Error as exc:
                raise gio_error(exc, wd, f"ler início de {item.inside}") from exc
    with Timeout(CHUNK_TIMEOUT):
        with open(item.src, "rb", buffering=0) as fh:
            return fh.read(n)


def head_local(path: Path, n: int = 16) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def veredito_conteudo(head: bytes, size: int) -> tuple[str | None, str | None]:
    """(extensão real, motivo de recusa). Recusa o que não dá pra abrir: conteúdo
    irreconhecível, ou container de vídeo/áudio pequeno demais pra ter mídia dentro."""
    real = sniff_bytes(head)
    if real is None:
        return None, "conteúdo não reconhecido"
    if FAMILIA.get(real) in ("video", "audio") and size < MIN_MEDIA_BYTES:
        return real, f"só o cabeçalho {real} ({human(size)}, sem mídia dentro)"
    return real, None


def deve_acrescentar(ext: str, real: str) -> bool:
    """Vale acrescentar a extensão real? Sim quando a atual não diz nada, ou quando
    ela mente sobre a família (extensão de imagem em arquivo de vídeo, o caso do
    BuzzCast). Se as duas concordam — .opus lido como Ogg, .mkv como Matroska —
    fica quieto, senão o backup viraria uma sopa de nomes duplos."""
    if ext in OPACO or ext in FRAG_EXT or ext[1:].isdigit():   # .0, .1, .2… do Glide
        return True
    atual, conteudo = FAMILIA.get(ext), FAMILIA.get(real)
    return atual is not None and conteudo is not None and atual != conteudo


def final_dest(dst: Path, tmp: Path, renomear: bool) -> Path:
    """Acrescenta a extensão real quando o nome não diz o que é, ou diz errado.
    Nunca substitui a original: '<hash>.0' vira '<hash>.0.jpg'."""
    if not renomear:
        return dst
    real = sniff_ext(tmp)
    if real and deve_acrescentar(os.path.splitext(dst.name)[1].lower(), real):
        return dst.with_name(dst.name + real)
    return dst


def landed(manifest: "Manifest", dest_root: Path, item: Item) -> Path | None:
    """Onde o arquivo já está no destino, se já veio íntegro. None se falta copiar.

    Confere o caminho gravado no manifesto antes do caminho espelho, porque um
    arquivo renomeado pela assinatura não está onde o espelho diria."""
    row = manifest.status_of(item.rel)
    known = [dest_root / row[1]] if row and row[1] else []
    org = safe_dest(dest_root, organizar_rel(item.rel, item.mtime))
    # Organizado antes do espelho plano: é onde o arquivo mora agora; o plano só
    # vale para cópia antiga que ainda não passou pelo --organizar.
    for cand in known + [org, safe_dest(dest_root, item.rel)]:
        if already_there(cand, item):
            return cand
    return None


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _progresso_gio(wd: "Watchdog"):
    def cb(*args):
        wd.touch()
        try:
            BARRA.parcial(int(args[0]))
        except Exception:          # exceção em callback C some; não deixa cegar o watchdog
            pass
    return cb


def fetch_gio(item: Item, tmp: Path) -> int:
    """Baixa pelo backend nativo do gvfs (~2x mais rápido que o FUSE neste aparelho).
    A thread-vigia cancela se o MTP parar de entregar bytes."""
    cancel = Gio.Cancellable()
    with Watchdog(cancel, CHUNK_TIMEOUT) as wd:
        try:
            # O callback recebe (atual, total, user_data): assinatura frouxa de
            # propósito, porque exceção dentro de callback C é engolida — um
            # TypeError aqui deixaria o watchdog cego sem ninguém notar.
            item.gfile.copy(Gio.File.new_for_path(str(tmp)),
                            Gio.FileCopyFlags.OVERWRITE, cancel,
                            _progresso_gio(wd), None)
        except GLib.Error as exc:
            raise gio_error(exc, wd, f"copiar {item.inside}") from exc
    return tmp.stat().st_size


def fetch_fuse(item: Item, tmp: Path) -> int:
    """Fallback pelo ponto de montagem, com watchdog por bloco lido (SIGALRM
    funciona aqui porque read() é syscall e volta com EINTR)."""
    written = 0
    with open(item.src, "rb", buffering=0) as fsrc, open(tmp, "wb") as fdst:
        while True:
            with Timeout(CHUNK_TIMEOUT):
                block = fsrc.read(CHUNK)
            if not block:
                break
            fdst.write(block)
            written += len(block)
            BARRA.parcial(written)
        fdst.flush()
        os.fsync(fdst.fileno())
    return written


def copy_one(item: Item, dst: Path, verify: str,
             renomear: bool = True) -> tuple[int, str | None, Path]:
    """Copia um arquivo. Devolve (bytes escritos, md5 ou None, caminho final).

    Sempre grava em .part e só renomeia depois de conferir o tamanho: se faltar
    energia ou o cabo sair, não sobra arquivo truncado passando por bom.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")

    written = fetch_gio(item, tmp) if item.gfile is not None else fetch_fuse(item, tmp)

    if item.size and written != item.size:
        tmp.unlink(missing_ok=True)
        raise OSError(errno.EIO, f"tamanho divergente: vieram {written} de {item.size} bytes")

    digest = None
    if verify == "md5":
        # Relê a origem e compara com o que foi gravado: é o único jeito de pegar
        # corrupção silenciosa no caminho. Por isso este modo lê tudo duas vezes.
        digest = md5_of(tmp)
        with Timeout(CHUNK_TIMEOUT * 2):
            origin = md5_of(item.src)
        if origin != digest:
            tmp.unlink(missing_ok=True)
            raise OSError(errno.EIO, "md5 do destino não bate com o da origem")

    dst = final_dest(dst, tmp, renomear)
    os.replace(tmp, dst)
    try:
        os.utime(dst, (item.mtime, item.mtime))
    except OSError:
        pass
    return written, digest, dst


def versao_livre(dst: Path, log: "Log") -> Path:
    """Caminho livre para não escrever por cima de cópia antiga de tamanho diferente.

    O app reescreve arquivo no mesmo caminho: vídeo de conversa que o celular
    esvaziou (0 byte) apagaria a cópia boa de 70 MB, e asset baixado pela metade
    viraria a versão definitiva. Aqui o antigo fica e o novo entra como .v2, .v3…
    """
    if not dst.exists():
        return dst
    stem, dot, ext = dst.name.rpartition(".")
    if not dot:
        stem, ext = dst.name, ""
    for n in range(2, 1000):
        cand = dst.with_name(f"{stem}.v{n}" + (f".{ext}" if ext else ""))
        if not cand.exists():
            log.write(f"  preservado o antigo, novo salvo como {cand.name}: {dst.name}")
            return cand
    return dst


def copy_group(items: list[Item], name: str, dest_root: Path, manifest: Manifest, log: Log,
               storage: Path, verify: str, dry: bool, space_guard: bool,
               renomear: bool, tot: Totals,
               descartados: list[tuple[str, int, str]] | None = None,
               incremental: bool = False) -> str:
    """Copia um grupo inteiro. Devolve 'complete' ou 'partial'."""
    group_bytes = sum(i.size for i in items)
    log.write(f"▶ {name}: {len(items)} arquivos, {human(group_bytes)}"
              + ("   [DRY-RUN]" if dry else ""))
    if not dry:
        BARRA.inicio(name, len(items), group_bytes, tot)
    try:
        return _copy_group_loop(items, group_bytes, dest_root, manifest, log, storage, verify,
                                dry, space_guard, renomear, tot, descartados, incremental)
    finally:
        BARRA.fim()


def _copy_group_loop(items, group_bytes, dest_root, manifest, log, storage, verify, dry,
                     space_guard, renomear, tot, descartados, incremental) -> str:
    group_done = 0
    group_failed = 0
    last_report = 0.0
    for idx, item in enumerate(items, 1):
        BARRA.arquivo(idx, group_done)
        dst = safe_dest(dest_root, organizar_rel(item.rel, item.mtime))

        hit = landed(manifest, dest_root, item)   # sessão anterior já trouxe este arquivo?

        # Extensão de fragmento: quem decide é o conteúdo, não o nome. Se o arquivo
        # já está aqui, lê os bytes locais (de graça); se não, lê só o começo no
        # celular antes de baixar o resto. Vem antes da retomada de propósito: assim
        # cópia feita sob política antiga é reavaliada e limpa sozinha.
        if item.suspeito and not dry:
            try:
                head = head_local(hit) if hit is not None else head_no_celular(item)
                real, recusa = veredito_conteudo(head, item.size)
            except (OSError, Stalled, FileNotFoundError) as exc:
                real, recusa = None, f"não li o início ({exc})"
            if recusa:
                if hit is not None:
                    hit.unlink(missing_ok=True)
                    log.write(f"  removido do backup (política atual recusa): {item.inside}")
                if descartados is not None:
                    ext = os.path.splitext(item.inside)[1].lower()
                    descartados.append((item.rel, item.size, f"fragmento {ext}: {recusa}"))
                manifest.finish(item.rel, "descartado", 0, error=recusa)
                tot.discarded += 1
                tot.discarded_bytes += item.size
                group_done += item.size
                continue
            if hit is None:
                tot.rescued += 1
                log.write(f"  {item.inside}: extensão engana, conteúdo é {real} — copiando",
                          echo=not BARRA.ativa)   # milhares de linhas: só no arquivo

        if hit is not None:
            row = manifest.status_of(item.rel)
            onde = str(hit.relative_to(dest_root))
            # row[1] != onde: o arquivo foi movido no disco (ex.: para a pasta do mês);
            # o manifesto passa a apontar para onde ele está de fato.
            if not dry and (row is None or row[0] != "done" or row[1] != onde):
                manifest.finish(item.rel, "done", item.size, dest=onde)
            tot.skipped += 1
            tot.bytes += item.size
            group_done += item.size
            continue

        if dry:
            # flush: sem isso a saída redirecionada fica no buffer e se perde
            # se a sessão for interrompida — justo quando você quer ver onde parou.
            print(f"  COPIARIA  {item.rel}  ({human(item.size)})", flush=True)
            group_done += item.size
            continue

        if space_guard:
            free = shutil.disk_usage(dest_root).free
            if free - item.size < RESERVE_BYTES:
                manifest.finish(item.rel, "skipped_nospace", 0, error=f"livres {human(free)}")
                log.write(f"SEM ESPAÇO ({human(free)} livres) — parando antes de lotar o disco.")
                log.write("Libere espaço ou use --dest em outro disco e rode de novo: "
                          "o progresso está salvo no manifesto.")
                tot.out_of_space = True
                return "partial"

        if incremental:
            dst = versao_livre(dst, log)

        error: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                nbytes, digest, final = copy_one(item, dst, verify, renomear)
                manifest.finish(item.rel, "done", nbytes, digest, attempt,
                                dest=str(final.relative_to(dest_root)))
                if final != dst:
                    tot.renamed += 1
                tot.copied += 1
                tot.bytes += nbytes
                group_done += nbytes
                BARRA.baixou(nbytes)
                error = None
                break
            except FileNotFoundError:
                manifest.finish(item.rel, "missing", 0, attempts=attempt,
                                error="arquivo desapareceu do celular durante a cópia")
                error = None
                break
            except (OSError, Stalled) as exc:
                error = f"{type(exc).__name__}: {exc}"
                dst.with_name(dst.name + ".part").unlink(missing_ok=True)
                retryable = isinstance(exc, Stalled) or getattr(exc, "errno", None) in RETRYABLE
                # O celular lista o arquivo mas não entrega: índice MTP velho (arquivo
                # apagado/movido depois da conexão). Insistir não muda nada — 2 tentativas
                # bastam; reconectar o cabo e --retry-failed é o que resolve.
                if MTP_RECUSA in str(exc) and attempt >= 2:
                    error += "  [celular recusou: reconecte o cabo e rode --retry-failed]"
                    break
                if not retryable or attempt == MAX_ATTEMPTS:
                    break
                if not alive(storage) and not wait_for_device(storage, log):
                    log.write("celular não voltou. Encerrando — reconecte e rode de novo.")
                    manifest.finish(item.rel, "failed", 0, attempts=attempt, error=error)
                    manifest.commit()
                    tot.disconnected = True
                    return "partial"
                wait = min(2 ** attempt, 30)
                log.write(f"  retry {attempt}/{MAX_ATTEMPTS} em {wait}s — {item.rel}: {error}")
                time.sleep(wait)
        if error:
            manifest.finish(item.rel, "failed", 0, attempts=MAX_ATTEMPTS, error=error)
            tot.failed += 1
            group_failed += 1
            log.write(f"FALHOU  {item.rel}  →  {error}")

        now = time.time()
        if now - last_report >= 5 or idx == len(items):
            last_report = now
            elapsed = now - tot.started
            rate = tot.bytes / elapsed if elapsed > 0.5 else 0
            eta = (group_bytes - group_done) / rate if rate > 0 else 0
            pct = 100 * group_done / group_bytes if group_bytes else 100
            # Com a barra na tela, a linha de 5 s vai só pro arquivo de log.
            log.write(f"  {pct:5.1f}%  {idx}/{len(items)}  {human(group_done)}/{human(group_bytes)}"
                      f"  {human(rate)}/s  resta ~{hms(eta)}  "
                      f"(novos {tot.copied}, já tinha {tot.skipped}, falhas {tot.failed})",
                      echo=not BARRA.ativa)

    BARRA.arquivo(len(items), group_done)

    manifest.commit()
    return "partial" if group_failed else "complete"


# ─────────────────────────────────── relatórios ───────────────────────────────────

def write_resume_point(manifest: Manifest, logs_dir: Path, dest_root: Path,
                       log: Log, note: str) -> Path:
    """Escreve _logs/RETOMAR.txt: onde a cópia parou e o comando exato pra continuar depois."""
    path = logs_dir / "RETOMAR.txt"
    counts = manifest.counts()
    pend_files = counts.get("pending", 0) + counts.get("failed", 0) + counts.get("skipped_nospace", 0)

    lines = [
        "PONTO DE RETOMADA DO BACKUP",
        f"gerado em      {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"situação       {note}",
        f"destino        {dest_root}",
        "",
        f"já no destino  {counts.get('done', 0)} arquivos, {human(manifest.total_done())}",
        f"pendentes      {pend_files} arquivos",
        f"falhas         {counts.get('failed', 0)}",
        f"sem espaço     {counts.get('skipped_nospace', 0)}",
        f"sumiram        {counts.get('missing', 0)}   (apagados no celular durante a cópia)",
        "",
        "COPIADO POR APP",
    ]
    for app, n, sz in manifest.by_app():
        lines.append(f"  {app:<22} {n:>7} arquivos  {human(sz)}")

    lines += ["", "GRUPOS JÁ VARRIDOS  (os que não aparecem ainda não foram tocados)"]
    for label, files, nbytes, status, finished in manifest.group_rows():
        mark = "ok     " if status == "complete" else "parcial"
        when = time.strftime("%d/%m %H:%M", time.localtime(finished)) if finished else "?"
        lines.append(f"  [{mark}] {when}  {label:<44} {files:>7} arq  {human(nbytes)}")

    lines += [
        "",
        "PARA CONTINUAR DEPOIS",
        f"  cd {dest_root}",
        "  python3 backup_celular_samsung.py                # continua de onde parou (revarre tudo, mais seguro)",
        "  python3 backup_celular_samsung.py --fast-resume  # não revarre grupos já concluídos (bem mais rápido)",
        "  python3 backup_celular_samsung.py --retry-failed # tenta de novo só o que falhou",
        "  python3 backup_celular_samsung.py --status       # relê esta situação sem mexer no celular",
        "",
        "O que já veio nunca é recopiado: o manifesto (_logs/manifesto.sqlite3) guarda cada",
        "arquivo com tamanho e status, e a cópia confere o destino antes de baixar de novo.",
        "Pode desconectar o cabo, desligar o PC e voltar semanas depois.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.write(f"ponto de retomada salvo em {path}")
    return path


def pendencia_importa(rel: str) -> bool:
    r = "/" + rel
    if r.rsplit("/", 1)[-1].lower() in FRAG_NOME:     # .nomedia e afins: marcador vazio
        return False
    return not any(p in r for p in DESCARTAVEL_SE_FALTAR)


MARCA_COMPLETA = "ultima_conferencia_completa.txt"


def marcar_ausentes(manifest: Manifest, vistos: set[str], f: Filtro,
                    desde: float | None, log: Log) -> None:
    """Pendência que a varredura de agora não encontrou no celular (numa pasta que
    foi varrida) não vai mais chegar: o arquivo foi apagado ou movido no aparelho.
    Vira 'ausente_no_celular' — fora da conta do 'pode formatar', mas listada em
    PERDIDOS.txt, porque também NÃO está no backup."""
    n = 0
    if not vistos:
        log.write("aviso: a varredura não viu nenhum arquivo — celular bloqueado? "
                  "Nada foi marcado como ausente.")
        return
    for (rel, mtime) in list(manifest.db.execute(
            "SELECT rel, mtime FROM files WHERE status IN ('pending','failed')")):
        if rel in vistos:
            continue
        inside = rel.split("/", 1)[1] if "/" in rel else rel
        pasta = inside.rsplit("/", 1)[0] if "/" in inside else ""
        if pasta not in PASTAS_LISTADAS:
            continue                     # pasta não foi lida: não dá para concluir nada
        if f.only and not any(under(inside, o) for o in f.only):
            continue                     # pasta não foi varrida nesta sessão
        if f.excluir and any(under(inside, e) for e in f.excluir):
            continue
        if f.apps_only and not app_of(inside):
            continue
        if desde is not None and (mtime or 0) < desde:
            continue                     # filtro de data escondeu, não sumiu
        manifest.db.execute("UPDATE files SET status='ausente_no_celular', "
                            "error='não encontrado no celular na varredura' WHERE rel=?", (rel,))
        n += 1
    manifest.commit()
    if n:
        log.write(f"{n} pendências não existem mais no celular — marcadas como "
                  f"'ausente_no_celular' (ver PERDIDOS.txt)")


def write_prontidao(manifest: Manifest, logs_dir: Path, log: Log) -> bool:
    """PODE_FORMATAR.txt: dá pra apagar o celular? Devolve True se sim."""
    pend = list(manifest.rows("failed")) + list(manifest.rows("skipped_nospace")) \
        + list(manifest.rows("pending"))
    importantes = [r for r in pend if pendencia_importa(r[0])]
    lixo = [r for r in pend if not pendencia_importa(r[0])]
    marca = logs_dir / MARCA_COMPLETA
    completa = marca.read_text(encoding="utf-8").strip() if marca.exists() else None
    ok = not importantes and completa is not None

    out = logs_dir / "PODE_FORMATAR.txt"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"gerado em {time.strftime('%Y-%m-%d %H:%M')}\n\n")
        fh.write(("VEREDITO: PODE FORMATAR (arquivos) — confira também a lista abaixo de\n"
                  "          coisas que NÃO são arquivos e não vêm por MTP.\n") if ok else
                 "VEREDITO: AINDA NÃO\n")
        fh.write(f"\nconferência completa (varredura sem --desde/--only/--apps): "
                 f"{completa or 'NUNCA FEITA — rode: python3 backup_celular_samsung.py'}\n")
        fh.write(f"pendências importantes: {len(importantes)}\n")
        for rel, _a, _d, size, _b, st, _g, err in importantes:
            fh.write(f"    {st}\t{human(size)}\t{rel}\t{err}\n")
        fh.write(f"pendências descartáveis (cache, temporários, status): {len(lixo)}\n")
        fh.write("\nNÃO vem por MTP — faça antes de formatar:\n"
                 "  - WhatsApp: Configurações > Conversas > Backup (Google Drive)\n"
                 "  - contatos, SMS, chamadas, agenda: Smart Switch ou conta Google\n"
                 "  - app autenticador (2FA): exportar/transferir contas\n"
                 "  - senhas salvas, apps de banco (vão pedir novo cadastro)\n")
    ausentes = list(manifest.rows("ausente_no_celular"))
    if ausentes:
        perd = logs_dir / "PERDIDOS.txt"
        with open(perd, "w", encoding="utf-8") as fh:
            fh.write("# Estavam no celular em backup anterior, NÃO estão mais no celular\n"
                     "# e NÃO estão neste backup. Formatar não muda nada para eles —\n"
                     "# mas se existirem em outro lugar (nuvem, outro disco), é lá.\n")
            fh.write(f"# total: {len(ausentes)} arquivos, "
                     f"{human(sum(r[3] or 0 for r in ausentes))}\n")
            for rel, _a, _d, size, *_ in ausentes:
                fh.write(f"{human(size)}\t{rel}\n")
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"\nATENÇÃO: {len(ausentes)} arquivos ({human(sum(r[3] or 0 for r in ausentes))}) "
                     f"não estão nem no celular nem no backup — lista em {perd}\n")
    log.write(f"prontidão para formatar: {'SIM' if ok else 'AINDA NÃO'} "
              f"({len(importantes)} pendências importantes, {len(lixo)} descartáveis"
              f"{'' if completa else ', conferência completa nunca feita'}) — {out}")
    return ok


def conferir_backup(manifest: Manifest, dest_root: Path, log: Log) -> int:
    """Sem tocar no celular: cada arquivo que o manifesto diz que veio está mesmo no
    disco, com o tamanho certo e sem começar zerado? O que não estiver vira 'failed',
    e --retry-failed busca de novo. Pega estrago de mover pasta, disco cheio, NTFS sujo."""
    rows = list(manifest.db.execute(
        "SELECT rel, dest, size, bytes FROM files WHERE status = 'done'"))
    ruins = 0
    t0 = time.time()
    BARRA.inicio("conferindo o disco", len(rows), sum(r[2] or 0 for r in rows), Totals())
    feitos = 0
    try:
        for i, (rel, dest, size, nbytes) in enumerate(rows, 1):
            BARRA.arquivo(i, feitos)
            feitos += size or 0
            path = dest_root / dest if dest else safe_dest(dest_root, rel)
            problema = None
            try:
                st = path.stat()
                if st.st_size != (nbytes or size):
                    problema = f"tamanho {st.st_size} != {nbytes or size}"
                elif st.st_size >= 16:
                    with open(path, "rb") as fh:
                        if fh.read(16) == bytes(16) and sniff_ext(path) is None \
                                and os.path.splitext(rel)[1].lower() in (
                                    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4",
                                    ".mov", ".3gp", ".mkv", ".mp3", ".m4a", ".opus", ".pdf"):
                            problema = "começa com zeros (arquivo corrompido)"
            except FileNotFoundError:
                problema = "sumiu do backup"
            except OSError as exc:
                problema = f"ilegível no disco: {exc}"
            if problema:
                ruins += 1
                if path.exists():
                    # Mesmo tamanho = a retomada pularia. Tira do caminho (sem apagar).
                    try:
                        path.rename(path.with_name(path.name + ".corrompido"))
                    except OSError:
                        pass
                manifest.db.execute(
                    "UPDATE files SET status='failed', error=? WHERE rel=?",
                    (f"conferência: {problema}", rel))
                log.write(f"  ✗ {rel}: {problema}")
    finally:
        manifest.commit()
        BARRA.fim()
    log.write(f"conferência do disco: {len(rows)} arquivos em {hms(time.time() - t0)}, "
              f"{ruins} com problema"
              + (" — rode --retry-failed com o celular conectado" if ruins else " — tudo íntegro"))
    return ruins


def write_reports(manifest: Manifest, logs_dir: Path, log: Log) -> None:
    index = logs_dir / "indice_arquivos.csv"
    with open(index, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["caminho_no_celular", "app", "caminho_no_backup", "tamanho_origem",
                         "bytes_copiados", "status", "md5", "erro"])
        writer.writerows(manifest.rows())

    log.write("já no destino, por app:")
    for app, n, sz in manifest.by_app():
        log.write(f"    {app:<20} {n:>7} arquivos  {human(sz)}")

    pend = list(manifest.rows("failed")) + list(manifest.rows("skipped_nospace"))
    if pend:
        report = logs_dir / "nao_copiados.txt"
        with open(report, "w", encoding="utf-8") as fh:
            for rel, _app, _dest, size, _b, status, _d, err in pend:
                fh.write(f"{status}\t{human(size)}\t{rel}\t{err}\n")
        log.write(f"{len(pend)} pendências listadas em {report}")
        log.write("Para tentar só elas: python3 backup_celular_samsung.py --retry-failed")

    log.write(f"manifesto: {manifest.counts()} | total no destino: {human(manifest.total_done())}")
    log.write(f"índice completo: {index}")


# ───────────────────── estimativa de tamanho x espaço livre ─────────────────────

def estimate(storages: list[Path], dest_root: Path, logs_dir: Path, log: Log,
             manifest: "Manifest", f: Filtro) -> int:
    """Varre só os metadados e diz quanto falta copiar x quanto cabe no destino.

    Não lê conteúdo de arquivo, mas ainda assim é uma volta completa pelo aparelho:
    no MTP isso leva de alguns minutos a bem mais, dependendo de Android/data.
    """
    log.write("── ESTIMATIVA (só metadados, nada é copiado) ──")
    rows: list[tuple[str, int, int, int, int]] = []   # label, arq, bytes, faltam_arq, faltam_bytes
    tot_files = tot_bytes = miss_files = miss_bytes = 0
    fora_files = fora_bytes = 0

    for storage in storages:
        for group in range(len(PRIORITY)):
            items, fora = collect_group(storage, group, log, f)
            fora_files += len(fora)
            fora_bytes += sum(s for _i, s, _m in fora)
            if not items:
                continue
            gb = sum(i.size for i in items)
            pend = [i for i in items if landed(manifest, dest_root, i) is None]
            pb = sum(i.size for i in pend)
            rows.append((group_label(group), len(items), gb, len(pend), pb))
            tot_files += len(items)
            tot_bytes += gb
            miss_files += len(pend)
            miss_bytes += pb
            log.write(f"  {group_label(group):<46} {len(items):>7} arq  {human(gb):>10}"
                      f"   faltam {len(pend):>7} / {human(pb)}")

    free = shutil.disk_usage(dest_root).free
    usable = free - RESERVE_BYTES
    lines = [
        "ESTIMATIVA DE CÓPIA",
        f"gerado em {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"destino   {dest_root}",
        "",
        f"{'GRUPO':<46} {'ARQUIVOS':>9} {'TAMANHO':>11}   {'FALTAM':>9} {'A BAIXAR':>11}",
    ]
    for label, n, b, pn, pb in rows:
        lines.append(f"{label:<46} {n:>9} {human(b):>11}   {pn:>9} {human(pb):>11}")
    lines += [
        "-" * 92,
        f"{'TOTAL':<46} {tot_files:>9} {human(tot_bytes):>11}   {miss_files:>9} {human(miss_bytes):>11}",
        "",
        f"já no destino          {human(tot_bytes - miss_bytes)}",
        f"ainda falta baixar     {human(miss_bytes)}  ({miss_files} arquivos)",
        f"fora da cópia          {human(fora_bytes)}  ({fora_files} arquivos que não abrem"
        f" sozinhos; --fragmentos inclui)",
        f"espaço livre no destino{human(free):>12}",
        f"reserva de segurança   {human(RESERVE_BYTES)}",
        f"utilizável             {human(max(usable, 0))}",
        "",
    ]
    if miss_bytes <= usable:
        verdict = f"CABE. Sobram {human(usable - miss_bytes)} depois de copiar tudo."
    else:
        verdict = (f"NÃO CABE. Faltam {human(miss_bytes - usable)}. "
                   f"Libere espaço ou use --dest em outro disco/HD externo.")
    lines.append(verdict)

    report = logs_dir / "estimativa.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.write("─" * 72)
    log.write(f"total no celular: {tot_files} arquivos / {human(tot_bytes)}")
    log.write(f"falta baixar:     {miss_files} arquivos / {human(miss_bytes)}")
    log.write(f"fora da cópia:    {fora_files} arquivos / {human(fora_bytes)} (não abrem sozinhos)")
    log.write(f"livre no destino: {human(free)} (utilizável {human(max(usable, 0))})")
    log.write(verdict)
    log.write(f"tabela completa em {report}")
    return miss_bytes


# ──────────────────────────────────── main ────────────────────────────────────

def organizar_destino(manifest: Manifest, dest_root: Path, logs_dir: Path, log: Log,
                      dry: bool) -> int:
    """Arruma no disco, sem tocar no celular, o que veio antes da divisão por mês.

    Para cada pasta de POR_MES em cada armazenamento já copiado, junta os arquivos
    soltos, os de <pasta>/AAAA-MM/ e os de <pasta>/mp4/AAAA-MM/ (arrumação manual
    antiga) em <pasta>/AAAA-MM/, com o mês tirado do nome. Só mexe no que o manifesto
    reconhece como vindo daquela pasta do celular; o resto fica onde está, listado.
    Cópia repetida (mesmo md5 no destino) é apagada; nome igual com conteúdo
    diferente fica parado, listado. O manifesto passa a apontar para o novo caminho.
    Cada movimento vai para _logs/organizar_<data>.tsv, para dar pra desfazer."""
    def candidatos(base: Path):
        for f in base.iterdir():
            if f.is_file():
                yield f
            elif f.is_dir() and PASTA_MES.match(f.name):
                yield from (g for g in f.iterdir() if g.is_file())
        mp4 = base / "mp4"
        if mp4.is_dir():
            for f in mp4.iterdir():
                if f.is_file():
                    yield f
                elif f.is_dir() and PASTA_MES.match(f.name):
                    yield from (g for g in f.iterdir() if g.is_file())

    acao = "MOVERIA" if dry else "movido"
    mov = dup = conflito = fora = ok = 0
    registro: list[tuple[str, str, str]] = []
    vazias: set[Path] = set()
    for label_dir in sorted(d for d in dest_root.iterdir() if d.is_dir() and d.name != "_logs"):
        for pasta in POR_MES:
            base = label_dir / pasta
            if not base.is_dir():
                continue
            log.write(f"organizando {base.relative_to(dest_root)}")
            fora_aqui = 0
            for f in sorted(candidatos(base)):
                if f.name.endswith(".part"):
                    continue
                rel = f"{label_dir.name}/{pasta}/{f.name}"
                row = manifest.status_of(rel)
                if row is None:
                    fora += 1
                    fora_aqui += 1
                    log.write(f"  fora do manifesto, deixado: {f.relative_to(dest_root)}",
                              echo=False)   # backup de outro aparelho: milhares de linhas
                    continue
                alvo = safe_dest(dest_root, organizar_rel(rel, f.stat().st_mtime))
                novo = str(alvo.relative_to(dest_root))
                if f == alvo:
                    ok += 1
                elif alvo.exists():
                    if alvo.stat().st_size == f.stat().st_size and md5_of(alvo) == md5_of(f):
                        dup += 1
                        log.write(f"  {'APAGARIA' if dry else 'apagado'} (cópia idêntica de "
                                  f"{novo}): {f.relative_to(dest_root)}")
                        registro.append(("apagado-duplicata", str(f.relative_to(dest_root)), novo))
                        if not dry:
                            f.unlink()
                            vazias.add(f.parent)
                    else:
                        conflito += 1
                        log.write(f"  CONFLITO, deixado (mesmo nome, conteúdo diferente de "
                                  f"{novo}): {f.relative_to(dest_root)}")
                        continue
                else:
                    mov += 1
                    log.write(f"  {acao}: {f.relative_to(base)} -> {alvo.relative_to(base)}",
                              echo=dry)
                    registro.append(("movido", str(f.relative_to(dest_root)), novo))
                    if not dry:
                        alvo.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(f, alvo)
                        vazias.add(f.parent)
                if not dry and row[1] != novo:
                    manifest.db.execute("UPDATE files SET dest = ? WHERE rel = ?", (novo, rel))
            if fora_aqui:
                log.write(f"  {fora_aqui} arquivos fora do manifesto deixados como estão "
                          "(lista no backup.log)")
            if not dry:
                manifest.commit()
                vazias.add(base / "mp4")

    if not dry:
        # Só remove pasta que ficou vazia; rmdir recusa sozinho as que não ficaram.
        for d in sorted(vazias, key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
                log.write(f"  pasta vazia removida: {d.relative_to(dest_root)}")
            except OSError:
                pass
        if registro:
            tsv = logs_dir / time.strftime("organizar_%Y%m%d_%H%M%S.tsv")
            with open(tsv, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, delimiter="\t")
                w.writerow(["acao", "de", "para"])
                w.writerows(registro)
            log.write(f"registro para desfazer: {tsv}")
    pre = "simulação — " if dry else ""
    log.write(f"{pre}{mov} {'a mover' if dry else 'movidos'}, {dup} duplicatas "
              f"{'a apagar' if dry else 'apagadas'}, {ok} já no lugar, "
              f"{conflito} conflitos, {fora} fora do manifesto")
    return 1 if conflito else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cópia completa de celular Android via MTP, retomável e priorizada.",
        epilog="Apps reconhecidos: " + ", ".join(APPS),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source", help=f"raiz do armazenamento (padrão: {SOURCE_ROOT}, "
                                     "ou detecta o MTP no gvfs)")
    ap.add_argument("--dest", default=str(DEST_ROOT), help=f"pasta de destino (padrão: {DEST_ROOT})")
    ap.add_argument("--apps", action="store_true",
                    help="copia SÓ mídias e figurinhas dos apps de conversa e redes sociais")
    ap.add_argument("--only", nargs="+", metavar="PASTA",
                    help="copia só estas pastas do celular (ex.: DCIM Music)")
    ap.add_argument("--incremental", action="store_true",
                    help="nunca escreve por cima: se já houver cópia de tamanho diferente, "
                         "o antigo fica e o novo entra como .v2, .v3…")
    ap.add_argument("--desde", metavar="AAAA-MM-DD",
                    help="só arquivos criados/alterados a partir desta data (ex.: 2026-08-18)")
    ap.add_argument("--excluir", nargs="+", metavar="PASTA", default=[],
                    help="não copia estas pastas (ex.: 'Download/YU-GI-OH Temporada Completa Dublado')")
    ap.add_argument("--no-android-data", action="store_true",
                    help="ignora Android/data, exceto dos apps de conversa/redes sociais")
    ap.add_argument("--verify", choices=["size", "md5"], default="size",
                    help="size (padrão, rápido) ou md5 (relê tudo: mais seguro, ~2x mais lento)")
    ap.add_argument("--no-space-guard", action="store_true",
                    help="não parar quando o disco de destino estiver acabando")
    ap.add_argument("--retry-failed", action="store_true", help="reprocessa apenas o que falhou antes")
    ap.add_argument("--fast-resume", action="store_true",
                    help="não revarre grupos já concluídos (retomada rápida)")
    ap.add_argument("--check", action="store_true",
                    help="só mede: tamanho a copiar x espaço livre no destino (não copia nada)")
    ap.add_argument("--conferir", action="store_true",
                    help="confere o backup no disco (existe, tamanho, não corrompido) — "
                         "não precisa do celular; o que estiver ruim vai pro --retry-failed")
    ap.add_argument("--status", action="store_true",
                    help="mostra o ponto de retomada salvo, sem acessar o celular")
    ap.add_argument("--fragmentos", action="store_true",
                    help="copiar também o que não abre sozinho (.exo, .temp, .preload, 0 byte)")
    ap.add_argument("--sem-renomear", action="store_true", dest="sem_renomear",
                    help="não acrescentar a extensão real em arquivo de cache com nome opaco")
    ap.add_argument("--sem-por-mes", action="store_true", dest="sem_por_mes",
                    help="não separar a Câmera em AAAA-MM: destino espelha o celular")
    ap.add_argument("--organizar", action="store_true",
                    help="só arruma o destino: Câmera já copiada vai para AAAA-MM e o "
                         "manifesto é corrigido (não toca no celular; use com --dry-run antes)")
    ap.add_argument("--no-gio", action="store_true",
                    help="não usar o gvfs nativo; ler tudo pelo ponto de montagem (mais lento)")
    ap.add_argument("--dry-run", action="store_true", help="mostra o que faria, sem copiar")
    args = ap.parse_args()

    global USE_GIO, POR_MES
    USE_GIO = not args.no_gio
    if args.sem_por_mes:
        POR_MES = ()
    only = [norm(o) for o in args.only] if args.only else None
    excluir = [norm(e) for e in args.excluir] or None
    desde = None
    if args.desde:
        try:
            desde = time.mktime(time.strptime(args.desde, "%Y-%m-%d"))   # meia-noite local
        except ValueError:
            ap.error(f"--desde: data inválida '{args.desde}' (use AAAA-MM-DD)")
    filtro = Filtro(only=only, excluir=excluir, apps_only=args.apps,
                    skip_data=args.no_android_data, keep_frag=args.fragmentos, desde=desde)
    descartados: list[tuple[str, int, str]] = []
    dest_root = Path(args.dest).expanduser()
    logs_dir = dest_root / "_logs"
    dest_root.mkdir(parents=True, exist_ok=True)
    log = Log(logs_dir / "backup.log")
    manifest = Manifest(logs_dir / "manifesto.sqlite3")
    tot = Totals()

    try:
        if args.organizar:
            if not POR_MES:
                ap.error("--organizar não combina com --sem-por-mes")
            return organizar_destino(manifest, dest_root, logs_dir, log, args.dry_run)

        if args.conferir:
            ruins = conferir_backup(manifest, dest_root, log)
            write_prontidao(manifest, logs_dir, log)
            return 1 if ruins else 0

        if args.status:
            write_prontidao(manifest, logs_dir, log)
            resume = write_resume_point(manifest, logs_dir, dest_root, log, "consulta (--status)")
            print()
            print(resume.read_text(encoding="utf-8"))
            return 0

        source = args.source
        if source is None and SOURCE_ROOT.is_dir():
            source = str(SOURCE_ROOT)
        storages = find_storages(source)
        if desde is not None:
            log.write(f"filtro --desde {args.desde}: só arquivos modificados a partir dessa data")
        log.write(f"armazenamentos: {', '.join(str(s) for s in storages)}")
        log.write(f"espaço livre no destino: {human(shutil.disk_usage(dest_root).free)}")
        if HAVE_GIO and USE_GIO:
            log.write("motor: gvfs nativo (Gio)")
        else:
            log.write("motor: ponto de montagem (FUSE) — mais lento"
                      + ("" if HAVE_GIO else "; instale python3-gi para acelerar"))

        if args.check:
            estimate(storages, dest_root, logs_dir, log, manifest, filtro)
            return 0

        if args.apps:
            log.write("modo --apps: só mídias/figurinhas de " + ", ".join(APPS))
        if args.retry_failed:
            log.write(f"{manifest.reset_failed()} registros marcados para nova tentativa")
        pending = manifest.pending() if args.retry_failed else None
        if pending is not None and not only:
            # Só varre as pastas que têm pendência: minutos em vez de uma hora.
            pastas = sorted({r.split("/", 1)[1].rsplit("/", 1)[0]
                             for r in pending if "/" in r.split("/", 1)[-1]})
            if pastas:
                filtro.only = pastas
                log.write(f"--retry-failed: {len(pending)} pendências em {len(pastas)} pastas")

        vistos_no_celular: set[str] = set()
        grupos_varridos: set[str] = set()
        for storage in storages:
            label = storage_label(storage)
            for group in range(len(PRIORITY)):
                key = f"{label}|{PRIORITY[group]}"
                if args.fast_resume and manifest.group_status(key) == "complete":
                    left, left_bytes = manifest.group_pending(f"{label}/{PRIORITY[group]}")
                    if not left:
                        log.write(f"↷ {group_label(group)}: já concluído, pulando varredura")
                        continue
                    log.write(f"↻ {group_label(group)}: {left} pendências "
                              f"({human(left_bytes)}) — vou varrer de novo")

                t0 = time.time()
                items, fora = collect_group(storage, group, log, filtro)
                dt = time.time() - t0
                if dt > 30:
                    # A listagem do MTP às vezes despenca de ~3500 para ~16 arq/s numa
                    # sessão longa. Fica registrado: é o sintoma pra reconectar o cabo.
                    log.write(f"  varredura levou {hms(dt)} para {len(items)} arquivos "
                              f"({len(items) / dt:.0f}/s)")
                vistos_no_celular.update(i.rel for i in items)
                vistos_no_celular.update(f"{label}/{i}" for i, _s, _m in fora)
                grupos_varridos.add(PRIORITY[group])
                if fora:
                    tot.discarded += len(fora)
                    tot.discarded_bytes += sum(s for _i, s, _m in fora)
                    descartados.extend((f"{label}/{i}", s, m) for i, s, m in fora)
                if pending is not None:
                    items = [i for i in items if i.rel in pending]
                if not items:
                    continue
                tot.files += len(items)
                if not args.dry_run:
                    for item in items:
                        manifest.note(item.rel, item.app, item.size, item.mtime)
                    manifest.commit()
                result = copy_group(items, group_label(group), dest_root, manifest, log, storage,
                                    args.verify, args.dry_run, not args.no_space_guard,
                                    not args.sem_renomear, tot, descartados,
                                    args.incremental)
                # Só marca grupo como concluído quando ele foi visto por inteiro: sob
                # --only/--retry-failed a lista é parcial, e marcar aqui faria o
                # --fast-resume pular depois o que nunca foi copiado.
                if not args.dry_run and not only and pending is None and desde is None:
                    manifest.mark_group(key, group_label(group), len(items),
                                        sum(i.size for i in items), result)
                if tot.out_of_space or tot.disconnected:
                    break
            if tot.out_of_space or tot.disconnected:
                break

        log.write("─" * 72)
        log.write(f"vistos {tot.files} arquivos | copiados agora {tot.copied} | "
                  f"já existiam {tot.skipped} | falhas {tot.failed}")
        log.write(f"volume desta sessão: {human(tot.bytes)} em {hms(time.time() - tot.started)}")
        if tot.renamed:
            log.write(f"{tot.renamed} arquivos ganharam a extensão real "
                      f"(nome opaco tipo '<hash>.0', ou extensão que mentia sobre o tipo)")
        if tot.rescued:
            log.write(f"{tot.rescued} arquivos com extensão de fragmento eram mídia de "
                      f"verdade e foram copiados")
        if descartados:
            report = logs_dir / "descartados.txt"
            with open(report, "w", encoding="utf-8") as fh:
                fh.write("# Deixados fora por não abrirem sozinhos. --fragmentos copia mesmo assim.\n")
                for rel, size, motivo in sorted(descartados):
                    fh.write(f"{motivo}\t{human(size)}\t{rel}\n")
            log.write(f"fora da cópia: {tot.discarded} arquivos / "
                      f"{human(tot.discarded_bytes)} — lista em {report}")
        if not args.dry_run:
            note = ("parou por falta de espaço" if tot.out_of_space else
                    "celular desconectou" if tot.disconnected else "sessão terminou normalmente")
            if (not only and not excluir and not args.apps and desde is None
                    and pending is None and not args.no_android_data
                    and not tot.out_of_space and not tot.disconnected):
                (logs_dir / MARCA_COMPLETA).write_text(
                    f"{time.strftime('%Y-%m-%d %H:%M')} — {tot.files} arquivos vistos, "
                    f"{tot.copied} copiados, {tot.failed} falhas\n", encoding="utf-8")
            if not (tot.out_of_space or tot.disconnected or args.fast_resume):
                marcar_ausentes(manifest, vistos_no_celular, filtro, desde, log)
            write_reports(manifest, logs_dir, log)
            write_prontidao(manifest, logs_dir, log)
            write_resume_point(manifest, logs_dir, dest_root, log, note)
        log.write("fim.")
        return 0
    except KeyboardInterrupt:
        log.write("interrompido — progresso salvo, rode de novo para continuar.")
        manifest.commit()
        if not (args.status or args.dry_run):
            write_resume_point(manifest, logs_dir, dest_root, log, "interrompido com Ctrl+C")
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
