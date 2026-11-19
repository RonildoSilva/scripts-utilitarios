#!/usr/bin/env bash
# Instala o Unity Hub (repositório oficial) e, opcionalmente, remove o driver NVIDIA 570.
# Uso: bash ./instalar-unityhub.sh            -> só Unity Hub
#      bash ./instalar-unityhub.sh --purge-570 -> também remove o 570
set -euo pipefail

sudo -v

if [[ "${1:-}" == "--purge-570" ]]; then
  echo ">> Removendo pacotes NVIDIA 570..."
  pkgs=$(dpkg -l | awk '/^ii/ && /570/ {print $2}')
  [[ -n "$pkgs" ]] && sudo apt purge -y $pkgs
  dpkg -l | awk '/^rc/ && /nvidia/ {print $2}' | xargs -r sudo dpkg --purge
fi

echo ">> Adicionando repositório do Unity Hub..."
wget -qO - https://hub.unity3d.com/linux/keys/public \
  | gpg --dearmor | sudo tee /usr/share/keyrings/Unity_Technologies_ApS.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/Unity_Technologies_ApS.gpg] https://hub.unity3d.com/linux/repos/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/unityhub.list >/dev/null

sudo apt update
sudo apt install -y unityhub

echo
echo ">> Verificação:"
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || true
command -v unityhub && echo "Unity Hub instalado."
