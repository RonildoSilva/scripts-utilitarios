# scripts-utilitarios

Scripts e anotações soltas para manter o notebook (Dell Inspiron 5480, Ubuntu 24.04)
e fazer backup do celular. Cada script tem no topo um cabeçalho dizendo o que faz e
como usar. Esta página é só o índice.

## Instalação

Os scripts Python usam só a biblioteca padrão, exceto `midia/imagens.py` (Pillow). Dependências extras:

```bash
sudo apt install python3-gi    # opcional: deixa o backup do celular bem mais rápido
sudo apt install ffmpeg        # necessário para midia/comprimir_videos.py (o ffplay dele também toca o som do docker-fodase)
sudo apt install python3-pil   # necessário para midia/imagens.py
sudo apt install texlive-latex-recommended   # só para o --pdf do documentos/csv_para_latex.py
```

Para usar um venv em vez do apt, veja `requirements.txt`.

## backup-celular/

| Arquivo | O que faz |
|---|---|
| `backup_celular_samsung.py` | Backup completo de um celular Android (Samsung A13) via MTP/gvfs. Copia por prioridade (mídias de WhatsApp/Telegram/redes sociais primeiro), pode ser retomado, retenta quando o MTP trava e grava cada arquivo de forma atômica. Ajuste `DEST_ROOT` e `SOURCE_ROOT` no topo. Use `--help` para ver as opções. |
| `versoes-antigas/backup_celular_samsung_2026-08-18.py` | Versão anterior (ago/2026), guardada só como referência. A atual tem tudo dela e mais: barra de progresso, `--desde`, lista de pendências descartáveis e proteção contra o celular bloqueado parecer vazio. |

```bash
python3 backup-celular/backup_celular_samsung.py --dry-run   # ver o que faria
python3 backup-celular/backup_celular_samsung.py             # copiar tudo
```

## docker/

| Arquivo | O que faz |
|---|---|
| `docker-fodase.sh` | Menu interativo para arrumar o Docker local: uso de disco, listar/parar/remover containers, volumes e imagens separados em **órfãos x em uso**, redes sem uso, **prune seguro** (nunca toca em volumes) e **APAGAR TUDO** (pede para digitar `fodase` e toca `midia/fah-perfect-sync.mp3`). Toda remoção mostra antes o que vai sumir e pede confirmação. |

```bash
./docker/docker-fodase.sh
```

Nas listas, escolha por número: `1 3 5-7`, `t` para todos, Enter para cancelar.

## midia/

| Arquivo | O que faz |
|---|---|
| `comprimir_videos.py` | Espelha uma pasta em outra comprimindo os vídeos com ffmpeg (CPU ou GPU) e copiando o resto sem alterar. Pode ser retomado e confere cada vídeo gerado. |
| `fah-perfect-sync.mp3` | Som do "APAGAR TUDO" do `docker/docker-fodase.sh`. Se mover ou renomear, o script não acha. |
| `imagens.py` | Redimensiona mantendo a proporção, corta em grade (padrão 2x2) e junta imagens lado a lado ou empilhadas. Nunca sobrescreve o original. Precisa do Pillow. |

```bash
python3 midia/comprimir_videos.py ORIGEM DESTINO --gpu
python3 midia/imagens.py redimensionar *.jpg --largura 800 -o menores/
python3 midia/imagens.py dividir mapa.png --linhas 3 --colunas 3
python3 midia/imagens.py juntar a.png b.png -o junto.png --vertical
```

## documentos/

| Arquivo | O que faz |
|---|---|
| `csv_para_latex.py` | Converte um CSV em tabela LaTeX (cabeçalho em negrito, escapa `& % $ # _` etc., detecta o separador). Pode destacar uma linha, gerar o `.tex` completo e compilar o PDF. |

```bash
python3 documentos/csv_para_latex.py dados.csv                          # imprime a tabela
python3 documentos/csv_para_latex.py dados.csv -o t.tex --pdf --destacar 3
```

## rede/

| Arquivo | O que faz |
|---|---|
| `servidor_https.py` | Serve uma pasta por HTTPS (como `python3 -m http.server`, mas com TLS). Gera e reaproveita um certificado autoassinado. Serve para testar câmera, microfone e PWA pelo celular. Com `--rede`, aceita conexões da rede local. |

```bash
python3 rede/servidor_https.py ~/meu-site --rede
```

## sistema/

| Arquivo | O que faz |
|---|---|
| `corrigir-limite-energia-cpu.sh` | Destrava o PL1 da CPU (preso em 8 W pelo `thermald --adaptive`) e deixa a MX150 desligar quando ociosa. Corrige o áudio robótico no Meet. `sudo ./corrigir-limite-energia-cpu.sh {teste\|permanente\|reverter}` |
| `instalar-unityhub.sh` | Instala o Unity Hub pelo repositório oficial. Com `--purge-570`, também remove o driver NVIDIA 570. |
| `resetar-indexador-tracker.sh` | Apaga e recria o índice do Tracker (busca de arquivos do GNOME) quando ele trava ou pesa. |
| `desativar-indexador-tracker.sh` | Desliga o Tracker de vez (`systemctl --user mask`). O comando para reverter está no cabeçalho do script. |

## guias/

| Arquivo | Assunto |
|---|---|
| `mysql-ubuntu-importar-sql-grande.md` | Instalar o MySQL no Ubuntu, criar usuário para o DBeaver e importar `.sql` de vários GB. |
| `plano-unreal-engine-4.9.md` | Plano para rodar a Unreal Engine 4.9 neste notebook (driver NVIDIA, kernel 7.0, Docker). |
