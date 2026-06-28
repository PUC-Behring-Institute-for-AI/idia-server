# IDIA Server — Inference & Deployment for Intelligent Agents

**Servidor de inferência LLM auto-hospedado com elasticidade automática de GPU
e carregamento sob demanda de modelos, implantável de forma idêntica em um host
local multi-GPU e na AWS.**

[![Phase](https://img.shields.io/badge/phase-4%20Monitoring-blue)](https://github.com/PUC-Behring-Institute-for-AI/idia-server)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Stack](https://img.shields.io/badge/stack-Ray%20Serve%20%7C%20vLLM%20%7C%20LiteLLM-orange)]()
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

---

- [1. Visão Geral](#1-visão-geral)
- [2. Arquitetura (Resumo Executivo)](#2-arquitetura-resumo-executivo)
- [3. Pré-requisitos](#3-pré-requisitos)
- [4. Início Rápido](#4-início-rápido)
- [5. Estrutura do Repositório](#5-estrutura-do-repositório)
- [6. Roadmap de Implementação](#6-roadmap-de-implementação)
- [7. Guia de Testes](#7-guia-de-testes)
- [8. Configuração](#8-configuração)
- [9. Segurança](#9-segurança)
- [10. Targets de Deploy](#10-targets-de-deploy)
- [11. Monitoramento](#11-monitoramento)
- [12. Mantenedor](#12-mantenedor)
- [13. Licença](#13-licença)
- [14. Referências](#14-referências)

---

## 1. Visão Geral

O **IDIA Server** é a plataforma de inferência de LLM do **PUC-Behring Institute
for AI**. Ele foi projetado para servir modelos de linguagem de grande porte
(LLMs) para múltiplos usuários e aplicações dentro do instituto, com controle
individual de orçamento e taxa de requisições, escalonamento automático de
réplicas (incluindo scale-to-zero para eliminar custo ocioso), e capacidade de
implantação idêntica em um servidor local multi-GPU e na AWS.

### Propriedades-chave

- **Três camadas bem definidas**: LiteLLM (gateway/auth), Ray Serve LLM
  (orquestração GPU), vLLM (motor de inferência) — cada camada com
  responsabilidades estritas e sem vazamento entre elas.
- **Elasticidade automática de GPU**: dois autoscalers independentes operam em
  cascata — o primeiro ajusta réplicas por modelo, o segundo ajusta nós físicos
  na AWS.
- **Scale-to-zero**: modelos ociosos não ocupam GPU. A primeira requisição após
  um período ocioso dispara o carregamento automático (cold start) sem
  intervenção manual.
- **Carregamento sob demanda**: múltiplos modelos podem ser declarados com
  `min_replicas: 0`; cada um é carregado apenas quando recebe a primeira
  requisição.
- **Implantação idêntica local/AWS**: o mesmo `docker-compose.yml` funciona em
  um laptop com GPU e em uma instância EC2. A diferença está apenas no
  `cluster.yaml` para elasticidade de nós.
- **Segurança por isolamento de rede**: apenas a porta 4000 (LiteLLM) é exposta
  ao host. As portas internas do Ray (8000, 8265, 10001) nunca são mapeadas no
  `docker-compose.yml`.
- **Gateway com autenticação e budgets**: o LiteLLM gerencia chaves virtuais
  por usuário/equipe com limites de RPM e orçamento, eliminando a necessidade
  de autenticação no Ray.

Para a especificação arquitetural completa (725+ linhas), consulte
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Este README é um resumo
executivo e guia de operação; a arquitetura é o documento de referência
definitivo.

---

## 2. Arquitetura (Resumo Executivo)

### 2.1 Diagrama de fluxo

```
Cliente (app / script / curl)
        │  HTTPS, OpenAI request format, virtual key
        ▼
┌─────────────────────────┐
│  LiteLLM      (:4000)    │  CPU only — auth, budget, rate-limit, spend tracking
│  ─────────────────────   │
│  Porta ÚNICA exposta     │  ← toda requisição chega aqui
└──────────┬───────────────┘
           │ internal network only — nunca exposto externamente
           ▼
┌─────────────────────────┐
│  Ray Serve LLM (:8000)   │  Autoscaling, GPU placement, multi-model/LoRA routing
│  ─────────────────────   │
│  Porta interna           │  ← inacessível de fora do container
└──────────┬───────────────┘
           │ in-process / mesma GPU — sem barreira de rede
           ▼
┌─────────────────────────┐
│  vLLM engine instance(s) │  GPU — model weights + KV cache, por réplica
└─────────────────────────┘
```

### 2.2 As três camadas

| Camada | Componente | Porta | Responsabilidade | Não é responsabilidade |
|--------|-----------|-------|------------------|----------------------|
| Gateway | **LiteLLM** | `:4000` (externa) | Autenticação, chaves virtuais, budgets por chave, rate limits, registro de gastos, unificação opcional com APIs comerciais | Posicionamento GPU, autoscaling, carregamento de modelo |
| Orquestração | **Ray Serve LLM** | `:8000` (interna) | Autoscaling de réplicas (incluindo scale-to-zero), alocação GPU-aware, roteamento multi-modelo, multiplexação LoRA com evicção LRU | Autenticação por usuário, provedores externos |
| Motor | **vLLM** | in-process | Inferência: pesos em VRAM, KV cache (PagedAttention), continuous batching, geração de tokens | Tudo acima — é um motor single-modelo sem conceito de usuários, chaves ou outros modelos |

### 2.3 Os dois autoscalers

O sistema tem dois loops de autoscaling independentes em granularidades diferentes.
Confundi-los é a fonte mais comum de erro ao raciocinar sobre capacidade e custo.

| | Ray Serve Autoscaler | Ray Cluster Autoscaler |
|---|---|---|
| **Escopo** | Um deployment (um modelo) | O cluster inteiro (todos os nós) |
| **Adiciona/remove** | Réplicas (processos) | Nós (VMs) |
| **Gatilho** | `target_ongoing_requests` excedido para aquele deployment | Demanda agregada de recursos excede o que os nós atuais fornecem |
| **Onde configurar** | `autoscaling_config` dentro de cada `LLMConfig` | `min_workers`/`max_workers` no `cluster.yaml` |
| **Ativo localmente?** | Sim | Não (só em cloud) |

Em um host multi-GPU local, apenas o **autoscaler de réplicas** opera. Na AWS
via Ray Cluster Launcher, ambos operam em sequência: o Ray Serve decide que
precisa de mais uma réplica → se nenhum slot GPU estiver livre → o cluster
autoscaler solicita uma nova instância EC2.

### 2.4 Ciclo de vida de uma requisição

1. Cliente envia requisição ao LiteLLM com uma chave virtual.
2. LiteLLM valida a chave (budget/RPM), resolve `model_name` para URL do Ray
   Serve.
3. LiteLLM encaminha para o ingress do Ray Serve LLM
   (`http://ray-head:8000/v1/...`).
4. Ray Serve roteia para o deployment do modelo correto. Se o deployment está
   em `min_replicas: 0` e ocioso, esta requisição dispara um **cold start**.
5. A réplica do deployment (instância do vLLM) admite a requisição no batch
   corrente (continuous batching) e gera tokens.
6. Tokens fluem de volta: vLLM → Ray Serve → LiteLLM → cliente.
7. LiteLLM registra custo/latência contra a chave virtual.

Para a especificação detalhada de cada camada, parâmetros de configuração,
deploy local e AWS, fine-tuning, custos e troubleshooting, consulte o documento
completo em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 3. Pré-requisitos

### 3.1 Para deploy local (todas as fases)

| Requisito | Versão mínima | Verificar com |
|-----------|--------------|---------------|
| NVIDIA driver | Compatível com a GPU | `nvidia-smi` |
| NVIDIA Container Toolkit | — | `nvidia-ctk runtime configure --runtime=docker` |
| Docker Engine | 24+ | `docker --version` |
| Docker Compose | **v2** (`docker compose`, não `docker-compose`) | `docker compose version` |
| Espaço em disco | ~16 GB (modelo 8B FP16) | `df -h ~/.cache/huggingface` |
| GPU NVIDIA | Compute capability ≥ 7.0 (V100 ou mais recente) | `nvidia-smi --query-gpu=name --format=csv,noheader` |
| Python | 3.11+ | `python --version` |

### 3.2 Para deploy AWS (Phase 3)

| Requisito | Detalhes |
|-----------|----------|
| Conta AWS | Com permissão para EC2, IAM, e service quota para GPU instances |
| `ray[default]` instalado | Para `ray up`, `ray exec`, `ray dashboard` |
| Credenciais AWS configuradas | `aws configure` ou variáveis de ambiente |
| Service quota | Solicitar aumento para a família de instância GPU desejada |

### 3.3 Para desenvolvimento e testes

| Requisito | Versão | Instalação |
|-----------|--------|------------|
| Python | 3.11+ | — |
| pytest | 8.x | `pip install pytest` |
| PyYAML | 6.x | `pip install pyyaml` (opcional para testes config) |

---

## 4. Início Rápido

> **Nota:** Este fluxo será funcional apenas após a conclusão da Phase 2
> (Build Core), que criará os artefatos de infraestrutura. Abaixo está a
> documentação antecipada do fluxo completo para referência.

### 4.1 Preparação

```bash
# 1. Clonar o repositório
git clone https://github.com/PUC-Behring-Institute-for-AI/idia-server.git
cd idia-server

# 2. Criar arquivo de secrets (NUNCA versionar este arquivo)
cat > .env << 'EOF'
HF_TOKEN=hf_seu_token_aqui
LITELLM_MASTER_KEY=sk-litellm-admin-change-me
MODEL_ID=llama-3.1-8b
MODEL_SOURCE=meta-llama/Llama-3.1-8B-Instruct
EOF

# 3. Verificar GPU disponível
nvidia-smi

# 4. Subir a stack
docker compose up -d
```

### 4.2 Verificação

```bash
# Acompanhar o carregamento do modelo (primeira vez baixa os pesos)
docker compose logs -f ray-head

# Verificar se o Ray enxerga todas as GPUs do host
docker compose exec ray-head ray status

# Teste end-to-end através da stack completa
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $(grep LITELLM_MASTER_KEY .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.1-8b","messages":[{"role":"user","content":"Explique PagedAttention em uma frase."}]}'
```

### 4.3 Consumo programático

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000",
    api_key="sk-12...",  # chave virtual gerada via /key/generate
)

response = client.chat.completions.create(
    model="llama-3.1-8b",
    messages=[{"role": "user", "content": "O que é continuous batching?"}],
    stream=True,
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

---

## 5. Estrutura do Repositório

```
idia-server/
├── AGENTS.md              ← Regras do projeto para agentes OpenCode (Phase 1 ✓)
├── .gitignore             ← Exclusões para versionamento (Phase 1 ✓)
├── pyproject.toml         ← Configuração pytest + ruff (Phase 1 ✓)
├── .env.example           ← Template de secrets (Phase 2 ✓)
├── Dockerfile.ray         ← Imagem Ray Serve LLM (Phase 2 ✓)
├── serve_config.yaml      ← Configuração Ray Serve (Phase 2 ✓)
├── docker-compose.yml     ← Orquestração local / single-EC2 (Phase 2 ✓)
├── config.yaml            ← Roteamento LiteLLM (Phase 2 ✓)
├── cluster.yaml           ← Definição do cluster AWS (Phase 3 ✓)
├── prometheus.yml         ← Configuração de monitoramento (Phase 4 ✓)
├── scripts/               ← Utilitários (entrypoints, helpers)
│   ├── render_config.py   ← Entrypoint Python — substitui placeholders env var (Phase 2 ✓)
│   └── deploy_cluster.sh  ← Deploy automatizado AWS via Ray Cluster Launcher (Phase 3 ✓)
├── grafana/               ← Dashboards e datasources (Phase 4 ✓)
├── tests/                 ← Suíte de testes (Phase 1 ✓)
│   ├── __init__.py
│   ├── conftest.py        ← Fixtures compartilhadas
│   ├── test_docs.py       ← Testes de estrutura de documentação
│   ├── test_config_schemas.py  ← Testes de schema YAML
│   ├── test_integration.py     ← Testes de integração simulada (Phase 2 ✓)
│   └── test_security.py        ← Testes de segurança (Phase 2 ✓, Phase 3 ✓)
├── docs/
│   ├── ARCHITECTURE.md    ← Documento vivo de arquitetura (Phase 1 ✓)
│   └── ...                ← Futuros: ADR.md, GLOSSARY.md conforme necessário
└── README.md              ← Este arquivo (Phase 1 ✓)
```

### Legenda dos marcadores

| Marcador | Significado |
|----------|-------------|
| ✓ | Criado e finalizado |
| ⏳ | A ser criado na fase indicada |
| (Phase N) | Fase de implementação responsável |

---

## 6. Roadmap de Implementação

O projeto está sendo desenvolvido em 5 fases incrementais, cada uma com revisão
humana antes de avançar para a próxima.

| Fase | Nome | Status | Entregáveis | Depende de |
|------|------|--------|-------------|------------|
| **1** | Fundação + AGENTS.md | ✅ **Concluída** | `AGENTS.md`, `.gitignore`, `pyproject.toml`, `tests/`, `docs/ARCHITECTURE.md` atualizado, `README.md` | — |
| **2** | Build Core | ✅ **Concluída** | `Dockerfile.ray`, `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `.env.example`, entrypoint script `render_config.py` | Fase 1 |
| **3** | Deploy AWS | ✅ **Concluída** | `cluster.yaml` (Ray Cluster Launcher), `scripts/deploy_cluster.sh`, guia EC2 + Compose expandido, testes de segurança para cluster | Fase 2 |
| **4** | Monitoramento | ✅ **Concluída** | `prometheus.yml`, integração Grafana no `docker-compose.yml` com provisioning automático de datasource, alertas documentados, testes de segurança para portas de monitoramento | Fase 2 |
| **5** | Documentação Final | ⏳ Pendente | `README.md` (revisão final), verificação de consistência código-arquitetura, handoff | Fases 1–4 |

Ao final de cada fase, a suíte de testes é executada para garantir que
nenhuma regressão foi introduzida.

---

## 7. Guia de Testes

### 7.1 Categorias

A suíte de testes usa **pytest 8.x** com quatro marcadores:

| Marcador | Categoria | O que valida | Requer infraestrutura? | Criada em |
|----------|-----------|-------------|----------------------|-----------|
| `docs` | Documentação | Estrutura de arquivos obrigatórios, seções de documentos vivos, footer de versão | Não | Fase 1 |
| `config` | Schema de configuração | Estrutura YAML de `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `cluster.yaml`, `prometheus.yml`, `.env.example` | Não (apenas PyYAML) | Fase 1 |
| `integration` | Integração | Build da imagem Docker, `docker compose up`, GPU detection, E2E inference | Sim (Docker + GPU NVIDIA) | Fase 2 |
| `security` | Segurança | Isolamento de portas (`:8000`, `:8265` inacessíveis), pin de imagens | Sim (Docker) | Fase 2 |

### 7.2 Como executar

```bash
# Instalar dependências de teste
pip install pytest pyyaml

# Testes rápidos (docs + config) — zero infraestrutura
pytest -m "docs or config" -v

# Suite completa (inclui integração e segurança)
pytest -v

# Testes de um arquivo específico
pytest tests/test_config_schemas.py -v

# Por marcador
pytest -m config -v
```

### 7.3 Política de skip

Testes que dependem de arquivos de fases futuras usam `pytest.skip()` com
mensagem explicativa — nunca falham pela ausência de algo que ainda será criado.
Isso garante que a suíte rode limpa desde a Fase 1.

### 7.4 Casos de teste atuais (Fase 1)

Em `tests/test_docs.py`:

| Teste | O que verifica |
|-------|---------------|
| `test_exists` | Cada arquivo obrigatório (`docs/ARCHITECTURE.md`, `AGENTS.md`, `README.md`) existe |
| `test_is_markdown` | Arquivos começam com `#` (cabeçalho markdown) |
| `test_contains_sections` | Documentos vivos contêm as seções de governança exigidas |
| `test_has_version_footer` | `ARCHITECTURE.md` tem footer de versão e tabela de histórico estrutural |

Em `tests/test_config_schemas.py`:

| Classe de teste | Arquivo alvo | Asserções principais |
|----------------|-------------|---------------------|
| `TestServeConfig` | `serve_config.yaml` | `proxy_location: EveryNode`, `http_options.port: 8000`, `applications` é lista não-vazia |
| `TestDockerCompose` | `docker-compose.yml` | Serviços `ray-head` e `litellm` presentes; `ipc: host` e `shm_size` em ray-head |
| `TestLiteLLMConfig` | `config.yaml` | `model_list` e `general_settings` presentes; master_key declarado |
| `TestClusterYaml` | `cluster.yaml` | `cluster_name`, `provider`, `available_node_types`; head_node é CPU-only; dashboard bound a `127.0.0.1`; imagem pinada; pre-render workflow |
| `TestPrometheusConfig` | `prometheus.yml` | `global` e `scrape_configs`; targets apontam para `ray-head:8080` e `litellm:4000` |
| `TestEnvExample` | `.env.example` | Declara `HF_TOKEN`, `LITELLM_MASTER_KEY`, `MODEL_ID`, `MODEL_SOURCE` |

---

## 8. Configuração

### 8.1 Variáveis de ambiente obrigatórias

| Variável | Exemplo | Obrigatória | Onde usar |
|----------|---------|-------------|-----------|
| `HF_TOKEN` | `hf_xxx...` | Sim | Download de pesos do HuggingFace |
| `LITELLM_MASTER_KEY` | `sk-litellm-admin-change-me` | Sim | Admin credential do LiteLLM |
| `MODEL_ID` | `llama-3.1-8b` | Sim | Alias que clientes usam no campo `model` |
| `MODEL_SOURCE` | `meta-llama/Llama-3.1-8B-Instruct` | Sim | Identificador no HuggingFace Hub |

### 8.2 Variáveis opcionais (com defaults documentados)

| Variável | Default | Descrição |
|----------|---------|-----------|
| `MAX_MODEL_LEN` | `8192` | Tamanho máximo de contexto; ajusta o dimensionamento do KV cache |
| `GPU_MEMORY_UTILIZATION` | `0.9` | Fração da memória GPU reservada para pesos + KV cache |

### 8.3 Convenções

- Secrets em `.env` (nunca versionado).
- `.env.example` é o template documentado (versionado).
- Todas as variáveis seguem `UPPER_SNAKE_CASE`.

---

## 9. Segurança

### 9.1 Portas e perímetro de rede

| Porta | Componente | Acessível externamente? | Regra |
|-------|-----------|------------------------|-------|
| `4000` | LiteLLM (API) | ✅ **Sim** — única porta pública | Única entrada para clientes |
| `8000` | Ray Serve (ingress) | ❌ **Não** — rede interna Compose | Nunca mapear em `docker-compose ports` |
| `8265` | Ray Dashboard | ❌ **Não** — acesso via `docker compose exec` ou túnel SSH | Dashboard bound a `127.0.0.1` |
| `10001` | Ray Client | ❌ **Não** — rede interna | Nunca expor |

### 9.2 Imagens de contêiner

| Imagem | Fonte | Tag | Política |
|--------|-------|-----|----------|
| Ray Serve LLM | `rayproject/ray-ml` | `2.55.0-py311-gpu` | **Pinada** — nunca `:latest` |
| LiteLLM | `docker.litellm.ai/berriai/litellm` | `v1.85.0` | **Pinada** — nunca `:latest` |
| Prometheus | `prom/prometheus` | Semver tag específica | **Pinada** |
| Grafana | `grafana/grafana` | Semver tag específica | **Pinada** |

### 9.3 Fronteiras de confiança

O sistema tem **duas fronteiras de confiança** bem definidas:

1. **Master key (admin)**: usada para gerir chaves virtuais, acessar endpoints
   administrativos. Nunca distribuída a clientes finais.
2. **Chaves virtuais (clientes)**: emitidas pelo LiteLLM via `/key/generate`,
   escopadas a budgets e rate limits. Nem a master key nem qualquer credencial
   interna é derivável de uma chave virtual.

### 9.4 ShadowRay e CVE-2026-27482

O Ray Dashboard e Jobs API foram **projetados sem autenticação**, assumindo
execução em rede já confiável. Isso é um risco documentado e ativamente
explorado (campanhas ShadowRay desde 2023, ShadowRay 2.0 em 2026).

**Mitigações obrigatórias (aplicadas na arquitetura):**

1. A porta 8265 (dashboard) nunca é mapeada no `docker-compose.yml`.
2. O dashboard é bound a `127.0.0.1` no cluster.yaml (`--dashboard-host=127.0.0.1`).
3. Ray ≥ 2.54.0 é obrigatório (fecha CVE-2026-27482).
4. O ingress do Ray Serve (8000) nunca é exposto — só o LiteLLM (4000) é
   acessível.
5. Qualquer path de rede para o Ray cluster é equivalente a root em todos os
   nós — tratá-lo como um banco de dados sem autorização de consulta.

Para a especificação completa de segurança, consulte
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#9-security--operational-hardening).

---

## 10. Targets de Deploy

A arquitetura suporta três targets de deploy com esforço crescente:

| Target | Elasticidade GPU | Esforço de setup | Documentação |
|--------|-----------------|-------------------|-------------|
| **Local (Docker Compose)** | Não (limitado às GPUs físicas da máquina) | Mínimo | [`ARCHITECTURE.md §6`](docs/ARCHITECTURE.md#6-local-deployment) |
| **AWS EC2 (Docker Compose)** | Não (redimensionamento manual da instância) | Baixo | [`ARCHITECTURE.md §7.2`](docs/ARCHITECTURE.md#72-ec2--compose) |
| **AWS Ray Cluster Launcher** | **Sim** — o cluster autoscaler provisiona/termina instâncias EC2 automaticamente | Médio | [`ARCHITECTURE.md §7.3`](docs/ARCHITECTURE.md#73-ray-cluster-launcher--automatic-physical-gpu-elasticity) |

O cliente sempre endereça a porta `:4000` — o host por trás dela (laptop,
instância EC2 única, ou cluster autoscaling) é invisível para o cliente. Esta é
a propriedade que torna o deploy local e cloud idênticos do lado do consumidor.

---

## 11. Monitoramento

O monitoramento foi implementado na **Fase 4** com Prometheus + Grafana,
integrados ao `docker-compose.yml`:

| Camada | Métricas exportadas | Endpoint Prometheus |
|--------|-------------------|-------------------|
| **vLLM** (cada réplica) | `time_to_first_token_seconds`, `e2e_request_latency_seconds`, `gpu_cache_usage_perc`, `num_preemptions_total`, `num_requests_waiting` | `ray-head:8080` (via Ray Serve) |
| **Ray Serve** | Contagem de réplicas por deployment, profundidade de fila, eventos de autoscaling | `ray-head:8080` |
| **Ray Cluster** | Contagem de nós, utilização GPU por nó, decisões do autoscaler | Dashboard (porta 8265, interna) |
| **LiteLLM** | Custo por chave/equipe, contagem de requisições, latência, eventos de fallback | `litellm:4000` |

**Infraestrutura:**

| Componente | Imagem | Porta exposta |
|-----------|--------|---------------|
| Prometheus | `prom/prometheus:v2.55.0` | Nenhuma (interna — acessado pelo Grafana) |
| Grafana | `grafana/grafana:11.4.0` | `127.0.0.1:3000` (localhost apenas) |

O datasource Prometheus é configurado automaticamente via provisioning
(`grafana/datasources/`). Dashboards oficiais do Ray Serve e vLLM podem
ser baixados e colocados em `grafana/dashboards/` para importação
automática.

**Iniciar o monitoramento:** já está incluso no `docker compose up -d`.
Acessar Grafana: `open http://localhost:3000` (credenciais: admin/admin).

### Alertas recomendados (configurar via Grafana UI)

| Alerta | Condição | Por quê |
|--------|----------|---------|
| Saturação KV cache | `gpu_cache_usage_perc > 0.95` por 5 min | Preempção/recompute iminente |
| Teto de réplicas | Deployment em `max_replicas` por >10 min | O teto do autoscaling, não a GPU, é o gargalo |
| Cluster no `max_workers` | Nós fixados no teto | Na AWS, limitado pelo `cluster.yaml` |
| Dashboard acessível | Qualquer hit externo em 8265/10001 | Deveria ser impossível — tratar como incidente |

---

## 12. Mantenedor

**Anaximandro Souza**

| | |
|---|---|
| **GitHub** | [@anaxsouza](https://github.com/anaxsouza) |
| **E-mail** | anaximandrosouza@icloud.com |
| **Instituição** | PUC-Behring Institute for AI |
| **Papel** | Arquiteto e mantenedor principal do IDIA Server |
| **Responsabilidades** | Definição de arquitetura, implementação, revisão de código, documentação, deploy e operação |

### Diretrizes para contribuições

1. Toda contribuição deve passar por revisão do mantenedor antes de merge.
2. Mudanças na arquitetura devem ser refletidas no `ARCHITECTURE.md` no mesmo
   PR/commit (regra do Contrato de Evolução de Documentos — veja
   [`ARCHITECTURE.md §16`](docs/ARCHITECTURE.md#16-document-evolution-contract)).
3. Commits seguem [Conventional Commits](https://www.conventionalcommits.org/)
   com referência `[phase-N]` quando sob plano ativo.
4. A suíte de testes deve passar limpa antes de qualquer merge.
5. Dúvidas sobre a arquitetura devem ser direcionadas ao mantenedor ou
   documentadas em issues no repositório.

---

## 13. Licença

A definir pelo PUC-Behring Institute for AI.

*Este repositório é mantido no âmbito das atividades de pesquisa do instituto.
O código e a documentação aqui contidos são propriedade intelectual do
PUC-Behring Institute for AI, salvo indicação em contrário.*

---

## 13. Licença

```
Copyright 2026 PUC-Behring Institute for AI

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

O IDIA Server é distribuído sob **Apache License 2.0** — uma licença permissiva
com proteção de patentes, compatível com projetos de IA (TensorFlow, PyTorch,
Kubernetes) e adequada para instituições de pesquisa brasileiras que precisam
incentivar tanto o uso acadêmico quanto a adoção industrial.

---

## 14. Referências

### Documentação interna

| Documento | Descrição |
|-----------|-----------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Especificação arquitetural completa (~1157 linhas) incluindo componentes, build, deploy, segurança, monitoramento, custos e troubleshooting |
| [`docs/ADR.md`](docs/ADR.md) | Architecture Decision Records — decisões arquiteturais documentadas por fase |
| [`AGENTS.md`](AGENTS.md) | Regras do projeto para agentes OpenCode — governança, evolução de documentos, testes |

### Documentação externa

| Recurso | URL |
|---------|-----|
| vLLM — Docker deployment | https://docs.vllm.ai/en/stable/deployment/docker/ |
| vLLM — Métricas Prometheus | https://docs.vllm.ai/en/stable/design/metrics/ |
| vLLM — Paralelismo e scaling | https://docs.vllm.ai/en/latest/serving/parallelism_scaling/ |
| Ray Serve LLM — Arquitetura | https://docs.ray.io/en/latest/serve/llm/architecture/overview.html |
| Ray Serve LLM — Serving guide | https://docs.ray.io/en/latest/serve/llm/index.html |
| Ray — KubeRay LLM example | https://docs.ray.io/en/latest/cluster/kubernetes/examples/rayserve-llm-example.html |
| Ray — Cluster YAML / AWS autoscaler | https://docs.ray.io/en/latest/cluster/vms/references/ray-cluster-configuration.html |
| Ray — Cluster config example | https://github.com/ray-project/ray/blob/master/python/ray/autoscaler/aws/example-full.yaml |
| Ray — Security guide | https://docs.ray.io/en/latest/ray-security/index.html |
| ShadowRay / CVE-2023-48022 | https://www.oligo.security/blog/shadowray-attack-ai-workloads-actively-exploited-in-the-wild |
| ShadowRay 2.0 (2026) | https://www.penligent.ai/hackinglabs/the-zombie-vulnerability-a-2026-autopsy-of-cve-2023-48022-and-the-shadowray-2-0-resurgence/ |
| CVE-2026-27482 | https://www.sentinelone.com/vulnerability-database/cve-2026-27482/ |
| LiteLLM — Docker quickstart | https://docs.litellm.ai/docs/proxy/docker_quick_start |
| LiteLLM — Load balancing | https://docs.litellm.ai/docs/proxy/load_balancing |
| LiteLLM — Health check routing | https://docs.litellm.ai/docs/proxy/health_check_routing |
| AWS — EC2 GPU pricing | https://aws.amazon.com/ec2/pricing/on-demand/ |
| AWS — EBS pricing | https://aws.amazon.com/ebs/pricing/ |
| Fine-tuning comparison (2026) | https://dev.to/ultraduneai/eval-003-fine-tuning-in-2026-axolotl-vs-unsloth-vs-trl-vs-llama-factory-2ohg |
| CNCF Survey 2025 | https://www.cncf.io/announcements/2026/01/20/kubernetes-established-as-the-de-facto-operating-system-for-ai-as-production-use-hits-82-in-2025-cncf-annual-cloud-native-survey/ |

---

---
*README version: 1.0 | Last updated: 2026-06-28 | Maintainer: @anaxsouza*
