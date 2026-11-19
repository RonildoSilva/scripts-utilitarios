#!/usr/bin/env python3
"""
Espelha uma pasta inteira em outra: recria a mesma estrutura de subpastas,
comprime os vídeos (ffmpeg, CPU ou GPU) e copia todo o resto (fotos, textos,
etc.) sem alterar. Mostra barra de progresso.

USO
    python3 comprimir_videos.py ORIGEM DESTINO
    python3 comprimir_videos.py ORIGEM DESTINO --gpu          # usa GPU se der
    python3 comprimir_videos.py ORIGEM DESTINO --crf 28       # vídeos menores
    nohup python3 comprimir_videos.py ORIGEM DESTINO > compress.log 2>&1 &

    O que acontece:
      - vídeo   -> DESTINO/<mesmo caminho>/<mesmo nome>.mp4 (comprimido)
      - o resto -> DESTINO/<mesmo caminho>/<mesmo nome>     (cópia idêntica)
    Pode parar (Ctrl+C, kill, desligar) e rodar de novo: o que já está pronto
    é pulado. Nada fica "pela metade" no destino: tudo é gravado num arquivo
    temporário e só renomeado para o nome final depois de conferido.

TRATAMENTO DE PROBLEMAS
    Antes de começar:
      - confere ffmpeg/ffprobe e encoders; testa a GPU (se --gpu)
      - confere se a origem é legível e o destino gravável
      - estima o espaço necessário e avisa se pode faltar
      - avisa sobre limitações do sistema de arquivos do destino (FAT32: 4GB)
      - impede duas execuções simultâneas no mesmo destino (lock)
      - apaga temporários deixados por uma execução interrompida

    Cada vídeo gerado é conferido (ffprobe: abre? duração bate com a original?).
    Quando algo falha, o erro é classificado:

      PARA TUDO (não adianta continuar; resolva e rode de novo):
        disco de destino cheio, destino somente leitura (ex.: NTFS "sujo"
        pelo Windows), cota excedida, disco de origem/destino desconectado.

      PULA O ARQUIVO (tentar de novo não resolve; copia o original no lugar):
        origem corrompida/incompleta, sem permissão de leitura,
        arquivo maior que o limite do sistema de arquivos (FAT32).

      TENTA DE NOVO (2x por padrão, com espera crescente):
        saída corrompida, ffmpeg travado (sem progresso), processo morto pelo
        sistema (falta de RAM), erro de E/S, origem mudou durante a conversão
        (ainda baixando?), falha da GPU (refaz na CPU), erro desconhecido.

      OUTROS:
        "vídeo" sem faixa de vídeo (só áudio) -> copiado como está;
        nome inválido/longo demais no destino -> salvo com nome ajustado.

    Ao final mostra um resumo com o motivo de cada falha.
    Código de saída: 0 = tudo ok | 2 = algum arquivo falhou | 1 = parou no meio.

OPÇÕES (flags)
    --gpu [auto|nvenc|qsv|vaapi]
        Usa a placa de vídeo para codificar (bem mais rápido, arquivos um
        pouco maiores na mesma qualidade). Sem valor = auto: testa NVIDIA
        (nvenc), Intel QuickSync (qsv) e VAAPI (Intel/AMD) e usa a primeira que
        funcionar de verdade. Se nenhuma funcionar, roda na CPU normalmente.
        Se a GPU falhar num vídeo específico, ele é refeito na CPU.
        Obs.: se a GPU só suportar H.264, usa H.264 mesmo com --codec x265.

    --codec {x265,x264}
        x265 (H.265/HEVC) -> PADRÃO. ~50% menor que x264 na mesma qualidade,
                             porém mais lento. Toca em celulares/TVs/players
                             atuais (VLC, mpv, Android, iPhone).
        x264 (H.264/AVC)  -> 3-5x mais rápido e compatível com qualquer
                             aparelho, mas gera arquivos maiores.

    --crf N
        Qualidade. MENOR = mais qualidade e arquivo maior.
        Cada +6 reduz o arquivo para cerca da metade.
            CPU x265: 22 (quase idêntico) | 26 (PADRÃO) | 28-30 (bem menor)
            CPU x264: 18 (quase idêntico) | 23 (PADRÃO) | 26-28 (bem menor)
        Na GPU o mesmo número vira o -cq/-qp/-global_quality do encoder
        (padrões próprios por GPU, ver GPU_QUALITY no código).

    --preset NOME   (só CPU)
        ultrafast, superfast, veryfast, faster, fast (PADRÃO), medium,
        slow, slower, veryslow. Mais lento = arquivo um pouco menor.

    --max-height N
        Reduz a resolução de vídeos mais altos que N pixels (ex.: 720).
        Padrão: mantém a resolução original.

    --audio-bitrate TAXA
        Bitrate do áudio AAC. Padrão: 128k (64k já é ok para voz).

    --retries N
        Novas tentativas por vídeo nos erros "tenta de novo". Padrão: 2.

    --stall-timeout SEG
        Mata o ffmpeg se ficar SEG segundos sem progresso. Padrão: 300.

    --no-copy
        Só comprime os vídeos; não copia os outros arquivos.

    --overwrite
        Reconverte/recopia arquivos que já existem no destino.
"""

