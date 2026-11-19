# Plano: Unreal Engine 4.9 no Inspiron 5480 (Ubuntu 24.04, kernel 7.0)

Data: 2026-10-20

## 0. Estado atual (checado)

| Item | Estado |
|---|---|
| CPU / RAM | i5-8265U, 15 GB |
| GPU | Intel UHD 620 + GeForce MX150 (GP108M, Pascal) |
| Driver NVIDIA | Userspace do **570** instalado, mas **sem módulo para o kernel 7.0** (o último `linux-modules-nvidia-570` é para o 6.17.0-22). Por isso a MX150 está sem driver. |
| `prime-select` | `on-demand` (a Intel cuida da tela, a NVIDIA entra só quando é chamada) |
| Pacote recomendado | `nvidia-driver-580`, com **módulo pré-compilado e assinado para 7.0.0-38** (`linux-modules-nvidia-580-generic-hwe-24.04`). Não precisa de DKMS. |
| Secure Boot | desativado |
| Docker | 28.3.2 instalado |
| Disco | 27 GB livres em `/` (91% usado); `~/Downloads` ocupa 50 GB; há 25 kernels antigos em `/lib/modules` |
| `corrigir-limite-energia-cpu.sh` | aplicado (`permanente`): PL1 em 15 W, thermald sem `--adaptive`, MX150 com `power/control=auto` (agora `suspended`) |

## 1. O `corrigir-limite-energia-cpu.sh` conflita com o driver NVIDIA?

**Não há conflito que impeça a instalação.** O script mexe em três coisas, e veja como cada uma se comporta:

1. **PL1 da CPU (RAPL, `intel-rapl-mmio`)**: o driver NVIDIA não mexe nos limites de potência da CPU. Não há conflito.
2. **thermald sem `--adaptive`**: o thermald não depende do driver NVIDIA. Não há conflito.
3. **`echo auto > /sys/bus/pci/devices/0000:02:00.0/power/control`**: é aqui que algo muda, mas não quebra nada.
   - Hoje, sem driver, o kernel consegue suspender a MX150 (`suspended`).
   - Com o driver NVIDIA carregado, quem decide é o driver. O desligamento fino da GPU (RTD3) só existe da Turing em diante, e a MX150 é Pascal. Então o driver tende a **manter a placa ligada (D0)** mesmo com `auto`. A linha do script simplesmente para de ter efeito.
   - Na prática, o consumo em repouso deve subir um pouco (talvez alguns watts) e a bateria vai durar menos. Isso não tem a ver com o problema de áudio/CPU que o script corrigiu.
   - Os pacotes NVIDIA do Ubuntu já instalam uma regra udev que faz o mesmo `power/control=auto`, então a linha fica só redundante.

**Pontos de atenção:**
- Se o pacote **`nvidia-powerd`** (Dynamic Boost) vier junto, desative com `sudo systemctl disable --now nvidia-powerd`. Ele serve para notebooks RTX, não para a MX150, e redistribui potência entre CPU e GPU.
- Com CPU (15 W) e MX150 (~25 W) puxando juntas durante a compilação ou o uso do editor, o notebook vai esquentar. O thermald continua protegendo por temperatura, que é o esperado.
- **Não é preciso reverter nem alterar o script.**

Se a bateria piorar demais no uso normal, há duas saídas: `sudo prime-select intel` quando não estiver usando a Unreal, ou descarregar o módulo `nvidia`.

## 2. Liberar disco (meta: ≥ 80 GB livres)

A UE 4.9 compilada ocupa ~40 a 60 GB, e a imagem Docker mais o cache levam mais alguns GB.

```bash
# Ver o que pesa
du -sh ~/Downloads/* | sort -h | tail -20

# Remover kernels antigos (mantenha o atual 7.0.0-38 e um anterior)
sudo apt autoremove --purge

# Limpar resíduos ("rc") dos módulos NVIDIA 570 antigos
dpkg -l | awk '/^rc/ && /nvidia/ {print $2}' | xargs -r sudo dpkg --purge
```

## 3. Driver NVIDIA 580

```bash
# Remover o 570 quebrado
sudo apt purge 'nvidia-*570*' 'libnvidia-*570*' 'linux-modules-nvidia-570-*'

# Instalar o 580 com módulo pré-compilado para o kernel HWE (7.0)
sudo apt install nvidia-driver-580 linux-modules-nvidia-580-generic-hwe-24.04

# Manter o modo on-demand
sudo prime-select on-demand
sudo reboot
```

Depois do reboot, verifique:

