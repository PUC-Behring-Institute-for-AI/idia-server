# IDIA Server — Guia de Operações

> **Documento de referência para o mantenedor.** Cobre todos os cenários de
> deploy, desde a instalação de pré-requisitos até a configuração de múltiplos
> modelos, gestão de usuários e monitoramento.
>
> Para uma visão arquitetural do sistema, consulte `docs/ARCHITECTURE.md`.
> Para regras de governança e agentes OpenCode, consulte `AGENTS.md`.

---

## Índice

1. [Visão geral do fluxo](#1-visão-geral-do-fluxo)
2. [Pré-requisitos](#2-pré-requisitos)
3. [Deploy local — servidor no instituto](#3-deploy-local--servidor-no-instituto)
4. [Configuração multi-model](#4-configuração-multi-model)
5. [Deploy na AWS](#5-deploy-na-aws)
6. [Gestão de usuários](#6-gestão-de-usuários)
7. [Monitoramento](#7-monitoramento)
8. [Integração com clientes](#8-integração-com-clientes)
9. [Manutenção](#9-manutenção)
10. [Referência de variáveis de ambiente](#10-referência-de-variáveis-de-ambiente)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Visão geral do fluxo

```
Mantenedor edita .env
        │
        ▼
./idia deploy local  (ou aws)
        │
        ├─ [1/5] render_config.py --render-all
        │         ├─ rendered_serve_config.yaml   → Ray Serve
        │         └─ rendered_litellm_config.yaml → LiteLLM
        │
        ├─ [2/5] docker compose pull (imagens)
        ├─ [3/5] docker compose up -d --build
        ├─ [4/5] wait loop: GET /health (10 min timeout)
        └─ [5/5] smoke_test.sh --wait
                        │
                        ▼
              http://localhost:4000  ✓
```

**Por que `./idia` e não `docker compose up` diretamente?**

O LiteLLM não faz substituição de variáveis de ambiente (`${VAR}`) no seu
arquivo de configuração. Se você rodar `docker compose up` sem o passo de
pré-renderização, os modelos terão o nome literal `"${MODEL_ID}"` e
**100% das requisições falharão** com `model not found`. O `./idia deploy
local` garante que os arquivos renderizados existam antes de subir os
containers.

---

## 2. Pré-requisitos

### 2.1 Hardware

| Cenário | GPU mínima | VRAM mínima | RAM | Disco |
|---------|-----------|-------------|-----|-------|
| Modelo 7-8B (Llama 3.1 8B, Mistral 7B) | 1× NVIDIA GPU | 20 GB | 32 GB | 100 GB |
| Modelo 13-14B (Qwen 2.5 14B) | 1× A100 / 2× A10G | 28 GB | 64 GB | 150 GB |
| Modelo 30B+ | 2-4× A100 / 4× A10G | 60+ GB | 128 GB | 300 GB |
| Desenvolvimento sem GPU (CPU-only) | — | — | 16 GB | 50 GB |

> **Nota:** Modelos rodando em CPU são 50-100× mais lentos. Útil apenas
> para testar o pipeline de configuração, não para uso em produção.

### 2.2 Software — Linux (Ubuntu 22.04+)

```bash
# 1. Docker Engine + Compose v2
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker
docker compose version  # deve mostrar v2.x

# 2. NVIDIA Container Toolkit (para GPU passthrough)
distribution=$(. /etc/os-release && echo "$ID$VERSION_ID")
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L "https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list" | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verificar: deve listar sua GPU
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi

# 3. Python 3.11+
sudo apt-get install -y python3.11 python3-pip
python3 --version  # 3.11.x

# 4. curl e jq
sudo apt-get install -y curl jq
```

### 2.3 Software — macOS (Apple Silicon / Intel)

```bash
# 1. Docker Desktop (com suporte a Compose v2)
# Baixar em: https://www.docker.com/products/docker-desktop/
# Habilitar: Docker Desktop → Settings → Features in development → Enable VirtioFS

# 2. Python 3.11+
brew install python@3.11
python3 --version  # 3.11.x

# 3. curl e jq
brew install curl jq

# Nota: macOS não suporta GPU passthrough para containers.
# O servidor pode ser iniciado sem GPU para testar configuração,
# mas não para inferência em produção.
```

### 2.4 Token HuggingFace

Muitos modelos de LLM são "gated" (exigem aceitação de termos de uso e um
token de acesso). Para obter o token:

1. Criar conta em https://huggingface.co (se não tiver)
2. Ir em https://huggingface.co/settings/tokens
3. Clicar em **New token** → tipo **Read** → copiar o token (`hf_...`)
4. Para modelos Llama (Meta): ir em
   https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct e clicar em
   **Request access** (aprovação automática em minutos)

### 2.5 Verificação final

```bash
# Todos esses comandos devem retornar sem erro:
docker compose version     # Docker Compose v2.x
python3 --version          # Python 3.11+
nvidia-smi                 # Mostra GPU(s) disponíveis
curl --version             # qualquer versão
```

---

## 3. Deploy local — servidor no instituto

### 3.1 Clonar o repositório

```bash
git clone https://github.com/PUC-Behring-Institute-for-AI/idia-server.git
cd idia-server
```

### 3.2 Configurar variáveis de ambiente

```bash
cp .env.example .env
```

Abrir `.env` em um editor e preencher:

```bash
# ──────────────────────────────────────────────────────────────
# OBRIGATÓRIOS — o servidor não sobe sem estes
# ──────────────────────────────────────────────────────────────

# Token HuggingFace para baixar pesos do modelo
# Obter em: https://huggingface.co/settings/tokens
HF_TOKEN=hf_aBcDeFgHiJkLmNoPqRsTuVwXyZ

# Chave master do LiteLLM — usada para criar virtual keys de usuários
# Gerar uma chave segura:
#   python3 -c "import secrets; print('sk-idia-' + secrets.token_hex(16))"
LITELLM_MASTER_KEY=sk-idia-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6

# Nome curto do modelo — é o que os clientes usarão no campo "model"
# Ex: "llama-3.1-8b", "mistral-7b", "qwen-2.5-14b"
MODEL_ID=llama-3.1-8b

# ID completo no HuggingFace Hub — usado para baixar os pesos
# Ex: "meta-llama/Llama-3.1-8B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3"
MODEL_SOURCE=meta-llama/Llama-3.1-8B-Instruct

# ──────────────────────────────────────────────────────────────
# OPCIONAIS — os defaults são adequados para a maioria dos casos
# ──────────────────────────────────────────────────────────────

# Comprimento máximo de contexto em tokens (default: 8192)
# Reduzir para economizar VRAM em GPUs menores
MAX_MODEL_LEN=8192

# Fração de VRAM a reservar para pesos + KV cache (default: 0.9)
# Valores: 0.1 a 1.0 — nunca 1.0 (sistema precisa de overhead)
GPU_MEMORY_UTILIZATION=0.9

# Número de GPUs no servidor (default: 1)
# Usado para validação de VRAM em modo multi-model
GPU_COUNT=1

# VRAM por GPU em GB (default: 24.0 — A10G no g5.xlarge AWS)
# Para outras GPUs: A100 = 80.0, RTX 3090 = 24.0, V100 = 16.0
GPU_VRAM_GB=24.0

# Senha do admin Grafana — troque antes de expor na rede
GRAFANA_ADMIN_PASSWORD=minha-senha-segura
```

> **Segurança:** O arquivo `.env` nunca deve ser commitado. O `.gitignore`
> já o exclui. Confirmar com `git status` — `.env` não deve aparecer.

### 3.3 Deploy (um único comando)

```bash
./idia deploy local
```

**Saída esperada:**

```
══════════════════════════════════════
  IDIA Server — Local Deploy
══════════════════════════════════════

[1/5] Rendering configs (serve_config + litellm_config)...
[✓] rendered_serve_config.yaml
[✓] rendered_litellm_config.yaml
[2/5] Pulling Docker images (skipping build)...
[3/5] Starting services...
[+] Running 5/5
 ✔ Container idia-server-ray-head-1    Started
 ✔ Container idia-server-litellm-1     Started
 ✔ Container idia-server-prometheus-1  Started
 ✔ Container idia-server-grafana-1     Started
[✓] Services started
[4/5] Waiting for server to be ready...
       URL: http://localhost:4000/health
       Timeout: 600s
       ⚠ Note: First boot downloads model weights — this may take 5-15 min

       . 0s elapsed
       . 10s elapsed
       . 20s elapsed
       ...
       . 480s elapsed      ← download + carregamento dos pesos
[✓] Server is ready (490s elapsed)
[5/5] Running smoke test...
[✓] Smoke test passed

══════════════════════════════════════
  IDIA Server — Server Running
══════════════════════════════════════

  API endpoint:  http://localhost:4000
  Grafana:       http://localhost:3000  (admin / $GRAFANA_ADMIN_PASSWORD)

Next steps:
  ./idia user create alice hard       # Create a user (researcher tier)
  ./idia user create bob  regular     # Create a user (grad student tier)
  ./idia status                       # Check all services
  ./idia logs                         # View logs
```

> **Primeiro deploy:** O download dos pesos do Llama 3.1 8B leva de 5 a 15
> minutos dependendo da velocidade da conexão (~16 GB). Deploys subsequentes
> iniciam em 2-3 minutos porque os pesos ficam no volume Docker `idia_hf_cache`.

### 3.4 Validar sem subir (dry-run)

Para verificar se a configuração está correta sem iniciar containers:

```bash
./idia deploy local --dry-run
```

Este comando renderiza os dois arquivos de configuração e os imprime. Útil
para verificar se `MODEL_ID`, `MODEL_SOURCE` e variáveis opcionais estão
sendo aplicados corretamente.

**Inspecionar os rendered configs:**

```bash
# Configuração do Ray Serve (serve_config renderizado)
cat rendered_serve_config.yaml

# Configuração do LiteLLM (gerado dinamicamente por render_config.py)
cat rendered_litellm_config.yaml
```

O `rendered_litellm_config.yaml` deve ter o `model_name` e o `model`
com o valor real de `MODEL_ID` (não `${MODEL_ID}`):

```yaml
model_list:
- litellm_params:
    api_base: http://ray-head:8000/v1
    api_key: no-auth-internal
    model: openai/llama-3.1-8b       # ← valor real, não placeholder
  model_name: llama-3.1-8b           # ← valor real
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  max_parallel_requests: 20
```

### 3.5 Verificar saúde dos serviços

```bash
./idia status
```

**Saída esperada (servidor saudável):**

```
══════════════════════════════════════
  IDIA Server — Status
══════════════════════════════════════

Services:
NAME                           STATUS          PORTS
idia-server-ray-head-1         Up (healthy)
idia-server-litellm-1          Up (healthy)    0.0.0.0:4000->4000/tcp
idia-server-prometheus-1       Up
idia-server-grafana-1          Up              127.0.0.1:3000->3000/tcp

LiteLLM health:
[✓] LiteLLM is healthy

Loaded models:
  • llama-3.1-8b

GPU status:
  GPU: NVIDIA A10G | VRAM: 14352 MiB / 24576 MiB | Util: 0 %
```

### 3.6 Enviar a primeira requisição

```bash
# Testar diretamente com curl (substitua SK pela sua chave master ou virtual)
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-idia-a1b2c3d4..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b",
    "messages": [
      {"role": "user", "content": "Em uma frase, o que é inteligência artificial?"}
    ],
    "temperature": 0.7,
    "max_tokens": 200
  }'
```

**Resposta esperada:**

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "model": "llama-3.1-8b",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Inteligência artificial é o campo da ciência da computação..."
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 22,
    "completion_tokens": 45,
    "total_tokens": 67
  }
}
```

---

## 4. Configuração multi-model

O IDIA Server suporta N modelos simultaneamente. Cada modelo roda como um
deployment independente no Ray Serve, e o LiteLLM roteia para o correto
baseado no campo `model` da requisição.

### 4.1 Requisitos de VRAM

Antes de configurar múltiplos modelos, calcule se a VRAM disponível é suficiente:

```
VRAM necessária = MODELS_COUNT × GPU_MEMORY_UTILIZATION × tamanho_estimado
```

Tamanhos estimados (FP16, sem quantização):

| Modelo | Parâmetros | VRAM mínima |
|--------|-----------|------------|
| Llama 3.1 / Mistral 7B | 7-8B | ~16 GB |
| Qwen 2.5 14B | 14B | ~28 GB |
| Llama 3.1 70B | 70B | ~140 GB |
| Llama 3.1 405B | 405B | ~800 GB |

**Exemplo:** 2 modelos de 8B com `GPU_MEMORY_UTILIZATION=0.9`:
- Necessário: 2 × 0.9 × 16 GB = 28.8 GB
- Viável em: 2× A10G (48 GB total), 1× A100 (80 GB)
- Inviável em: 1× A10G (24 GB) — o `render_config.py` bloqueia o deploy

### 4.2 Editar `.env`

```bash
# Comentar ou remover as variáveis de single-model:
# MODEL_ID=llama-3.1-8b
# MODEL_SOURCE=meta-llama/Llama-3.1-8B-Instruct

# Habilitar modo multi-model:
MODELS_COUNT=2

MODEL_1_ID=llama-3.1-8b
MODEL_1_SOURCE=meta-llama/Llama-3.1-8B-Instruct

MODEL_2_ID=qwen-2.5-7b
MODEL_2_SOURCE=Qwen/Qwen2.5-7B-Instruct

# Ajustar recursos:
GPU_COUNT=2                    # GPUs disponíveis no servidor
GPU_VRAM_GB=24.0               # VRAM de cada GPU
GPU_MEMORY_UTILIZATION=0.85    # Ligeiramente menor para acomodar overhead
```

### 4.3 Re-deploy

```bash
./idia stop
./idia deploy local
```

O `render_config.py` valida automaticamente o orçamento de VRAM antes de
gerar os configs. Se os modelos não couberem, o deploy falha com diagnóstico:

```
FATAL: VRAM budget exceeded.
  Models requested : 2
  Est. VRAM/model  : 16.00 GB (utilization=0.85)
  Total required   : 27.20 GB
  Available (GPUs) : 24.00 GB (1 GPU × 24.00 GB)
  Fix: Reduce MODELS_COUNT, lower GPU_MEMORY_UTILIZATION, or add more GPUs.
```

### 4.4 Verificar os dois modelos

```bash
./idia status
# Deve mostrar:
#   Loaded models:
#     • llama-3.1-8b
#     • qwen-2.5-7b

# Testar ambos:
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model": "llama-3.1-8b", "messages": [{"role":"user","content":"ping"}]}'

curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model": "qwen-2.5-7b", "messages": [{"role":"user","content":"ping"}]}'
```

---

## 5. Deploy na AWS

### 5.1 Pré-requisitos AWS

Além dos pré-requisitos locais (seção 2), você precisará de:

**5.1.1 Conta AWS com permissões**

A conta ou role IAM precisa das seguintes permissões mínimas:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances", "ec2:RunInstances", "ec2:TerminateInstances",
        "ec2:StopInstances", "ec2:StartInstances",
        "ec2:CreateSecurityGroup", "ec2:AuthorizeSecurityGroupIngress",
        "ec2:DescribeSecurityGroups", "ec2:DescribeKeyPairs",
        "ec2:DescribeSubnets", "ec2:DescribeVpcs",
        "ec2:CreateTags", "ec2:DescribeTags",
        "iam:PassRole", "iam:GetRole"
      ],
      "Resource": "*"
    }
  ]
}
```

**5.1.2 Configurar AWS CLI**

```bash
pip install awscli
aws configure
# AWS Access Key ID: AKIA...
# AWS Secret Access Key: ...
# Default region: us-east-1  (ou us-west-2)
# Default output format: json

# Verificar:
aws sts get-caller-identity
# {
#     "UserId": "AIDA...",
#     "Account": "123456789012",
#     "Arn": "arn:aws:iam::123456789012:user/nome-usuario"
# }
```

**5.1.3 Par de chaves EC2**

```bash
# Criar par de chaves (se não tiver):
aws ec2 create-key-pair --key-name idia-server --query 'KeyMaterial' \
    --output text > ~/.ssh/idia-server.pem
chmod 400 ~/.ssh/idia-server.pem

# Se já tiver um par de chaves, ajustar cluster.yaml:
# auth.ssh_private_key: ~/.ssh/seu-arquivo.pem
```

**5.1.4 Solicitação de quota GPU**

Instâncias GPU (g5.xlarge) exigem quota específica. Verificar e solicitar:

```bash
# Verificar quota atual:
aws service-quotas get-service-quota \
    --service-code ec2 \
    --quota-code L-DB2E81BA \
    --region us-east-1
# Procurar "Value" — deve ser > 0

# Se quota = 0, solicitar aumento em:
# https://console.aws.amazon.com/servicequotas/home/services/ec2/quotas
# Quota: "Running On-Demand G and VT instances"
# Valor solicitado: 8 (2 instâncias × 4 vCPUs cada)
# Justificativa: "Research institute LLM serving"
# Prazo de aprovação: 2-5 dias úteis
```

**5.1.5 Ray CLI**

```bash
pip install "ray[default]==2.56.0"
ray --version  # 2.56.0
```

### 5.2 Criar Security Group (recomendado)

O Security Group define quem pode acessar a porta 4000 (API) e 22 (SSH).

```bash
# Configurar faixa de IPs permitidos:
export ALLOWED_IP_RANGE="200.x.x.0/24"    # IP(s) da rede do instituto
export ALLOWED_SSH_RANGE="200.x.x.y/32"   # IP fixo do administrador
export AWS_REGION="us-east-1"

./scripts/create_security_groups.sh

# Saída:
# [✓] Security group created: sg-0a1b2c3d4e5f6g7h
# [✓] Ingress rules added: 4000 (ALLOWED_IP_RANGE), 22 (ALLOWED_SSH_RANGE)

# Exportar para o deploy:
export SG_ID="sg-0a1b2c3d4e5f6g7h"
```

### 5.3 (Opcional) Pre-cachear modelos no S3

Reduz o cold start de cada GPU worker de ~15 minutos para ~2 minutos.

```bash
# Criar bucket (uma vez):
export S3_MODEL_CACHE_BUCKET="idia-models-cache-$(aws sts get-caller-identity \
    --query Account --output text)"
aws s3 mb "s3://$S3_MODEL_CACHE_BUCKET" --region "$AWS_REGION"

# Pré-baixar e fazer upload (necessário HF_TOKEN no .env):
source .env
./idia cache

# Saída:
# Downloading meta-llama/Llama-3.1-8B-Instruct from HuggingFace...
# Uploading to s3://idia-models-cache-123456789012/...
# [✓] Cache complete — cold start reduced from ~15 min to ~2 min
```

Depois, editar `cluster.yaml` para descomentar o S3 sync:

```yaml
# cluster.yaml — descomentar esta seção:
worker_setup_commands:
  - aws s3 sync s3://idia-models-cache-123456789012/ /root/.cache/huggingface/ --quiet
```

### 5.4 Deploy

```bash
# Configurar .env (mesmo que local):
cp .env.example .env && vim .env  # preencher todos os campos

# Deploy:
./idia deploy aws
```

**O que o `deploy_cluster.sh` faz:**

```
[1/5] Validando variáveis de ambiente...
[2/5] Renderizando serve_config.yaml...
       → rendered_config.yaml
[3/5] Criando Security Group...
       → sg-0a1b2c3d4e5f6g7h
[4/5] Iniciando cluster Ray na AWS...
       ray up -y cluster.yaml
       [Cria head node c5.2xlarge + aguarda boot]
       [Copia rendered_config.yaml via file_mounts]
[5/5] Deployando modelo...
       ray exec cluster.yaml "serve run /app/rendered_config.yaml"
       [GPU worker(s) g5.xlarge sobem automaticamente sob demanda]

[✓] Deploy completo
    Endpoint: http://54.x.x.x:4000
```

### 5.5 Post-deploy

```bash
# Obter IP do head node:
HEAD_IP=$(ray get-head-ip cluster.yaml)
echo "API: http://$HEAD_IP:4000"

# Criar usuários no cluster:
./idia user create alice hard    "http://$HEAD_IP:4000"
./idia user create bob   regular "http://$HEAD_IP:4000"

# Verificar modelos carregados:
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    "http://$HEAD_IP:4000/v1/models" | jq '.data[].id'

# Túnel SSH para dashboards:
ssh -i ~/.ssh/idia-server.pem -L 3000:127.0.0.1:3000 ubuntu@$HEAD_IP &
ssh -i ~/.ssh/idia-server.pem -L 8265:127.0.0.1:8265 ubuntu@$HEAD_IP &
# Grafana:       http://localhost:3000
# Ray Dashboard: http://localhost:8265
```

### 5.6 Gerenciar custo AWS

```bash
# Parar cluster (preserva volumes EBS — cobre cold start de ~2 min no próximo boot):
ray down cluster.yaml -y

# Smoke test pós-boot:
./scripts/smoke_test.sh --wait --endpoint "http://$HEAD_IP:4000"
```

**Estimativa de custo mensal (50 usuários, uso misto):**

| Cenário | Instâncias | Custo/hora | Custo/mês |
|---------|-----------|-----------|----------|
| Idle (só head node) | c5.2xlarge | $0.34 | ~$245 |
| 1 modelo ativo (8h/dia útil) | + g5.xlarge | +$1.01/h | +$160 |
| 2 modelos ativos (8h/dia útil) | + 2× g5.xlarge | +$2.01/h | +$320 |
| Scale-to-zero (noite/fim de semana) | — | $0 | — |

> **Proteção de custo:** Configurar um AWS Budget para alertar quando o gasto
> mensal superar um limite:
> ```bash
> aws budgets create-budget \
>   --account-id $(aws sts get-caller-identity --query Account --output text) \
>   --budget '{"BudgetName":"idia-server","BudgetLimit":{"Amount":"500","Unit":"USD"},
>              "TimeUnit":"MONTHLY","BudgetType":"COST"}' \
>   --notifications-with-subscribers '[{"Notification":{"NotificationType":"ACTUAL",
>     "ComparisonOperator":"GREATER_THAN","Threshold":80},
>     "Subscribers":[{"SubscriptionType":"EMAIL","Address":"seu@email.com"}]}]'
> ```

---

## 6. Gestão de usuários

O IDIA Server usa o sistema de virtual keys do LiteLLM. Cada usuário recebe
uma chave única com limites de uso definidos pelo tier.

### 6.1 Tiers disponíveis

| Tier | RPM | TPM | Indicado para |
|------|-----|-----|---------------|
| `hard` | 15 | 50 000 | Pesquisadores, usuários intensivos |
| `regular` | 4 | 15 000 | Mestrandos, estudantes de pós-graduação |
| `light` | 1 | 5 000 | Graduandos, uso ocasional |

### 6.2 Criar usuário

```bash
./idia user create <nome> <tier>
```

Exemplos:

```bash
./idia user create alice hard
# {
#   "key": "sk-idia-user-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6",
#   "key_alias": "alice",
#   "team_id": "hard",
#   "models": ["llama-3.1-8b"],
#   "expires": null
# }

./idia user create carlos regular
./idia user create diana light
```

> **Importante:** A chave é gerada uma única vez e exibida apenas no momento
> da criação. Não há como recuperá-la depois. Armazene em local seguro e
> envie ao usuário por canal seguro (e-mail institucional criptografado ou
> similar).

### 6.3 Listar usuários

```bash
./idia user list
# Active virtual keys:
#   alice (hard) — expires: never
#   carlos (regular) — expires: never
#   diana (light) — expires: never
```

### 6.4 Revogar acesso

LiteLLM permite revogar chaves via API:

```bash
# Revogar chave de um usuário:
curl -X POST http://localhost:4000/key/delete \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-idia-user-a1b2c3d4..."]}'
```

### 6.5 Criar chave com expiração

Para acesso temporário (ex: alunos de um semestre):

```bash
# Calcular data de expiração (ex: fim do semestre, 6 meses):
EXPIRES=$(python3 -c "
from datetime import datetime, timedelta
exp = datetime.utcnow() + timedelta(days=180)
print(exp.strftime('%Y-%m-%dT%H:%M:%S.000Z'))
")

# A API LiteLLM aceita `expires` diretamente:
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"key_alias\": \"estudante-turma-2026\",
    \"team_id\": \"light\",
    \"expires\": \"$EXPIRES\",
    \"models\": [\"llama-3.1-8b\"]
  }"
```

---

## 7. Monitoramento

### 7.1 Grafana (dashboards)

Acessar: **http://localhost:3000** (local) ou via túnel SSH (AWS)

Credenciais: `admin` / `$GRAFANA_ADMIN_PASSWORD`

O dashboard **vLLM Metrics** (provisionado automaticamente) exibe:

| Painel | Métrica | Alerta sugerido |
|--------|---------|-----------------|
| Request Throughput | req/s | < 0.1 req/s por mais de 10 min durante horário de pico |
| Time to First Token | ms P95 | > 5 000 ms |
| Inter-token Latency | ms P95 | > 500 ms |
| GPU KV Cache Hit Rate | % | < 20% (indica que o contexto está grande demais) |
| GPU Memory Usage | % VRAM | > 95% (risco de OOM) |
| Running Requests | contagem | > 50 (possível gargalo de throughput) |

### 7.2 Métricas via CLI

```bash
# Ver todas as métricas Ray Serve expostas:
docker compose exec ray-head curl -s http://localhost:8080/metrics | grep vllm

# Métricas chave:
# vllm:num_requests_running     — requisições em execução no vLLM
# vllm:gpu_cache_usage_perc     — uso do KV cache
# vllm:time_to_first_token_ms   — latência da primeira resposta
```

### 7.3 Logs por serviço

```bash
./idia logs               # todos os serviços (Ctrl+C para sair)
./idia logs ray-head      # Ray Serve + vLLM (inferência)
./idia logs litellm       # LiteLLM (gateway, auth, routing)
./idia logs prometheus    # Prometheus (scraping)
./idia logs grafana       # Grafana (dashboards)
```

### 7.4 Métricas de uso LiteLLM

```bash
# Resumo de uso por chave (últimas 24h):
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    http://localhost:4000/spend/logs?limit=100 | jq .
```

---

## 8. Integração com clientes

O endpoint é compatível com a API OpenAI. Qualquer cliente que suporte
`base_url` personalizado funciona diretamente.

### 8.1 Python — SDK OpenAI

```python
from openai import OpenAI

# Substituir pelo endpoint real e chave do usuário:
client = OpenAI(
    base_url="http://localhost:4000/v1",      # local
    # base_url="http://54.x.x.x:4000/v1",   # AWS
    api_key="sk-idia-user-a1b2c3d4..."
)

# Chat completion:
response = client.chat.completions.create(
    model="llama-3.1-8b",
    messages=[
        {"role": "system", "content": "Você é um assistente de pesquisa especializado em biologia molecular."},
        {"role": "user", "content": "Explique o mecanismo de CRISPR-Cas9."}
    ],
    temperature=0.7,
    max_tokens=1000,
    stream=False  # True para streaming
)

print(response.choices[0].message.content)
print(f"Tokens usados: {response.usage.total_tokens}")
```

**Streaming:**

```python
stream = client.chat.completions.create(
    model="llama-3.1-8b",
    messages=[{"role": "user", "content": "Escreva um resumo sobre RNA mensageiro."}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### 8.2 LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://localhost:4000/v1",
    api_key="sk-idia-user-...",
    model="llama-3.1-8b",
    temperature=0.7
)

response = llm.invoke("Qual a diferença entre RNA e DNA?")
print(response.content)
```

### 8.3 OpenCode / agentes de IA

Para usar o IDIA Server como provider em OpenCode ou outros agentes,
configurar como provider OpenAI-compatible:

```jsonc
// ~/.config/opencode/opencode.json — adicionar provider:
{
  "providers": {
    "idia": {
      "api_key": "sk-idia-user-...",
      "base_url": "http://localhost:4000/v1",
      "name": "IDIA Server (local)"
    }
  },
  "model": "idia/llama-3.1-8b"
}
```

### 8.4 curl (scripts de automação)

```bash
#!/usr/bin/env bash
# Exemplo de script de automação usando o IDIA Server

IDIA_ENDPOINT="http://localhost:4000"
IDIA_KEY="sk-idia-user-..."
MODEL="llama-3.1-8b"

query_llm() {
    local prompt="$1"
    curl -sf "$IDIA_ENDPOINT/v1/chat/completions" \
        -H "Authorization: Bearer $IDIA_KEY" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"$MODEL\",
            \"messages\": [{\"role\": \"user\", \"content\": \"$prompt\"}],
            \"temperature\": 0.3,
            \"max_tokens\": 500
        }" | jq -r '.choices[0].message.content'
}

# Uso:
result=$(query_llm "Resuma em 2 frases: o que é machine learning?")
echo "$result"
```

---

## 9. Manutenção

### 9.1 Trocar o modelo

```bash
# Editar .env:
MODEL_ID=mistral-7b
MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3

# Re-deploy:
./idia stop && ./idia deploy local

# Verificar: o volume idia_hf_cache é preservado entre deploys.
# Se o novo modelo não estiver em cache, será baixado automaticamente.
```

### 9.2 Atualizar o servidor (nova versão do repositório)

```bash
git pull origin main

# Re-renderizar e reiniciar:
./idia stop
./idia deploy local
```

> **Nota:** Se `Dockerfile.ray` foi atualizado, a imagem será reconstruída
> automaticamente pelo `docker compose up --build`.

### 9.3 Limpar cache de modelos

```bash
# Listar volumes:
docker volume ls | grep idia

# Remover cache HuggingFace (força re-download no próximo boot):
docker volume rm idia_hf_cache

# Remover todos os volumes (dados de métricas também):
./idia stop && docker compose down -v
```

### 9.4 Backup das chaves de usuários

As virtual keys do LiteLLM são armazenadas em memória (por padrão). Em caso
de restart, todas as chaves são perdidas. Para persistência:

```bash
# Exportar chaves antes de parar:
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    http://localhost:4000/key/info > backup_keys_$(date +%Y%m%d).json

# Após restart, recriar chaves a partir do backup.
# (LiteLLM Pro suporta banco de dados para persistência — ver docs oficiais)
```

### 9.5 Verificar consistência dos configs

```bash
# Rodar a suíte de testes de configuração (não requer GPU, ~5 segundos):
pip install pytest pyyaml
pytest tests/ -m "config or docs or security" -v

# Esperado: todos passam sem infraestrutura
```

---

## 10. Referência de variáveis de ambiente

| Variável | Obrigatória | Default | Descrição |
|----------|------------|---------|-----------|
| `HF_TOKEN` | Sim | — | Token HuggingFace para baixar modelos gated |
| `LITELLM_MASTER_KEY` | Sim | — | Chave admin LiteLLM para criar virtual keys |
| `MODEL_ID` | Sim* | — | Alias do modelo (usado pelos clientes no campo `model`) |
| `MODEL_SOURCE` | Sim* | — | ID do modelo no HuggingFace Hub |
| `MODELS_COUNT` | Não | 0 | Número de modelos em modo multi-model (0 = single) |
| `MODEL_N_ID` | Condicional | — | Alias do N-ésimo modelo (quando `MODELS_COUNT > 0`) |
| `MODEL_N_SOURCE` | Condicional | — | ID HF do N-ésimo modelo (quando `MODELS_COUNT > 0`) |
| `MAX_MODEL_LEN` | Não | 8192 | Comprimento máximo de contexto em tokens |
| `GPU_MEMORY_UTILIZATION` | Não | 0.9 | Fração de VRAM reservada (0.0–1.0) |
| `GPU_COUNT` | Não | 1 | Número de GPUs (usado para validação VRAM) |
| `GPU_VRAM_GB` | Não | 24.0 | VRAM por GPU em GB |
| `GRAFANA_ADMIN_PASSWORD` | Não | — | Senha admin Grafana |
| `RAY_MEMORY_LIMIT` | Não | 16g | Limite de RAM para o container Ray head |
| `RAY_MEMORY_RESERVATION` | Não | 8g | Reserva de RAM para o container Ray head |
| `RAY_SHM_SIZE` | Não | 4gb | Tamanho do shared memory para comunicação Ray |

(*) Obrigatória em modo single-model. Desnecessária quando `MODELS_COUNT > 0`.

---

## 11. Troubleshooting

### "model not found" em todas as requisições

**Causa:** `docker compose up` foi executado diretamente, sem pre-renderizar
os configs. O `config.yaml` do LiteLLM contém `${MODEL_ID}` como texto
literal, e o LiteLLM tenta rotear para um modelo chamado `"${MODEL_ID}"`.

**Solução:**
```bash
./idia stop
./idia deploy local   # pré-renderiza antes de subir
```

### Timeout no step 4/5 (wait loop)

**Causa A:** Primeiro deploy com modelo grande — download normal.
```bash
# Verificar progresso do download:
./idia logs ray-head | grep -E "Downloading|Loading|model"
```

**Causa B:** `HF_TOKEN` inválido ou modelo gated sem acesso aprovado.
```bash
# Testar token diretamente:
curl -H "Authorization: Bearer $HF_TOKEN" \
    "https://huggingface.co/api/models/meta-llama/Llama-3.1-8B-Instruct"
# Se retornar 401, o token está inválido.
# Se retornar 403, aceitar os termos em: https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct
```

**Causa C:** VRAM insuficiente — vLLM falha com OOM e Ray entra em crashloop.
```bash
# Verificar se há OOM nos logs:
./idia logs ray-head | grep -iE "out of memory|CUDA error|OOM"
# Se sim: reduzir GPU_MEMORY_UTILIZATION ou usar modelo menor
```

### "FATAL: VRAM budget exceeded"

**Causa:** Configuração multi-model com modelos que não cabem nas GPUs disponíveis.

**Solução:** Ajustar em `.env`:
- Reduzir `MODELS_COUNT`
- Reduzir `GPU_MEMORY_UTILIZATION` (ex: 0.9 → 0.7)
- Usar modelos menores
- Aumentar `GPU_COUNT` se houver mais GPUs

### 401 Unauthorized

**Causa:** Chave inválida, expirada, ou ausente no header.
```bash
# Verificar chave:
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    http://localhost:4000/v1/models
# Se retornar 200, a master key funciona.

# Verificar se a virtual key existe:
./idia user list
```

### 429 Too Many Requests

**Causa:** Rate limit do tier excedido. Aguardar 60 segundos ou usar tier superior.

### GPU worker AWS não sobe

**Causa:** Quota EC2 insuficiente.
```bash
# Verificar events do autoscaler:
ray exec cluster.yaml "cat /tmp/ray/session_latest/logs/monitor*" | tail -50
# Procurar: "ResourceUnavailableError" ou "InsufficientCapacity"
```

**Solução:** Solicitar quota em
https://console.aws.amazon.com/servicequotas/home/services/ec2/quotas

### Grafana não abre (localhost:3000 recusado)

**Causa:** No deploy local, Grafana está `Up` mas ainda inicializando.
```bash
docker compose ps grafana        # Checar se está "Up"
./idia logs grafana | tail -20   # Ver se há erro de startup
```

**Causa AWS:** Grafana não é exposto externamente — usar túnel SSH:
```bash
ssh -i ~/.ssh/idia-server.pem -L 3000:127.0.0.1:3000 ubuntu@$HEAD_IP
```

### Modelo não aparece no `./idia status` (Loaded models vazio)

**Causa:** Ray Serve em `min_replicas: 0` — nenhuma réplica ativa até a
primeira requisição.

**Solução:** Enviar uma requisição para "acordar" o modelo:
```bash
curl -sf http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model":"llama-3.1-8b","messages":[{"role":"user","content":"ping"}]}'
# Primeira resposta pode demorar 30-90s (cold start do Ray replica)
```

---

*Document version: 1.0 | Created: 2026-06-29 | Maintainer: @anaxsouza*