import argparse
import errno
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".3gp"}

DEFAULT_CODEC = "x265"
DEFAULT_CRF = {"x265": 26, "x264": 23}
DEFAULT_PRESET = "fast"
DEFAULT_AUDIO_BITRATE = "128k"
DEFAULT_RETRIES = 2
DEFAULT_STALL = 300
RETRY_WAITS = [10, 30, 60, 120]  # espera entre tentativas (s)
MIN_FREE = 200 * 1024 * 1024  # abaixo disso no destino = disco cheio
EST_RATIO = 0.5  # estimativa: vídeo comprimido ~50% do original
GPU_FAIL_LIMIT = 3  # após N falhas seguidas da GPU, desliga a GPU
TMP_TAG = ".cvtmp"
LOCK_NAME = ".compress_videos.lock"
BAD_CHARS = '<>:"|?*\\'

# qualidade padrão por tipo de GPU (cq / qp / global_quality)
GPU_QUALITY = {
    "nvenc": {"x265": 28, "x264": 25},
    "qsv": {"x265": 26, "x264": 24},
    "vaapi": {"x265": 26, "x264": 24},
}
GPU_ENCODER = {
    ("nvenc", "x265"): "hevc_nvenc",
    ("nvenc", "x264"): "h264_nvenc",
    ("qsv", "x265"): "hevc_qsv",
    ("qsv", "x264"): "h264_qsv",
    ("vaapi", "x265"): "hevc_vaapi",
    ("vaapi", "x264"): "h264_vaapi",
}

SOURCE_CORRUPT_KEYS = (
    "invalid data found",
    "moov atom not found",
    "could not find codec parameters",
    "error while decoding",
    "corrupt",
    "truncat",
    "end of file",
    "invalid nal",
    "header missing",
)


# ----------------------------------------------------------------- utilidades
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def hms(sec: float) -> str:
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m:02d}m{s:02d}s"


def bar(frac: float, width: int = 30) -> str:
    frac = min(max(frac, 0.0), 1.0)
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


