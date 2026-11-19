#!/usr/bin/env bash
# Reseta o Tracker (indexador de arquivos do GNOME, usado pela busca do Nautilus).
#
# Quando usar: o tracker-miner-fs-3 está comendo CPU/disco sem parar, a busca
# de arquivos mostra resultados velhos ou o banco de índice corrompeu.
#
# O que faz: para os processos, apaga o banco de índice e sobe tudo de novo.
# Ele vai reindexar a pasta pessoal do zero, então fica pesado por um tempo.
# Nenhum arquivo seu é apagado, só o índice.
#
# Uso: ./resetar-indexador-tracker.sh       (sem sudo, roda como seu usuário)
# Ver também: desativar-indexador-tracker.sh, para desligar o Tracker de vez.

echo "Encerrando os processos do Tracker..."
tracker3 daemon -t

echo "Resetando o banco de dados do sistema de arquivos..."
tracker3 reset -s

echo "Reiniciando os processos do Tracker..."
tracker3 daemon -s

echo "Verificando o status inicial (pode demorar alguns segundos para iniciar a contagem)..."
sleep 2
tracker3 status
