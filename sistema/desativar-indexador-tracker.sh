#!/usr/bin/env bash
# Desliga de vez o Tracker (indexador de arquivos do GNOME).
#
# Quando usar: o Tracker continua pesando mesmo depois de resetar
# (resetar-indexador-tracker.sh) e você não precisa da busca de conteúdo
# dentro de arquivos no Nautilus.
#
# O que faz: aplica "mask" nos serviços de usuário do Tracker, impedindo que
# eles iniciem, mesmo se outro programa pedir. Vale a partir do próximo login
# (ou pare os processos agora com: tracker3 daemon -t).
#
# Efeito colateral: a busca do Nautilus passa a achar só pelo nome do arquivo.
#
# Uso: ./desativar-indexador-tracker.sh     (sem sudo)
# Para reverter:
#   systemctl --user unmask tracker-extract-3.service tracker-miner-fs-3.service \
#     tracker-miner-rss-3.service tracker-writeback-3.service \
#     tracker-xdg-portal-3.service tracker-miner-fs-control-3.service

systemctl --user mask \
  tracker-extract-3.service \
  tracker-miner-fs-3.service \
  tracker-miner-rss-3.service \
  tracker-writeback-3.service \
  tracker-xdg-portal-3.service \
  tracker-miner-fs-control-3.service
