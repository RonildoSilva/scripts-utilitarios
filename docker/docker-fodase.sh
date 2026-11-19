#!/usr/bin/env bash
# docker-fodase.sh — menu interativo para arrumar (ou detonar) o Docker local.
#
# O que dá para fazer:
#   - ver uso de disco (resumo e detalhado)
#   - listar, parar e remover containers
#   - separar volumes órfãos (nenhum container usa) dos que estão em uso, e remover
#   - separar imagens órfãs (<none>, sem tag) das em uso, e remover
#   - achar redes sem nenhum container ligado, e remover
#   - prune seguro: limpa só o que ninguém usa e NUNCA toca em volumes
#   - APAGAR TUDO: containers, imagens, volumes, redes e cache de build,
#     ao som de ../midia/fah-perfect-sync.mp3
#
# Toda remoção mostra antes o que vai sumir e pede confirmação.
#
# Uso: ./docker-fodase.sh
# Requer: docker acessível pelo seu usuário (grupo docker) ou rodar com sudo.
# Áudio: usa o primeiro que achar entre ffplay, mpv, mpg123, cvlc, pw-play, paplay.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIO="$SCRIPT_DIR/../midia/fah-perfect-sync.mp3"

# Redes que o Docker cria sozinho e não deixa remover.
REDES_PADRAO="bridge host none"

