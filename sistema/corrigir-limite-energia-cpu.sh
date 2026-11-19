#!/usr/bin/env bash
# Corrige o PL1 da CPU preso em 8 W (Dell Inspiron 5480, i5-8265U) pelo thermald --adaptive.
# Sintoma: audio robotico no Google Meet com a janela em primeiro plano.
# Uso: sudo ./corrigir-limite-energia-cpu.sh {teste|permanente|reverter}
set -euo pipefail

PL1_UW=15000000   # 15 W (TDP nominal); DPTF da Dell aceita 10,5-22 W
RAPL=/sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw
NVIDIA_PCI=/sys/bus/pci/devices/0000:02:00.0

[ "$(id -u)" -eq 0 ] || { echo "Rode com sudo"; exit 1; }

case "${1:-}" in
  teste)
    # Temporario: volta ao normal no proximo boot
    systemctl stop thermald
    echo "$PL1_UW" > "$RAPL"
    echo "PL1 agora: $(( $(cat "$RAPL") / 1000000 )) W (thermald parado ate o reboot)"
    # MX150 sem driver carregado fica ligada (D0) o tempo todo; deixa o kernel desliga-la
    echo auto > "$NVIDIA_PCI/power/control"
    sleep 2
    echo "MX150: $(cat "$NVIDIA_PCI/power/runtime_status")"
    ;;
  permanente)
    # 1) thermald sem --adaptive (continua protegendo por temperatura, sem aplicar a tabela DPTF que crava 8 W)
    mkdir -p /etc/systemd/system/thermald.service.d
    cat > /etc/systemd/system/thermald.service.d/no-adaptive.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/sbin/thermald --systemd --dbus-enable
EOF

    # 2) Servico que aplica PL1 e desliga a MX150 ociosa no boot e ao voltar da suspensao
    cat > /etc/systemd/system/cpu-power-limit.service <<EOF
[Unit]
Description=PL1 da CPU em $((PL1_UW / 1000000)) W e runtime PM da MX150
After=thermald.service suspend.target hibernate.target hybrid-sleep.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo $PL1_UW > $RAPL; [ -e $NVIDIA_PCI/power/control ] && echo auto > $NVIDIA_PCI/power/control || true'

[Install]
WantedBy=multi-user.target suspend.target hibernate.target hybrid-sleep.target
EOF

    systemctl daemon-reload
    systemctl restart thermald
    systemctl enable --now cpu-power-limit.service
    echo "PL1: $(( $(cat "$RAPL") / 1000000 )) W | thermald: $(systemctl is-active thermald) | MX150: $(cat "$NVIDIA_PCI/power/runtime_status")"
    ;;
  reverter)
    systemctl disable --now cpu-power-limit.service 2>/dev/null || true
    rm -f /etc/systemd/system/cpu-power-limit.service /etc/systemd/system/thermald.service.d/no-adaptive.conf
    rmdir /etc/systemd/system/thermald.service.d 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart thermald
    echo "Revertido. Reinicie para voltar ao estado original."
    ;;
  *)
    echo "Uso: sudo $0 {teste|permanente|reverter}"
    exit 1
    ;;
esac
