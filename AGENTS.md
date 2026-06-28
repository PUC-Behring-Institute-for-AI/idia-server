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
├── .env.example           ← template de secrets (Phase 2 ✓)
├── Dockerfile.ray         ← imagem Ray Serve LLM (Phase 2 ✓)
├── docker-compose.yml     ← orquestração local / single-EC2 (Phase 2 ✓)
├── serve_config.yaml      ← config do Ray Serve (Phase 2 ✓)
├── config.yaml            ← roteamento LiteLLM (Phase 2 ✓)
├── cluster.yaml           ← definição do cluster AWS (Phase 3 ✓)
├── prometheus.yml         ← monitoring scrape config (Phase 4 ✓)
├── scripts/               ← utilitários (entrypoint, helpers)
│   ├── render_config.py   ← entrypoint Python — substitui placeholders env var (Phase 2 ✓)
│   └── deploy_cluster.sh  ← deploy automatizado AWS via Ray Cluster Launcher (Phase 3 ✓)
├── grafana/               ← dashboards e datasources (Phase 4 ✓)
│   ├── datasources/       ← provisioning do datasource Prometheus
│   └── dashboards/        ← JSONs importados dos dashboards oficiais Ray/vLLM
├── tests/                 ← suíte de testes (pytest)
│   ├── __init__.py
│   ├── conftest.py        ← fixtures compartilhadas
│   ├── test_docs.py       ← testes de estrutura de documentação
│   ├── test_config_schemas.py  ← testes de schema de configs
│   ├── test_integration.py ← testes de integração simulados (render_config, Compose) (Phase 2 ✓)
│   └── test_security.py   ← testes de segurança (portas, pinning, boundaries) (Phase 2 ✓)
├── docs/
│   ├── ARCHITECTURE.md    ← documento vivo de arquitetura
│   └── ...                ← futuros: ADR.md, GLOSSARY.md conforme necessário
└── README.md              ← documentação do repositório (Phase 1 ✓)
```

## Implementation Phases

| Phase | Name | Dependencies |
|-------|------|-------------|
| **1** | Foundation + AGENTS.md + README.md | — |
| **2** | Build Core | Phase 1 | ✅ |
| **3** | AWS Deployment | Phase 2 | ✅ |
| **4** | Monitoring | Phase 2 | ✅ |
| **5** | Final Documentation (revision + handoff) | Phases 1–4 |

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

## Anti-Drift Rule (AXIOM — NON-OVERRIDABLE)

Toda tarefa de implementação que cria, modifica ou remove um artefato de
infraestrutura (Dockerfile, config YAML, Compose, entrypoint, script de
deploy, pipeline de CI/CD, teste de integração ou segurança) DEVE:

1. Declarar no plano: `[UPDATES ARCHITECTURE.md — section X]`
2. Atualizar a seção correspondente no architecture doc no mesmo commit
3. Atualizar o footer de versão do architecture doc
4. Adicionar entrada na Structural Change History

**Violação:** se um artefato for mergeado sem a atualização correspondente da
arquitetura, o commit é considerado incompleto. A correção deve ser feita
antes de qualquer outro trabalho.

Esta regra está em vigor desde a Fase 2 e se aplica a todas as fases
subsequentes.

---

## Governance & Maintainability Axioms (AXIOM — NON-OVERRIDABLE)

Estas regras existem porque a rastreabilidade de decisões e a facilidade de
manutenção são prioridades do projeto. Um novo membro da equipe ou um
agente OpenCode deve conseguir entender qualquer parte do sistema usando
apenas a documentação e os commits — sem entrevistar o autor original.

### 0. Decision Closure Rule — Planos só existem com decisões fechadas

Nenhum plano de implementação é considerado completo enquanto houver
decisões de projeto pendentes. O autor do plano deve:

1. Identificar todas as questões em aberto durante a análise do problema.
2. Documentar cada questão explicitamente no plano.
3. **Fechar cada decisão** antes de concluir o plano, usando:
   - Melhores práticas da área quando o usuário não tiver preferência.
   - A recomendação fundamentada do autor quando o usuário delegar.
   - Investigação adicional (skills, pesquisa, código existente) quando
     necessário — nunca palpites não verificados.
4. Registrar a decisão e sua justificativa no plano ou na documentação.

**Violação:** um plano apresentado com questões em aberto não aprovadas
não autoriza implementação. A implementação deve parar até que todas as
decisões estejam fechadas.

**Exemplo:** se o plano levanta "qual instância EC2 usar?" sem responder,
o plano está incompleto. O autor deve pesquisar, recomendar e documentar
a escolha (ex.: g5.xlarge por 1× A10G 24GB — adequado para modelos 7-8B).

### 1. Architecture Feedback Loop — Toda descoberta de implementação realimenta a arquitetura

A implementação inevitavelmente revela detalhes não antecipados na
arquitetura original. Quando isso acontece:

1. A descoberta é registrada.
2. A arquitetura (`ARCHITECTURE.md`) é atualizada para refletir o
   entendimento corrigido.
3. A implementação prossegue sobre a arquitetura atualizada — nunca
   sobre a versão desatualizada.

**Ciclo:** `Arquitetura → Implementação → Descoberta → Atualização da
Arquitetura → Continuação da Implementação`

**Isso se aplica a:**
- Parâmetros que se revelam diferentes do esperado.
- Workflows que exigem passos adicionais não documentados.
- Dependências ou versões que se provam incompatíveis.
- Qualquer diferença entre o comportamento real e o especificado.

**Registro:** cada iteração do ciclo deve ser rastreável via commit ou
entrada na Structural Change History do `ARCHITECTURE.md`.

### 2. Traceability Axiom — Todo commit deve ser compreensível por um novo membro 6 meses depois

Um commit não é apenas "o que mudou" — é **por que mudou**, qual decisão
foi tomada, e qual alternativa foi descartada.

| Critério | Obrigatório? | Exemplo (bom) | Exemplo (ruim) |
|----------|-------------|---------------|----------------|
| **Por que** esta mudança existe? | ✅ | "cluster.yaml: pre-render workflow porque serve_config.yaml tem placeholders ${VAR} desde a Fase 2 — Cluster Launcher não suporta env vars nativamente" | "cluster.yaml: fix worker config" |
| **Qual decisão** foi tomada? | ✅ | "cluster.yaml: g5.xlarge (1× A10G 24GB) — melhor custo-benefício para modelos 7-8B; ver análise em §14.2" | "cluster.yaml: add gpu worker" |
| **Qual alternativa** foi descartada? | ✅ | "Opção A (env vars via head_setup_commands) descartada porque hardcoda secrets no cluster.yaml" | "cluster.yaml: fix worker type" |
| **O que** mudou (diff)? | ✅ (implícito no git) | — | — |

**Na prática:** a mensagem do commit deve conter, em linguagem natural,
as respostas para "por que", "qual decisão" e "qual alternativa".

**Documentação derivada:** quando uma decisão de implementação modifica
a arquitetura, o `ARCHITECTURE.md` deve ser atualizado no mesmo commit,
e a entrada na Structural Change History deve referenciar o commit.

### 3. Maintainability Over Novelty — Preferir o conhecido sobre o novo

Quando múltiplas abordagens técnicas resolvem o mesmo problema:

1. Preferir a abordagem mais documentada, mais testada e mais conhecida
   pela equipe.
2. Abordagens experimentais ou de vanguarda exigem justificativa
   explícita de por que a abordagem estabelecida não atende.
3. "Porque é mais novo/mais rápido/melhor" não é justificativa suficiente
   sem evidência mensurável para o caso de uso específico.
4. Se uma abordagem nova é escolhida, documentar explicitamente o que
   se espera ganhar e qual o plano de fallback.

**Exceção:** quando o problema ativo não pode ser resolvido por
abordagens estabelecidas — nesse caso, documentar por que.

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

A implementação do templating usa o entrypoint Python `scripts/render_config.py`, que substitui placeholders `${VAR}` por variáveis de ambiente antes de delegar ao Ray Serve.

---

## Testing Strategy

O IDIA Server usa **pytest 8.x** como executor. A suíte cobre quatro categorias de teste,
cada uma com seu marcador e requisitos de infraestrutura.

### Categorias de Teste

| Marcador | Categoria | O que valida | Requer infraestrutura? | Fase |
|----------|-----------|-------------|----------------------|------|
| `docs` | Documentação | Estrutura de arquivos obrigatórios, seções de documentos vivos, footer de versão | Não — roda com `pip install pytest` | 1 |
| `config` | Schema de configuração | Estrutura YAML de `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `cluster.yaml`, `prometheus.yml`, `.env.example`; Grafana datasource provisioning | Não — apenas PyYAML | 1 |
| `integration` | Integração | `render_config.py`: substituição de env vars, validação YAML, dry-run, caminhos de erro; consistência do Compose (build source, pinning, env vars) | Componente unitário: apenas pytest; full suite: Docker + GPU | 2 |
| `security` | Segurança | Isolamento de portas (`:8000`, `:8265`, `:10001` inacessíveis externamente; apenas `:4000` externa; `:9090` não publicada; `:3000` bound a localhost), pin de imagens (`no :latest`), fronteiras de confiança (master_key declarado), binding do dashboard | Verificação de YAML: apenas pytest; verificação de rede: Docker | 2 |