def free_space(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1


def dir_ok(path: Path) -> bool:
    """A pasta existe e responde? (pega disco USB desconectado)."""
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def fs_type(path: Path) -> str:
    """Tipo do sistema de arquivos onde 'path' está (via /proc/mounts)."""
    best, kind = "", "?"
    try:
        p = str(path.resolve())
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                mnt = parts[1].replace("\\040", " ")
                if (p == mnt or p.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, kind = mnt, parts[2]
    except OSError:
        pass
    return kind


def file_sig(p: Path):
    try:
        st = p.stat()
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


def sanitize(name: str) -> str:
    """Nome aceito por NTFS/exFAT/FAT e com no máx. ~200 bytes."""
    clean = "".join("_" if (c in BAD_CHARS or ord(c) < 32) else c for c in name)
    stem, dot, ext = clean.rpartition(".")
    if not dot:
        stem, ext = clean, ""
    stem = stem.rstrip(" .") or "_"
    ext = ("." + ext) if dot else ""
    while len((stem + ext).encode()) > 200:
        stem = stem[:-1]
    return stem + ext


def probe(path: Path):
    """Retorna (duração, tem_faixa_de_video) ou (0.0, None) se ilegível."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=codec_type:stream_disposition=attached_pic",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0, None
    try:
        dur = float(data.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        dur = 0.0
    streams = data.get("streams")
    if streams is None:
        return dur, None
    has_video = any(
        s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        for s in streams
    )
    return dur, has_video


def copy_atomic(src: Path, dst: Path):
    """Copia para um temporário e renomeia: nunca deixa arquivo pela metade."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + TMP_TAG)
    try:
        shutil.copyfile(src, tmp)
        try:
            shutil.copystat(src, tmp)
        except OSError:
            pass  # datas/permissões não são essenciais
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ------------------------------------------------------------ erros / classes
class EncodeError(Exception):
    def __init__(self, msg, returncode=None, stalled=False):
        super().__init__(msg)
        self.returncode = returncode
        self.stalled = stalled


class VerifyError(Exception):
    pass


class SourceChanged(Exception):
    pass


class Fatal(Exception):
    pass


def classify(exc, src, dst, src_root, dst_root, on_gpu):
    """
    Decide o que fazer com um erro. Retorna (ação, dicas, espera_extra)
    ação: fatal | skip | not_video | name | retry
    """
    text = str(exc).lower()
    en = getattr(exc, "errno", None)
    hints = []
    free = free_space(dst_root)
    if free >= 0:
        hints.append(f"espaço livre no destino: {human(free)}")

    # --- problemas que afetam tudo -> parar
    if not dir_ok(src_root):
        return "fatal", hints + ["pasta/disco de ORIGEM inacessível (desconectado?)"], 0
    if not dir_ok(dst_root):
        return "fatal", hints + ["pasta/disco de DESTINO inacessível (desconectado?)"], 0
    if en == errno.ENOSPC or "no space left" in text or (0 <= free < MIN_FREE):
        return "fatal", hints + ["DISCO DE DESTINO CHEIO — libere espaço"], 0
    if en == errno.EDQUOT or "quota exceeded" in text:
        return "fatal", hints + ["cota de disco excedida no destino"], 0
    if en == errno.EROFS or "read-only file system" in text or not os.access(dst_root, os.W_OK):
        h = "destino está SOMENTE LEITURA"
        if fs_type(dst_root).startswith(("ntfs", "fuseblk")):
            h += (" (NTFS: costuma ser Windows hibernado/Inicialização rápida ou disco "
                  "'sujo' — desligue o Windows por completo ou rode 'sudo ntfsfix -d')")
        return "fatal", hints + [h], 0

    # --- problemas do arquivo
    if en == errno.EFBIG or "file too large" in text:
        return "skip", hints + [
            f"arquivo grande demais para o sistema de arquivos do destino ({fs_type(dst_root)}; FAT32 = máx. 4GB)"
        ], 0
    if en == errno.ENAMETOOLONG or "file name too long" in text:
        return "name", hints + ["nome longo demais no destino"], 0
    if en == errno.EINVAL and isinstance(exc, OSError) and dst.name != sanitize(dst.name):
        return "name", hints + ["nome com caracteres inválidos no destino"], 0
    if not src.exists():
        return "skip", hints + ["arquivo de ORIGEM sumiu (apagado/movido durante o processo)"], 0
    if not os.access(src, os.R_OK):
        return "skip", hints + ["sem permissão de LEITURA no arquivo de origem"], 0
    if en == errno.EACCES or "permission denied" in text:
        return "skip", hints + [f"sem permissão de ESCRITA em {dst.parent}"], 0
    if isinstance(exc, SourceChanged):
        return "retry", hints + [
            "arquivo de origem MUDOU durante a conversão (ainda baixando/sendo copiado?)"
        ], 30
    if ("matches no streams" in text or "does not contain any stream" in text
            or "failed to set value '0:v:0'" in text):
        return "not_video", hints + ["não tem faixa de vídeo (só áudio?)"], 0

    # --- problemas passageiros -> tentar de novo
    if isinstance(exc, EncodeError):
        if exc.stalled:
            return "retry", hints + [
                "ffmpeg TRAVOU (sem progresso) — disco lento/USB instável/arquivo problemático"
            ], 0
        rc = exc.returncode
        if rc is not None and rc < 0:
            sig = -rc
            h = f"ffmpeg foi morto pelo sinal {sig}"
            if sig == signal.SIGKILL:
                h += " — provável FALTA DE RAM (OOM killer); feche outros programas"
            return "retry", hints + [h], 60
    if en == errno.ENOMEM or "cannot allocate memory" in text:
        return "retry", hints + ["FALTA DE RAM"], 60
    if en == errno.EIO or "input/output error" in text:
        return "retry", hints + ["erro de E/S no disco (cabo/USB/setor ruim?)"], 30
    if "error opening input" in text or (any(k in text for k in SOURCE_CORRUPT_KEYS) and not on_gpu):
        return "skip", hints + ["o arquivo de ORIGEM parece corrompido/incompleto"], 0
    if isinstance(exc, VerifyError):
        return "retry", hints + ["arquivo gerado saiu corrompido/incompleto"], 0
    if on_gpu:
        return "retry", hints + ["falha na GPU — vou refazer na CPU"], 0
    return "retry", hints + ["erro não identificado"], 0


# ---------------------------------------------------------------- encoders
def make_encoder(kind, codec, quality, device=None):
    name = "libx265" if codec == "x265" else "libx264"
    if kind != "cpu":
        name = GPU_ENCODER[(kind, codec)]
    return {"kind": kind, "codec": codec, "q": quality, "name": name, "device": device}


def video_args(enc, preset, max_height):
    """(args antes do -i, args de vídeo depois do -i)."""
    pre, vf = [], []
    if max_height:
        vf.append(f"scale=-2:'trunc(min({max_height},ih)/2)*2'")
    else:
        vf.append("scale='trunc(iw/2)*2':'trunc(ih/2)*2'")  # x264/x265 exigem dimensões pares
    k, q, name = enc["kind"], str(enc["q"]), enc["name"]
    if k == "cpu":
        vf.append("format=yuv420p")
        codec = ["-c:v", name, "-crf", q, "-preset", preset]
        if enc["codec"] == "x265":
            codec += ["-x265-params", "log-level=error"]
    elif k == "nvenc":
        vf.append("format=yuv420p")
        codec = ["-c:v", name, "-rc", "vbr", "-cq", q, "-b:v", "0", "-preset", "p5"]
    elif k == "qsv":
        vf.append("format=nv12")
        codec = ["-c:v", name, "-global_quality", q, "-preset", "medium"]
    else:  # vaapi
        pre = ["-vaapi_device", enc["device"]]
        vf += ["format=nv12", "hwupload"]
        codec = ["-c:v", name, "-rc_mode", "CQP", "-qp", q]
    if enc["codec"] == "x265":
        codec += ["-tag:v", "hvc1"]
    return pre, ["-vf", ",".join(vf)] + codec


def build_cmd(src, dst, enc, preset, max_height, audio_bitrate):
    pre, vargs = video_args(enc, preset, max_height)
    return (
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y", "-progress", "pipe:1"]
        + pre
        + ["-i", str(src), "-map", "0:V:0", "-map", "0:a?"]  # V maiúsculo = ignora capas
        + vargs
        + ["-c:a", "aac", "-b:a", audio_bitrate, "-movflags", "+faststart", "-f", "mp4", str(dst)]
    )


def encoder_works(enc, preset) -> str:
    """Faz um encode de teste de 1s. Retorna '' se ok, senão o erro."""
    pre, vargs = video_args(enc, preset, None)
    cmd = (
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        + pre
        + ["-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=25"]
        + vargs
        + ["-f", "null", "-"]
    )
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "tempo esgotado"
    if r.returncode == 0:
        return ""
    lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
    return lines[-1] if lines else f"código {r.returncode}"


def detect_gpu(mode, codec, crf, preset):
    kinds = ["nvenc", "qsv", "vaapi"] if mode == "auto" else [mode]
    other = "x264" if codec == "x265" else "x265"
    available = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    devices = sorted(str(p) for p in Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").exists() else []
    report = []
    fallback = None  # GPU que funciona, mas só no outro codec
    for kind in kinds:
        if kind == "vaapi" and not devices:
            report.append("vaapi: nenhum /dev/dri/renderD*")
            continue
        for dev in devices if kind == "vaapi" else [None]:
            for c in (codec, other):
                name = GPU_ENCODER[(kind, c)]
                if f" {name} " not in available:
                    report.append(f"{name}: não compilado no ffmpeg")
                    continue
                q = crf if crf is not None else GPU_QUALITY[kind][c]
                enc = make_encoder(kind, c, q, dev)
                err = encoder_works(enc, preset)
                label = f"{name}{f' ({dev})' if dev else ''}"
                if not err:
                    if c == codec:
                        return enc, report
                    fallback = fallback or enc
                    report.append(f"{label}: OK")
                else:
                    report.append(f"{label}: {err[:90]}")
    return fallback, report


# ---------------------------------------------------------------- encode
def encode(cmd, file_dur, done_before, total_dur, t_start, stall_timeout):
    """Roda o ffmpeg, desenha as barras e vigia travamentos."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,  # Ctrl+C é tratado aqui, não direto no ffmpeg
    )
    assert proc.stdout is not None and proc.stderr is not None
    err_tail = deque(maxlen=30)
    state = {"last": time.time(), "stalled": False}

    def read_stderr():  # evita travar com pipe cheio quando há muitos erros
        for line in proc.stderr:
            if line.strip():
                err_tail.append(line.strip())

    def watchdog():
        while proc.poll() is None:
            if time.time() - state["last"] > stall_timeout:
                state["stalled"] = True
                proc.kill()
                return
            time.sleep(2)

    threading.Thread(target=read_stderr, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    cur, speed = 0.0, "?"
    tty = sys.stdout.isatty()
    last_print = 0.0
    try:
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key == "out_time_us" and val.isdigit():
                new = int(val) / 1_000_000
                if new > cur:
                    state["last"] = time.time()
                cur = new
            elif key == "speed":
                speed = val.strip()
            elif key == "progress":
                now = time.time()
                if not tty and now - last_print < 30 and val != "end":
                    continue  # em log (nohup) imprime só a cada 30s
                last_print = now
                f_file = cur / file_dur if file_dur else 0
                done = done_before + min(cur, file_dur or cur)
                f_tot = done / total_dur if total_dur else 0
                elapsed = now - t_start
                eta = elapsed / done * (total_dur - done) if done > 0 else 0
                msg = (
                    f"   arquivo {bar(f_file, 20)} {f_file * 100:5.1f}% {speed:>6} | "
                    f"total {bar(f_tot, 20)} {f_tot * 100:5.1f}%  ETA {hms(eta)}"
                )
                print(("\r" + msg) if tty else msg, end="" if tty else "\n", flush=True)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if tty:
            print()
    time.sleep(0.1)  # deixa a thread de stderr terminar
    if state["stalled"]:
        raise EncodeError(f"sem progresso por {stall_timeout}s", proc.returncode, stalled=True)
    if proc.returncode != 0:
        msg = " | ".join(list(err_tail)[-3:]) or f"ffmpeg saiu com código {proc.returncode}"
        raise EncodeError(msg, proc.returncode)


def verify_output(out: Path, src_dur: float):
    if not out.exists() or out.stat().st_size == 0:
        raise VerifyError("arquivo de saída vazio ou inexistente")
    out_dur, has_video = probe(out)
    if out_dur <= 0 or not has_video:
        raise VerifyError("arquivo de saída ilegível (ffprobe não conseguiu ler)")
    if src_dur > 0 and abs(out_dur - src_dur) > max(2.0, src_dur * 0.02):
        raise VerifyError(f"saída incompleta: duração {hms(out_dur)} vs original {hms(src_dur)}")


def check_tail(out: Path, seconds: int = 10):
    """Decodifica os últimos segundos: pega arquivos truncados/corrompidos no fim."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-sseof", f"-{seconds}", "-i", str(out),
             "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise VerifyError(f"falha ao decodificar o final: {e}")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or [f"código {r.returncode}"]
        raise VerifyError(f"final do arquivo não decodifica: {tail[0]}")


# ---------------------------------------------------------------- main
def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def acquire_lock(dst_root: Path):
    path = dst_root / LOCK_NAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        sys.exit(f"Já existe outra execução usando este destino ({dst_root}).")
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd, path


def main():
    ap = argparse.ArgumentParser(
        description="Espelha ORIGEM em DESTINO comprimindo os vídeos e copiando o resto. "
        "Veja o topo do arquivo para detalhes."
    )
    ap.add_argument("input", type=Path, help="pasta de origem")
    ap.add_argument("output", type=Path, help="pasta de destino (criada se não existir)")
    ap.add_argument(
        "--gpu", nargs="?", const="auto", default=None,
        choices=["auto", "nvenc", "qsv", "vaapi"],
        help="usa GPU se disponível (padrão do flag: auto); senão roda na CPU",
    )
    ap.add_argument("--codec", choices=["x265", "x264"], default=DEFAULT_CODEC,
                    help="x265 = menor arquivo (padrão); x264 = mais rápido e compatível")
    ap.add_argument("--crf", type=int, help="qualidade; menor = melhor (padrão: 26 x265 / 23 x264)")
    ap.add_argument("--preset", default=DEFAULT_PRESET,
                    help="ultrafast..veryslow (só CPU); mais lento = arquivo menor (padrão: fast)")
    ap.add_argument("--max-height", type=int, default=None,
                    help="limita a altura, ex.: 1080 ou 720 (padrão: mantém original)")
    ap.add_argument("--audio-bitrate", default=DEFAULT_AUDIO_BITRATE,
                    help="bitrate do áudio AAC (padrão: 128k)")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"novas tentativas por vídeo (padrão: {DEFAULT_RETRIES})")
    ap.add_argument("--stall-timeout", type=int, default=DEFAULT_STALL,
                    help=f"mata o ffmpeg após N s sem progresso (padrão: {DEFAULT_STALL})")
    ap.add_argument("--no-copy", action="store_true", help="não copia arquivos que não são vídeo")
    ap.add_argument("--overwrite", action="store_true", help="refaz arquivos já existentes no destino")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _raise_interrupt)
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:  # respeita o nohup
        signal.signal(signal.SIGHUP, _raise_interrupt)

    # ---------- verificações iniciais
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("ffmpeg/ffprobe não encontrado. Instale com: sudo apt install ffmpeg")
    cpu_crf = args.crf if args.crf is not None else DEFAULT_CRF[args.codec]
    cpu_enc = make_encoder("cpu", args.codec, cpu_crf)
    err = encoder_works(cpu_enc, "ultrafast")
    if err:
        sys.exit(f"O ffmpeg instalado não consegue usar {cpu_enc['name']}: {err}")

    src_root = args.input.expanduser().resolve()
    dst_root = args.output.expanduser().resolve()
    if not src_root.is_dir():
        sys.exit(f"Pasta de origem não existe: {src_root}")
    if not os.access(src_root, os.R_OK | os.X_OK):
        sys.exit(f"Sem permissão de leitura na origem: {src_root}")
    if src_root == dst_root:
        sys.exit("Origem e destino não podem ser a mesma pasta.")
    if dst_root in src_root.parents:
        sys.exit("O destino não pode conter a origem.")
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
        probe_file = dst_root / (".write_test" + TMP_TAG)
        probe_file.write_bytes(b"ok")
        probe_file.unlink()
    except OSError as e:
        hint = ""
        if e.errno == errno.EROFS and fs_type(dst_root).startswith(("ntfs", "fuseblk")):
            hint = (" — NTFS somente leitura: desligue o Windows por completo "
                    "(sem hibernar/Inicialização rápida) ou rode 'sudo ntfsfix -d'")
        sys.exit(f"Não consigo gravar no destino {dst_root}: {e.strerror}{hint}")

    lock_fd, lock_path = acquire_lock(dst_root)

    # limpa temporários de execuções interrompidas
    leftovers = [p for p in dst_root.rglob(f"*{TMP_TAG}*") if p.is_file()]
    leftovers += [p for p in dst_root.rglob("*.part.mp4") if p.is_file()]
    for p in leftovers:
        p.unlink(missing_ok=True)
    if leftovers:
        print(f"Removidos {len(leftovers)} temporário(s) de uma execução interrompida.")

    if src_root in dst_root.parents:
        print(f"Aviso: destino está dentro da origem; {dst_root} será ignorado na leitura.")

    dfs = fs_type(dst_root)
    if dfs in ("vfat", "msdos"):
        print("Aviso: destino é FAT32 — arquivos acima de 4GB não cabem.")

    # ---------- GPU
    gpu_enc = None
    if args.gpu:
        print(f"Testando GPU ({args.gpu})...")
        gpu_enc, report = detect_gpu(args.gpu, args.codec, args.crf, args.preset)
        if gpu_enc:
            dev = f" em {gpu_enc['device']}" if gpu_enc["device"] else ""
            print(f"GPU: usando {gpu_enc['name']} (qualidade {gpu_enc['q']}){dev}")
            if gpu_enc["codec"] != args.codec:
                print(f"   obs.: esta GPU não faz {args.codec}; usando {gpu_enc['codec']} "
                      "(mais rápido, mas arquivos maiores)")
        else:
            print("GPU: nenhuma utilizável — rodando na CPU.")
            for r in report:
                print(f"   - {r}")

    # ---------- varredura
    def ignored(p: Path) -> bool:
        return (
            dst_root in p.parents
            or TMP_TAG in p.name
            or p.name.endswith(".part.mp4")
        )

    all_files = []
    for p in src_root.rglob("*"):
        try:
            if p.is_file() and not ignored(p):
                all_files.append(p)
        except OSError:
            pass
    all_files.sort()
    if not all_files:
        sys.exit(f"Nenhum arquivo encontrado em {src_root}")
    videos = [p for p in all_files if p.suffix.lower() in EXTS]
    others = [] if args.no_copy else [p for p in all_files if p.suffix.lower() not in EXTS]

    def target_for_other(src):
        return dst_root / src.relative_to(src_root)

    def already_there(dst, size=None):
        for d in (dst, dst.with_name(sanitize(dst.name))):
            if d.exists() and (size is None or d.stat().st_size == size):
                return True
        return False

    copy_queue = [
        s for s in others
        if args.overwrite or not already_there(target_for_other(s), s.stat().st_size)
    ]

    taken = set()
    queue = []
    checked_ok = redo = 0
    if not args.overwrite:
        print("Verificando integridade dos vídeos já convertidos...")
    for src in videos:
        rel = src.relative_to(src_root)
        dst = dst_root / rel.with_suffix(".mp4")
        if dst in taken:  # a.mkv e a.mp4 na mesma pasta -> a.mkv.mp4
            dst = dst_root / rel.parent / (rel.name + ".mp4")
        taken.add(dst)
        if not args.overwrite:
            existing = [d for d in (dst, dst.with_name(sanitize(dst.name))) if d.exists()]
            if existing:
                src_dur, _ = probe(src)
                bad = None
                for d in existing:
                    try:
                        verify_output(d, src_dur)
                        check_tail(d)
                        bad = None
                        break
                    except VerifyError as e:
                        bad = (d, e)
                if bad is None:
                    checked_ok += 1
                    continue
                print(f"Convertido com problema, refazendo: {bad[0].relative_to(dst_root)} ({bad[1]})")
                for d in existing:
                    d.unlink(missing_ok=True)
                redo += 1
        queue.append([src, dst, None, None])
    if checked_ok or redo:
        print(f"Já convertidos verificados: {checked_ok} ok | {redo} corrompidos (serão refeitos)")

    # estimativa de espaço
    need = sum(s.stat().st_size for s in copy_queue)
    need += int(sum(q[0].stat().st_size for q in queue) * EST_RATIO)
    free = free_space(dst_root)
    print(
        f"\nDestino: {dst_root} ({dfs}) | livre: {human(free)} | "
        f"estimado necessário: ~{human(need)}"
    )
    if 0 <= free < need + MIN_FREE:
        print("AVISO: pode faltar espaço. Se encher, o processo para sozinho e dá para continuar depois.")

    copied = copy_skipped = 0
    not_video = 0
    failures = []  # (caminho relativo, motivo)
    stopped = None
    total_in = total_out = 0
    t_start = time.time()
    gpu_fail_streak = 0

    def summary():
        print("\n===== Resumo =====")
        print(f"Tempo: {hms(time.time() - t_start)}")
        if others or copied:
            print(f"Outros arquivos: {copied} copiados | {len(others) - len(copy_queue)} já existiam")
        if total_in:
            print(
                f"Vídeos: entrada {human(total_in)} | saída {human(total_out)} | "
                f"redução {(1 - total_out / total_in) * 100:.1f}%"
            )
        if not_video:
            print(f"Arquivos de 'vídeo' sem imagem copiados como estão: {not_video}")
        if failures:
            print(f"Falhas ({len(failures)}):")
            for rel, why in failures:
                print(f"   - {rel}: {why}")
        print(f"Destino: {dst_root}")

    try:
        # ---------- 1) copia o que não é vídeo
        for src in copy_queue:
            dst = target_for_other(src)
            rel = src.relative_to(src_root)
            for attempt in range(args.retries + 1):
                try:
                    copy_atomic(src, dst)
                    copied += 1
                    break
                except OSError as e:
                    action, hints, wait = classify(e, src, dst, src_root, dst_root, False)
                    if action == "fatal":
                        raise Fatal(f"ao copiar {rel}: " + "; ".join(hints))
                    if action == "name" and dst.name != sanitize(dst.name):
                        dst = dst.with_name(sanitize(dst.name))
                        print(f"   {rel}: nome ajustado para '{dst.name}'")
                        continue
                    if action == "retry" and attempt < args.retries:
                        time.sleep(wait or RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
                        continue
                    failures.append((str(rel), f"cópia: {e.strerror or e} ({'; '.join(hints[1:])})"))
                    break
        if others:
            print(f"Outros arquivos: {copied} copiados | {len(others) - len(copy_queue)} já existiam")

        # ---------- 2) vídeos
        for item in queue:
            item[2], item[3] = probe(item[0])
        total_dur = sum(q[2] for q in queue)
        print(
            f"Vídeos: {len(videos)} | {len(videos) - len(queue)} já convertidos | "
            f"{len(queue)} na fila ({hms(total_dur)} de vídeo)"
        )
        if queue:
            e0 = gpu_enc or cpu_enc
            extra = f" preset={args.preset}" if e0["kind"] == "cpu" else ""
            print(
                f"encoder={e0['name']} qualidade={e0['q']}{extra}"
                f"{f' max-height={args.max_height}' if args.max_height else ''}\n"
            )

        done_dur = 0.0
        attempts = args.retries + 1
        for idx, (src, dst, dur, has_video) in enumerate(queue, 1):
            rel = src.relative_to(src_root)
            if not dir_ok(src_root):
                raise Fatal("pasta/disco de ORIGEM inacessível (desconectado?)")
            if not dir_ok(dst_root):
                raise Fatal("pasta/disco de DESTINO inacessível (desconectado?)")
            try:
                size_txt = human(src.stat().st_size)
            except OSError:
                size_txt = "?"
            print(f"[{idx}/{len(queue)}] {rel}  ({size_txt}, {hms(dur)})")

            enc = gpu_enc or cpu_enc
            ok = False
            give_up = "not_video" if has_video is False else None
            for attempt in range(1, attempts + 1 if give_up is None else 1):
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.stem + TMP_TAG + ".mp4")
                cmd = build_cmd(src, tmp, enc, args.preset, args.max_height, args.audio_bitrate)
                try:
                    before = file_sig(src)
                    encode(cmd, dur, done_dur, total_dur, t_start, args.stall_timeout)
                    if file_sig(src) != before:
                        raise SourceChanged("origem mudou durante a conversão")
                    verify_output(tmp, dur)
                    os.replace(tmp, dst)
                    ok = True
                    if enc["kind"] != "cpu":
                        gpu_fail_streak = 0
                    break
                except (EncodeError, VerifyError, SourceChanged, OSError) as e:
                    tmp.unlink(missing_ok=True)
                    on_gpu = enc["kind"] != "cpu"
                    action, hints, wait = classify(e, src, dst, src_root, dst_root, on_gpu)
                    print(f"   ERRO (tentativa {attempt}/{attempts}) [{enc['name']}]: {e}")
                    for h in hints:
                        print(f"      - {h}")
                    if action == "fatal":
                        raise Fatal("; ".join(hints))
                    if action == "not_video":
                        give_up = "not_video"
                        break
                    if action == "skip":
                        give_up = hints[-1]
                        break
                    if action == "name" and dst.name != sanitize(dst.name):
                        dst = dst.with_name(sanitize(dst.name))
                        print(f"      nome ajustado para '{dst.name}'")
                    if on_gpu:
                        enc = cpu_enc
                        gpu_fail_streak += 1
                        if gpu_fail_streak >= GPU_FAIL_LIMIT and gpu_enc:
                            print(f"      GPU falhou {GPU_FAIL_LIMIT}x seguidas — desligando a GPU, seguindo só na CPU")
                            gpu_enc = None
                        print("      refazendo na CPU...")
                        continue  # troca para CPU sem esperar
                    give_up = hints[-1]
                    if attempt < attempts:
                        w = max(wait, RETRY_WAITS[min(attempt - 1, len(RETRY_WAITS) - 1)])
                        print(f"      tentando de novo em {w}s...")
                        time.sleep(w)

            done_dur += dur

            if ok:
                s_in, s_out = src.stat().st_size, dst.stat().st_size
                total_in += s_in
                total_out += s_out
                pct = (1 - s_out / s_in) * 100 if s_in else 0
                note = "  (ficou MAIOR que o original — tente --crf mais alto)" if s_out > s_in else ""
                print(f"   -> {human(s_out)}  ({pct:.0f}% de redução){note}")
                continue

            # não deu para converter: copia o original para o destino ficar completo
            fb = dst.with_name(src.name) if src.suffix.lower() != ".mp4" else dst
            try:
                copy_atomic(src, fb)
                copied_txt = "original copiado sem comprimir"
            except OSError as ce:
                action, hints, _ = classify(ce, src, fb, src_root, dst_root, False)
                if action == "fatal":
                    raise Fatal("; ".join(hints))
                copied_txt = f"e não deu para copiar o original: {ce.strerror or ce}"
            if give_up == "not_video":
                not_video += 1
                print("   -> sem faixa de vídeo; copiado como está")
            else:
                print(f"   -> desisti; {copied_txt}")
                failures.append((str(rel), f"{give_up} ({copied_txt})"))

    except Fatal as e:
        stopped = f"PROCESSO PARADO: {e}\nResolva e rode o mesmo comando de novo para continuar."
    except KeyboardInterrupt:
        stopped = "Interrompido. Rode o mesmo comando de novo para continuar de onde parou."
    finally:
        for p in list(dst_root.rglob(f"*{TMP_TAG}*")) if dir_ok(dst_root) else []:
            p.unlink(missing_ok=True)
        try:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    summary()
    if stopped:
        print(f"\n{stopped}")
        sys.exit(1)
    sys.exit(2 if failures else 0)


if __name__ == "__main__":
    main()