```bash
nvidia-smi                                  # deve listar a MX150
prime-run glxinfo -B | grep renderer        # deve mostrar NVIDIA
cat /sys/class/powercap/intel-rapl-mmio:0/constraint_0_power_limit_uw   # ainda 15000000
systemctl is-active cpu-power-limit thermald
systemctl status nvidia-powerd 2>/dev/null  # se existir, desativar
```

> `prime-run` vem do pacote `nvidia-prime`. Se não existir, use:
> `__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia <comando>`

## 4. Acesso ao código-fonte

1. Crie ou entre numa conta Epic Games e vincule sua conta GitHub (Epic, Account, Apps and Accounts).
2. Aceite o convite para a organização `EpicGames` no GitHub.
3. Clone só a branch da 4.9:
   ```bash
   git clone --depth 1 -b 4.9 https://github.com/EpicGames/UnrealEngine.git ~/UE/UE-4.9
   ```

## 5. Ambiente de build em container (toolchain de 2015)

A 4.9 espera clang 3.5/3.6 e bibliotecas antigas, e no Ubuntu 24.04 a compilação quebra. A ideia é compilar dentro de um Ubuntu 14.04.

```Dockerfile
# ~/UE/docker/Dockerfile
FROM ubuntu:14.04
RUN apt-get update && apt-get install -y \
    build-essential clang-3.5 mono-complete mono-xbuild mono-dmcs \
    libmono-corlib4.0-cil libmono-system-data-datasetextensions4.0-cil \
    libmono-system-web-extensions4.0-cil libmono-system-management4.0-cil \
    libmono-system-xml-linq4.0-cil cmake dos2unix git xdg-user-dirs \
    libqt4-dev libgtk-3-dev libnss3 libxss1 libasound2 \
 && update-alternatives --install /usr/bin/clang clang /usr/bin/clang-3.5 100 \
 && update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-3.5 100
ARG UID=1000
RUN useradd -m -u $UID ue
USER ue
WORKDIR /ue
```

```bash
cd ~/UE/docker && docker build -t ue49-build .
docker run --rm -it -v ~/UE/UE-4.9:/ue ue49-build bash
# dentro do container:
./Setup.sh
./GenerateProjectFiles.sh
make -j6 UE4Editor ShaderCompileWorker UnrealLightmass UnrealPak CrashReportClient
```

**Riscos desta etapa:**
- **`Setup.sh` baixa dependências do CDN da Epic.** Para versões tão antigas, o download pode falhar, e isso é o bloqueio mais provável. Teste o `Setup.sh` primeiro, antes de investir no resto.
- A lista de pacotes do Dockerfile é a da documentação da época e pode precisar de ajustes. Use o `Engine/Build/BatchFiles/Linux/InstallPrereqs.sh` do próprio repositório como referência.
- Compilar num i5-8265U leva **várias horas**. Use `-j6` para sobrar CPU e deixe o notebook na tomada.

## 6. Rodar o editor

Opção A, mais simples: rodar o binário no host com a NVIDIA.

```bash
cd ~/UE/UE-4.9/Engine/Binaries/Linux
prime-run ./UE4Editor
```

Opção B, se faltar alguma biblioteca antiga no host: rodar dentro do container com GPU.
1. Instale o `nvidia-container-toolkit` no host e rode `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`.
2. Rode o container com acesso ao X11 e à GPU:
   ```bash
   xhost +local:docker
   docker run --rm -it --gpus all \
     -e NVIDIA_DRIVER_CAPABILITIES=all -e DISPLAY=$DISPLAY \
     -v /tmp/.X11-unix:/tmp/.X11-unix -v ~/UE/UE-4.9:/ue \
     ue49-build ./Engine/Binaries/Linux/UE4Editor
   ```

A primeira abertura compila shaders e demora bastante. Comece com um projeto em branco ou de template, com qualidade de escalabilidade "Low" (a MX150 tem 2 GB de VRAM).

## 7. Checklist

- [ ] ≥ 80 GB livres
- [ ] 570 removido, 580 instalado, `nvidia-smi` funcionando
- [ ] PL1 ainda em 15 W e `cpu-power-limit.service` ativo após o reboot
- [ ] `nvidia-powerd` ausente ou desativado
- [ ] Conta Epic vinculada ao GitHub e branch 4.9 clonada
- [ ] Imagem `ue49-build` criada e `Setup.sh` OK (**ponto de decisão**: se falhar, avaliar a 4.27)
- [ ] Build concluído
- [ ] Editor abrindo com renderer NVIDIA

## Plano B

Se a 4.9 não for obrigatória, a **UE 4.27** usa um toolchain próprio (o `Setup.sh` baixa um clang embutido), compila direto no Ubuntu 24.04 sem container e tem suporte melhor no Linux. Os passos 2 e 3 continuam iguais.