### Como executar

```bash
# Instalar dependências de teste
pip install pytest pyyaml

# Rodar testes rápidos (docs + config) — zero infraestrutura
pytest -m "docs or config" -v

# Rodar todos os testes (inclui integração e segurança simulados)
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
| `TestClusterYaml` | `cluster.yaml` | `cluster_name`, `provider`, `available_node_types`; head_node é CPU-only; dashboard bound a 127.0.0.1; imagem Docker pinada; gpu_worker min_workers=0; file_mounts mapeia rendered_config |
| `TestPrometheusConfig` | `prometheus.yml` | `global` e `scrape_configs`; targets apontam para `ray-head:8080` e `litellm:4000`; scrape_interval=15s; sem rule_files (alertas no Grafana) |
| `TestGrafanaDatasourceConfig` | `grafana/datasources/datasource.yml` | datasource Prometheus configurado como default; url=http://prometheus:9090; access=proxy |
| `TestEnvExample` | `.env.example` | Declara `HF_TOKEN`, `LITELLM_MASTER_KEY`, `MODEL_ID`, `MODEL_SOURCE` |

#### `test_integration.py` (— integration)

| Classe/Teste | O que verifica |
|-------------|---------------|
| `TestRenderConfig.test_render_with_minimal_env` | `render()` substitui placeholders com env vars |
| `TestRenderConfig.test_render_injects_defaults` | Optional vars (GPU_MEMORY_UTILIZATION) usam default quando ausentes |
| `TestRenderConfig.test_render_validates_full_template` | Estrutura do YAML renderizado corresponde a `§5.3` (proxy_location, port, min/max_replicas) |
| `TestRenderConfig.test_dry_run_flag` | `--dry-run` produz YAML válido sem executar serve |
| `TestRenderConfigErrors.test_missing_required_var_fails` | Exit 1 com mensagem se MODEL_ID ausente |
| `TestRenderConfigErrors.test_bad_yaml_template_fails` | Exit 1 se template inválido após substituição |
| `TestComposeConsistency.test_ray_head_builds_locally` | ray-head usa build local (Dockerfile.ray) |
| `TestComposeConsistency.test_litellm_uses_pinned_image` | litellm usa tag semver, não :latest |
| `TestComposeConsistency.test_ray_head_passes_vars_to_entrypoint` | ray-head passa MODEL_ID, MODEL_SOURCE, MAX_MODEL_LEN, GPU_MEMORY_UTILIZATION |

#### `test_security.py` (— security)

| Classe/Teste | O que verifica |
|-------------|---------------|
| `TestPortIsolation.test_only_4000_published` | Apenas porta 4000 aparece em `ports:` no Compose |
| `TestPortIsolation.test_ray_ingress_not_published` | Porta 8000 NÃO está em `ports:` |
| `TestPortIsolation.test_dashboard_not_published` | Porta 8265 NÃO está em `ports:` |
| `TestPortIsolation.test_ray_client_not_published` | Porta 10001 NÃO está em `ports:` |
| `TestImagePinning.test_dockerfile_no_latest` | Dockerfile.ray não usa `:latest` |
| `TestImagePinning.test_compose_no_latest` | Nenhum serviço no Compose usa `:latest` |
| `TestTrustBoundaries.test_litellm_config_has_master_key` | config.yaml declara `general_settings.master_key` |
| `TestDashboardBinding.test_dashboard_host_set_to_localhost` | serve_config.yaml http_options.host=0.0.0.0 (proxy interno)
| `TestClusterSecurity.test_cluster_dashboard_bound_localhost` | cluster.yaml contém `--dashboard-host=127.0.0.1`
| `TestClusterSecurity.test_cluster_image_pinned` | cluster.yaml não usa imagem `:latest`
| `TestClusterSecurity.test_cluster_head_node_cpu_only` | cluster.yaml head node é CPU-only (§7.3)
| `TestMonitoringPortIsolation.test_prometheus_port_not_published` | Porta 9090 (Prometheus) não está em `ports:` no Compose
| `TestMonitoringPortIsolation.test_grafana_port_bound_localhost` | Porta 3000 (Grafana) bound a 127.0.0.1

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