# ───────────────────────────── aparência ─────────────────────────────
if [ -t 1 ]; then
  N=$'\e[1m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; C=$'\e[36m'; D=$'\e[2m'; X=$'\e[0m'
else
  N=; R=; G=; Y=; C=; D=; X=
fi

titulo() { printf '\n%s━━ %s ━━%s\n' "$C$N" "$1" "$X"; }
ok()     { printf '%s✔ %s%s\n' "$G" "$1" "$X"; }
aviso()  { printf '%s⚠ %s%s\n' "$Y" "$1" "$X"; }
erro()   { printf '%s✘ %s%s\n' "$R" "$1" "$X" >&2; }
pausa()  { printf '\n%sEnter para voltar ao menu...%s' "$D" "$X"; read -r _; }

# confirma "pergunta" -> 0 se respondeu s/S/sim
confirma() {
  local r
  printf '%s%s [s/N] %s' "$Y" "$1" "$X"
  read -r r
  [[ "$r" =~ ^([sS]|[sS][iI][mM])$ ]]
}

# escolhe "rótulo" item1 item2 ... -> preenche o array ESCOLHIDOS.
# Aceita números separados por espaço/vírgula, intervalos (2-5) ou "t" para todos.
ESCOLHIDOS=()
escolhe() {
  local rotulo="$1"; shift
  local itens=("$@") entrada tok i a b
  ESCOLHIDOS=()
  [ ${#itens[@]} -eq 0 ] && return 1
  printf '%sQuais %s? (ex.: 1 3 5-7, t = todos, Enter = cancelar): %s' "$N" "$rotulo" "$X"
  read -r entrada
  [ -z "$entrada" ] && return 1
  if [[ "$entrada" =~ ^[tT]$ ]]; then
    ESCOLHIDOS=("${itens[@]}"); return 0
  fi
  for tok in ${entrada//,/ }; do
    if [[ "$tok" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      a=${BASH_REMATCH[1]}; b=${BASH_REMATCH[2]}
    elif [[ "$tok" =~ ^[0-9]+$ ]]; then
      a=$tok; b=$tok
    else
      aviso "ignorando '$tok'"; continue
    fi
    for ((i = a; i <= b; i++)); do
      if ((i >= 1 && i <= ${#itens[@]})); then
        ESCOLHIDOS+=("${itens[i-1]}")
      else
        aviso "número $i fora da lista"
      fi
    done
  done
  [ ${#ESCOLHIDOS[@]} -gt 0 ]
}

# numera stdin: "  1) linha"
numera() { awk '{ printf "  %3d) %s\n", NR, $0 }'; }

# ───────────────────────────── checagens ─────────────────────────────
checa_docker() {
  if ! command -v docker >/dev/null; then
    erro "docker não está instalado."; exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    erro "Não consegui falar com o Docker. O serviço está rodando? Seu usuário está no grupo docker?"
    erro "Tente: sudo systemctl start docker   ou   sudo $0"
    exit 1
  fi
}

# ───────────────────────────── disco ─────────────────────────────
uso_disco() {
  titulo "Uso de disco"
  docker system df
  echo
  printf '%sRECLAIMABLE = quanto dá para liberar sem afetar o que está em uso.%s\n' "$D" "$X"
  if confirma "Ver detalhado (por imagem, container e volume)?"; then
    docker system df -v
  fi
  pausa
}

# ───────────────────────────── containers ─────────────────────────────
lista_containers() {
  titulo "Containers"
  local rodando parados
  rodando=$(docker ps -q | wc -l)
  parados=$(docker ps -aq --filter status=exited --filter status=created --filter status=dead | wc -l)
  printf 'Rodando: %s%s%s   Parados: %s%s%s\n\n' "$G" "$rodando" "$X" "$Y" "$parados" "$X"
  docker ps -a --size --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Size}}'
  pausa
}

parar_containers() {
  titulo "Parar containers"
  mapfile -t ids < <(docker ps --format '{{.ID}}')
  if [ ${#ids[@]} -eq 0 ]; then ok "Nenhum container rodando."; pausa; return; fi
  docker ps --format '{{.Names}}  ({{.Image}}, {{.Status}})' | numera
  if escolhe "parar" "${ids[@]}"; then
    docker stop "${ESCOLHIDOS[@]}" >/dev/null && ok "${#ESCOLHIDOS[@]} container(s) parado(s)."
  fi
  pausa
}

remover_containers() {
  titulo "Remover containers"
  echo "  1) só os parados (seguro)"
  echo "  2) escolher da lista (inclui rodando, com força)"
  read -rp "Opção: " op
  case "$op" in
    1)
      mapfile -t ids < <(docker ps -aq --filter status=exited --filter status=created --filter status=dead)
      if [ ${#ids[@]} -eq 0 ]; then ok "Nenhum container parado."; pausa; return; fi
      docker ps -a --filter status=exited --filter status=created --filter status=dead \
        --format '{{.Names}}  ({{.Image}}, {{.Status}})' | numera
      confirma "Remover esses ${#ids[@]}?" && docker rm "${ids[@]}" >/dev/null && ok "Removidos."
      ;;
    2)
      mapfile -t ids < <(docker ps -a --format '{{.ID}}')
      if [ ${#ids[@]} -eq 0 ]; then ok "Nenhum container."; pausa; return; fi
      docker ps -a --format '{{.Names}}  ({{.Image}}, {{.Status}})' | numera
      if escolhe "remover" "${ids[@]}" && confirma "Remover ${#ESCOLHIDOS[@]} container(s), parando os que estão rodando?"; then
        docker rm -f "${ESCOLHIDOS[@]}" >/dev/null && ok "Removidos."
      fi
      ;;
  esac
  pausa
}

# ───────────────────────────── volumes ─────────────────────────────
# Imprime "volume<TAB>containers que usam" (vazio = órfão).
volumes_com_uso() {
  local v usa
  for v in $(docker volume ls -q); do
    usa=$(docker ps -a --filter "volume=$v" --format '{{.Names}}' | paste -sd, -)
    printf '%s\t%s\n' "$v" "$usa"
  done
}

volumes() {
  titulo "Volumes"
  local linhas orfaos=() v usa
  mapfile -t linhas < <(volumes_com_uso)
  if [ ${#linhas[@]} -eq 0 ]; then ok "Nenhum volume."; pausa; return; fi

  printf '%sEm uso%s (algum container, mesmo parado, aponta para ele):\n' "$G$N" "$X"
  for l in "${linhas[@]}"; do
    v=${l%%$'\t'*}; usa=${l#*$'\t'}
    [ -n "$usa" ] && printf '  %s  %s← %s%s\n' "$v" "$D" "$usa" "$X"
  done
  printf '\n%sÓrfãos%s (nenhum container usa):\n' "$Y$N" "$X"
  for l in "${linhas[@]}"; do
    v=${l%%$'\t'*}; usa=${l#*$'\t'}
    [ -z "$usa" ] && orfaos+=("$v")
  done
  if [ ${#orfaos[@]} -eq 0 ]; then echo "  (nenhum)"; else printf '%s\n' "${orfaos[@]}" | numera; fi

  printf '\n%sTamanhos: opção "Uso de disco" > detalhado.%s\n' "$D" "$X"
  echo
  echo "  1) remover órfãos escolhidos"
  echo "  2) remover TODOS os órfãos"
  echo "  Enter) voltar"
  read -rp "Opção: " op
  case "$op" in
    1) if escolhe "volumes" "${orfaos[@]}" && confirma "Apagar ${#ESCOLHIDOS[@]} volume(s)? Os DADOS somem para sempre."; then
         docker volume rm "${ESCOLHIDOS[@]}" >/dev/null && ok "Removidos."
       fi ;;
    2) if [ ${#orfaos[@]} -gt 0 ] && confirma "Apagar os ${#orfaos[@]} volumes órfãos? Os DADOS somem para sempre."; then
         docker volume rm "${orfaos[@]}" >/dev/null && ok "Removidos."
       fi ;;
  esac
  pausa
}

# ───────────────────────────── imagens ─────────────────────────────
imagens() {
  titulo "Imagens"
  local orfas
  orfas=$(docker images -qf dangling=true | wc -l)
  echo "Órfãs (<none>, sem tag, sobra de build ou de pull): $orfas"
  echo
  docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedSince}}'
  echo
  echo "  1) remover imagens órfãs"
  echo "  2) remover imagens sem nenhum container usando (inclui as com tag)"
  echo "  3) escolher da lista"
  echo "  Enter) voltar"
  read -rp "Opção: " op
  case "$op" in
    1) confirma "Remover $orfas imagem(ns) órfã(s)?" && docker image prune -f ;;
    2) aviso "Imagens com tag que nenhum container usa também vão embora (terá que baixar/buildar de novo)."
       confirma "Continuar?" && docker image prune -af ;;
    3) mapfile -t ids < <(docker images --format '{{.ID}}')
       docker images --format '{{.Repository}}:{{.Tag}}  ({{.Size}})' | numera
       if escolhe "imagens" "${ids[@]}" && confirma "Remover ${#ESCOLHIDOS[@]} imagem(ns)?"; then
         docker rmi "${ESCOLHIDOS[@]}" && ok "Removidas."
       fi ;;
  esac
  pausa
}

# ───────────────────────────── redes ─────────────────────────────
redes() {
  titulo "Redes"
  local n usa sem_uso=()
  printf '%-30s %-10s %s\n' "REDE" "DRIVER" "CONTAINERS"
  while IFS=$'\t' read -r n drv; do
    if [[ " $REDES_PADRAO " == *" $n "* ]]; then
      printf '%-30s %-10s %s\n' "$n" "$drv" "${D}(padrão do Docker, não remove)${X}"
      continue
    fi
    usa=$(docker ps -a --filter "network=$n" --format '{{.Names}}' | paste -sd, -)
    if [ -n "$usa" ]; then
      printf '%-30s %-10s %s\n' "$n" "$drv" "${G}$usa${X}"
    else
      printf '%-30s %-10s %s\n' "$n" "$drv" "${Y}nenhum (sem uso)${X}"
      sem_uso+=("$n")
    fi
  done < <(docker network ls --format '{{.Name}}\t{{.Driver}}')

  echo
  if [ ${#sem_uso[@]} -eq 0 ]; then ok "Nenhuma rede sem uso."; pausa; return; fi
  echo "Sem uso:"
  printf '%s\n' "${sem_uso[@]}" | numera
  echo
  echo "  1) remover escolhidas"
  echo "  2) remover todas sem uso"
  echo "  Enter) voltar"
  read -rp "Opção: " op
  case "$op" in
    1) if escolhe "redes" "${sem_uso[@]}" && confirma "Remover ${#ESCOLHIDOS[@]} rede(s)?"; then
         docker network rm "${ESCOLHIDOS[@]}" >/dev/null && ok "Removidas."
       fi ;;
    2) confirma "Remover as ${#sem_uso[@]} redes sem uso?" &&
         docker network rm "${sem_uso[@]}" >/dev/null && ok "Removidas." ;;
  esac
  pausa
}

# ───────────────────────────── prune seguro ─────────────────────────────
prune_seguro() {
  titulo "Prune seguro"
  cat <<EOF
Vai remover só o que ninguém está usando:
  • containers parados
  • imagens órfãs (<none>), mas não imagens com tag
  • redes sem nenhum container
  • cache de build
${G}NÃO toca em volumes${X}, então os dados de bancos etc. ficam.

Antes:
EOF
  docker system df
  echo
  if confirma "Fazer o prune seguro?"; then
    docker container prune -f
    docker image prune -f
    docker network prune -f
    docker builder prune -f
    echo
    ok "Pronto. Depois:"
    docker system df
  fi
  pausa
}

# ───────────────────────────── apagar tudo ─────────────────────────────
toca_audio() {
  [ -f "$AUDIO" ] || { aviso "Áudio não encontrado em $AUDIO"; return; }
  if   command -v ffplay  >/dev/null; then ffplay -nodisp -autoexit -loglevel quiet "$AUDIO" &
  elif command -v mpv     >/dev/null; then mpv --no-video --really-quiet "$AUDIO" &
  elif command -v mpg123  >/dev/null; then mpg123 -q "$AUDIO" &
  elif command -v cvlc    >/dev/null; then cvlc --play-and-exit --quiet "$AUDIO" &
  elif command -v pw-play >/dev/null; then pw-play "$AUDIO" &
  elif command -v paplay  >/dev/null; then paplay "$AUDIO" &
  else aviso "Nenhum player de áudio encontrado (instale ffmpeg ou mpv)."; return
  fi
  disown 2>/dev/null || true
} >/dev/null 2>&1

apagar_tudo() {
  titulo "${R}APAGAR TUDO${C}"
  docker system df
  cat <<EOF

${R}${N}Isso vai:${X}
  • parar e remover ${N}todos${X} os containers
  • remover ${N}todas${X} as imagens
  • remover ${N}todos${X} os volumes, com os dados de bancos e tudo mais
  • remover todas as redes criadas e todo o cache de build

${R}Não tem como desfazer.${X}
EOF
  confirma "Tem certeza?" || { pausa; return; }
  local r
  printf '%sDigite %sfodase%s%s para confirmar: %s' "$R" "$N" "$X" "$R" "$X"
  read -r r
  [ "$r" = "fodase" ] || { aviso "Cancelado."; pausa; return; }

  toca_audio
  local ids
  mapfile -t ids < <(docker ps -aq)
  [ ${#ids[@]} -gt 0 ] && docker rm -f "${ids[@]}" >/dev/null
  mapfile -t ids < <(docker volume ls -q)
  [ ${#ids[@]} -gt 0 ] && docker volume rm -f "${ids[@]}" >/dev/null
  docker system prune -af --volumes >/dev/null
  docker builder prune -af >/dev/null 2>&1
  echo
  ok "Foi tudo. Estado atual:"
  docker system df
  pausa
}

# ───────────────────────────── menu ─────────────────────────────
menu() {
  while true; do
    clear 2>/dev/null || true
    printf '%s🐳 docker-fodase%s  %s(%s containers rodando, %s no total)%s\n\n' \
      "$C$N" "$X" "$D" "$(docker ps -q | wc -l)" "$(docker ps -aq | wc -l)" "$X"
    cat <<EOF
  1) Uso de disco
  2) Listar containers
  3) Parar containers
  4) Remover containers
  5) Volumes (órfãos x em uso)
  6) Imagens (órfãs x em uso)
  7) Redes (sem uso x em uso)
  8) Prune seguro (não toca em volumes)
  ${R}9) APAGAR TUDO${X}
  0) Sair
EOF
    read -rp $'\nOpção: ' op
    case "$op" in
      1) uso_disco ;;
      2) lista_containers ;;
      3) parar_containers ;;
      4) remover_containers ;;
      5) volumes ;;
      6) imagens ;;
      7) redes ;;
      8) prune_seguro ;;
      9) apagar_tudo ;;
      0|q|Q) exit 0 ;;
      *) aviso "Opção inválida"; sleep 1 ;;
    esac
  done
}

checa_docker
menu
