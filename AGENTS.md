# AGENTS.md — IDIA Server
# Location: /Users/anaxsouza/Documents/Github/idia-server/AGENTS.md
# Inherits: ~/.config/opencode/AGENTS.md (global rules)
# Requires: global >= 2.0
# Version: 1.1
# Last updated: 2026-06-28

## Project

**Name:** IDIA Server (Inference & Deployment for Intelligent Agents)
**Description:** Self-hosted LLM inference server with automatic GPU elasticity
  and on-demand model loading, deployable identically on a local multi-GPU host
  and on AWS.
**Stack:** Python 3.11+, Docker Compose v2, Ray Serve LLM (2.55.0),
  vLLM (0.18.0), LiteLLM (1.85.0), Prometheus, Grafana
**Stack standard:** ~/.config/opencode/standards/python.md
**Testing:** pytest 8.x, PyYAML (config schema validation)
**Repository:** https://github.com/PUC-Behring-Institute-for-AI/idia-server

## Architecture (3-Tier)

| Tier | Component | Role | Port |
|------|-----------|------|------|
| Gateway | **LiteLLM** | Auth, virtual keys, budgets, rate-limits, spend tracking | `:4000` (única porta externa) |
| Orchestration | **Ray Serve LLM** | Replica autoscaling (scale-to-zero), GPU placement, multi-model routing, LoRA multiplexing | `:8000` (interna apenas) |
| Engine | **vLLM** | Inference: weights in VRAM, KV cache, token generation | in-process (sem porta) |

Fluxo: `Client → :4000 → LiteLLM → :8000 → Ray Serve → vLLM`

Referência arquitetural completa: `docs/ARCHITECTURE.md`

## Directory Layout

```
idia-server/
├── AGENTS.md              ← este arquivo — regras do projeto para agentes OpenCode
├── .gitignore
├── pyproject.toml         ← configuração pytest, ruff
├── .env.example           ← template de secrets (Phase 2)
├── Dockerfile.ray         ← imagem Ray Serve LLM (Phase 2)
├── docker-compose.yml     ← orquestração local / single-EC2 (Phase 2)
├── serve_config.yaml      ← config do Ray Serve (Phase 2)
├── config.yaml            ← roteamento LiteLLM (Phase 2)
├── cluster.yaml           ← definição do cluster AWS (Phase 3)
├── prometheus.yml         ← monitoring scrape config (Phase 4)
├── scripts/               ← utilitários (entrypoint, helpers)
├── tests/                 ← suíte de testes (pytest)
│   ├── __init__.py
│   ├── conftest.py        ← fixtures compartilhadas
│   ├── test_docs.py       ← testes de estrutura de documentação
│   └── test_config_schemas.py  ← testes de schema de configs
├── docs/
│   ├── ARCHITECTURE.md    ← documento vivo de arquitetura
│   └── ...                ← futuros: ADR.md, GLOSSARY.md conforme necessário
└── README.md              ← documentação do repositório (Phase 5)
```

## Implementation Phases

| Phase | Name | Dependencies |
|-------|------|-------------|
| **1** | Foundation + AGENTS.md | — |
| **2** | Build Core | Phase 1 |
| **3** | AWS Deployment | Phase 2 |
| **4** | Monitoring | Phase 2 |
| **5** | Final Documentation | Phases 1–4 |

---

## Document Evolution Contract

### ARCHITECTURE.md — Living Document Rules

O documento de arquitetura evolui com o código. Estas regras previnem desync:

**SYNC-REQUIRED Triggers** — qualquer alteração em:
- `Dockerfile.ray` — imagem base, dependências, entrypoint
- `serve_config.yaml` — modelos, autoscaling, engine_kwargs
- `docker-compose.yml` — serviços, portas, networks, volumes, GPU config
- `cluster.yaml` — tipos de nó, IAM, limites de autoscaling
- `config.yaml` — LiteLLM routing, model list, health checks
- `prometheus.yml` — scrape targets, alert rules
- Qualquer arquivo em `tests/` que introduza nova categoria de teste (#)
- Port mappings, topologia de rede, perímetros de segurança
- Estratégia de carregamento de modelos ou GPU placement

**Minor Update** — version bump, ajuste de parâmetro, nova env var:
- Editar apenas a seção afetada.
- Sem revisão completa do documento.
- Atualizar footer com data e seções alteradas.

**Major Update** — nova camada, novo target de deploy, mudança de pattern:
- Revisão completa do documento.
- Seções antigas marcadas com `[DEPRECATED — see section X]`.
- Requer aprovação humana antes do merge.

**Desync Prevention:**
- Se código e ARCHITECTURE.md discordam: o código é a verdade, mas o doc deve ser atualizado no mesmo PR/commit.
- Toda task de implementação que afeta a arquitetura declara: `[UPDATES ARCHITECTURE.md — section X]` no plano.
- Nunca mergear código sem a atualização correspondente do architecture doc.

**Version Footer:**
```markdown
---
*Document version: 1.1 | Last updated: 2026-06-28 | Sections changed: [list]*
```

### AGENTS.md — Update Rules

- Atualizado quando uma nova Fase é planejada (novos stacks, ferramentas, workflows).
- Atualizado quando versões de componentes mudam materialmente.
- Atualizado quando novas constraints são descobertas durante o desenvolvimento.
- Atualizado quando o diretório ou a suíte de testes muda significativamente.
- A seção **Testing Strategy** abaixo deve refletir exatamente os testes implementados em `tests/`.

---

## Security Constraints (from ARCHITECTURE 

Derivadas da arquitetura. **Não negociáveis.**

| Regra | Fonte |
|-------|-------|
| **`:4000`** é a ÚNICA porta exposta ao host | §9.1 |
| **`:8000`** (Ray ingress) é interna — nunca mapeada em `docker-compose ports` | §9.3 |
| **`:8265`** (Ray dashboard) é interna — acesso via `docker compose exec` ou túnel SSH | §9.2 |
| **`:10001`** (Ray Client) é interna | §9.2 |
| Todas as imagens **pinnadas a tags imutáveis** — nunca `:latest` | §9.1 |
| **Duas fronteiras de confiança**: master key (admin) vs. virtual keys (clientes) | §9.1 |
| TLS termina na borda (ALB/NLB no AWS, reverse proxy local), nunca dentro dos containers | §9.1 |
| Ray cluster tratado como banco sem autorização — qualquer path de rede = root | §9.2 |
| Dashboard bound a `127.0.0.1`, nunca `0.0.0.0` | §9.2 |
| Ray ≥ 2.54.0 obrigatório (fecha CVE-2026-27482) | §9.2 |

## Container Image Policy

- **`Dockerfile.ray`**: `FROM rayproject/ray-ml:2.55.0-py311-gpu`, pinado.
  `RUN pip install "ray[serve,llm]==2.55.0" vllm`
- **LiteLLM**: `docker.litellm.ai/berriai/litellm:v1.85.0`, pinado.
- **Prometheus**: `prom/prometheus`, pinado a semver tag específica.
- **Grafana**: `grafana/grafana`, pinado a semver tag específica.
- Nenhuma imagem usa `:latest`.

## Env Var Convention

- Secrets em `.env` (nunca commitado).
- `.env.example` é o template documentado (commitado).
- Todas as env vars seguem `UPPER_SNAKE_CASE`.
- Obrigatórias: `HF_TOKEN`, `LITELLM_MASTER_KEY`, `MODEL_ID`, `MODEL_SOURCE`.
- Opcionais (com defaults documentados): `MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION`.

## Model Configuration

O modelo é configurável via `.env` com duas variáveis:

| Variável | Exemplo | Obrigatória |
|----------|---------|-------------|
| `MODEL_ID` | `llama-3.1-8b` | Sim |
| `MODEL_SOURCE` | `meta-llama/Llama-3.1-8B-Instruct` | Sim |

A implementação do templating (envsubst vs Python) será decidida na Fase 2.

---

## Testing Strategy

O IDIA Server usa **pytest 8.x** como executor. A suíte cobre quatro categorias de teste,
cada uma com seu marcador e requisitos de infraestrutura.

### Categorias de Teste

| Marcador | Categoria | O que valida | Requer infraestrutura? | Fase |
|----------|-----------|-------------|----------------------|------|
| `docs` | Documentação | Estrutura de arquivos obrigatórios, seções de documentos vivos, footer de versão | Não — roda com `pip install pytest` | 1 |
| `config` | Schema de configuração | Estrutura YAML de `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `cluster.yaml`, `prometheus.yml`, `.env.example` | Não — apenas PyYAML | 1 |
| `integration` | Integração | Build da imagem Docker, `docker compose up`, GPU detection, E2E inference, escala de réplicas | Docker + GPU (NVIDIA) | 2 |
| `security` | Segurança | Isolamento de portas (`:8000`, `:8265` inacessíveis externamente), pin de imagens | Docker | 2 |

### Como executar

```bash
# Instalar dependências de teste
pip install pytest pyyaml

# Rodar testes rápidos (docs + config) — zero infraestrutura
pytest -m "docs or config" -v

# Rodar todos os testes (inclui integração e segurança)
pytest -v

# Rodar testes de um arquivo específico
pytest tests/test_config_schemas.py -v

# Rodar por marcador
pytest -m config -v
```

### O que cada teste valida

#### `test_docs.py` (— docs)

| Teste | O que verifica |
|-------|---------------|
| `test_exists` | Cada arquivo obrigatório (`docs/ARCHITECTURE.md`, `AGENTS.md`, `README.md`) existe |
| `test_is_markdown` | Arquivos começam com `#` (cabeçalho markdown) |
| `test_contains_sections` | Documentos vivos contêm as seções de governança exigidas (Document Evolution Contract, Structural Change History) |
| `test_has_version_footer` | ARCHITECTURE.md tem footer de versão e tabela de histórico estrutural |

#### `test_config_schemas.py` (— config)

Cada classe de teste valida a estrutura de um arquivo de configuração contra a especificação na arquitetura:

| Classe | Arquivo alvo | Key assertions |
|--------|-------------|---------------|
| `TestServeConfig` | `serve_config.yaml` | `proxy_location: EveryNode`, `http_options.port: 8000`, `applications` é lista não-vazia |
| `TestDockerCompose` | `docker-compose.yml` | Serviços `ray-head` e `litellm` presentes; `ipc: host` e `shm_size` em ray-head |
| `TestLiteLLMConfig` | `config.yaml` | `model_list` e `general_settings` presentes; master_key declarado |
| `TestClusterYaml` | `cluster.yaml` | `cluster_name`, `provider`, `available_node_types`; head_node é CPU-only |
| `TestPrometheusConfig` | `prometheus.yml` | `global` e `scrape_configs`; targets apontam para `ray-head:8080` e `litellm:4000` |
| `TestEnvExample` | `.env.example` | Declara `HF_TOKEN`, `LITELLM_MASTER_KEY`, `MODEL_ID`, `MODEL_SOURCE` |

### Política de Skipping

Testes que dependem de arquivos de fases futuras usam `pytest.skip()` com mensagem
explicativa — nunca falham pela ausência de algo que ainda será criado.
Isso permite que a suíte rode limpa desde a Fase 1.

### Adicionando Novos Testes

1. Criar arquivo `tests/test_<area>.py`.
2. Usar o marcador apropriado: `@pytest.mark.docs`, `@pytest.mark.config`,
   `@pytest.mark.integration`, `@pytest.mark.security`.
3. Usar as fixtures compartilhadas de `conftest.py` (`repo_root`, `docs_dir`, `config_files`).
4. Se o teste depende de um arquivo de fase futura, usar `pytest.skip()` se o arquivo não existir.
5. Registrar o novo marcador em `pyproject.toml` se for nova categoria.
6. Atualizar esta seção no AGENTS.md.

---

## Git Conventions

Segue `~/.config/opencode/AGENTS.md §8` (Conventional Commits).
Commits incluem referência `[phase-N]` quando sob plano ativo.

## Stack-Specific Rules

- **Docker Compose v2** obrigatório (`docker compose`, não `docker-compose`).
- **Nunca** expor portas internas (8000, 8265, 10001) no `docker-compose.yml`.
- Toda imagem pinada por tag semver — `:latest` proibido.
- Secrets via `.env` + variáveis de ambiente, nunca hardcoded.
- Configs YAML seguem schemas validados por `tests/test_config_schemas.py`.
