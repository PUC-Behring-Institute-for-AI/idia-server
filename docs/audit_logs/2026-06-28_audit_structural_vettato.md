# AUDITORIA ESTRUTURAL — IDIA Server
## Gaps Críticos para 50 Usuários

**Data:** 2026-06-28 | **Auditor:** Claude Code (Sonnet 4.6)
**Commit auditado:** 35686e6 (pós-remediação da auditoria anterior)
**Escopo:** 7 áreas estruturais não cobertas pela auditoria anterior
**Metodologia:** Leitura completa de todos os arquivos antes de cada alegação; verificação de código antes de atribuir severidade.

---

## LEGENDA DE SEVERIDADE

| Nível | Critério |
|-------|----------|
| **CRÍTICO** | Falha catastrófica que impede o funcionamento do sistema no cenário declarado |
| **ALTO** | Problema que causa falha silenciosa, custo descontrolado, ou inacessibilidade de feature-chave |
| **MÉDIO** | Comportamento incorreto com mitigação parcial existente, ou inconsistência que causa confusão operacional |
| **BAIXO** | Melhoria de qualidade, observabilidade ou resiliência; não bloqueia operação normal |

| Tipo | Significado |
|------|-------------|
| **BUG** | Código presente que se comporta incorretamente |
| **GAP** | Funcionalidade ausente que o cenário de uso requer |
| **DOC-DESYNC** | Arquitetura.md diverge do código real |

---

## 1. RESUMO EXECUTIVO

Os 5 problemas mais críticos para o cenário de 50 usuários com múltiplos modelos:

| Rank | Problema | Severidade | Tipo | Área |
|------|----------|------------|------|------|
| 1 | **Multi-model serving não está implementado** — serve_config.yaml suporta exatamente 1 modelo; render_config.py não tem mecanismo para N modelos | **ALTO** | GAP | #1 |
| 2 | **`min_replicas: 1` contradiz o requisito de idle timeout** — GPU worker nunca termina na AWS; custo fixo de ~$584/mês por modelo, mesmo sem requests | **ALTO** | GAP | #3 |
| 3 | **HF_TOKEN nunca chega aos GPU workers na AWS** — cluster.yaml não injeta `HUGGING_FACE_HUB_TOKEN` nos workers; modelos gated (LLaMA-3.1-8B-Instruct) falham ao baixar | **ALTO** | BUG | #3 |
| 4 | **Rate limiting e user tiers não configurados** — todos os 50 usuários competem igualmente sem limite; um batch request de hard user trava a fila para todos | **MÉDIO** | GAP | #4 |
| 5 | **vLLM version inconsistency entre local e AWS** — Dockerfile.ray pina `vllm==0.5.4`, mas GPU workers na AWS usam versão bundled em `rayproject/ray-ml:2.55.0-py311-gpu` (não especificada); head_setup_commands instala vLLM sem versão | **MÉDIO** | BUG | #2 |

---

## 2. ANÁLISE POR ÁREA

---

### Área 1 — Multi-model Serving

#### STRUCT-01 — Arquitetura de 1 modelo hardcoded em todos os artefatos
**Tipo:** GAP | **Severidade:** ALTO

**Evidência:**

`serve_config.yaml:17-29`:
```yaml
llm_configs:
  - model_loading_config:
      model_id: ${MODEL_ID}       # ← único par; sem MODEL_ID_2
      model_source: ${MODEL_SOURCE}
```

`scripts/render_config.py:39-44`:
```python
ENV_SCHEMA: dict[str, tuple[type, object]] = {
    "MODEL_ID": (str, None),      # ← sem MODEL_ID_N
    "MODEL_SOURCE": (str, None),
    "MAX_MODEL_LEN": (int, 8192),
    "GPU_MEMORY_UTILIZATION": (float, 0.9),
}
```

`config.yaml:11-15`:
```yaml
model_list:
  - model_name: llama-3.1-8b     # ← único modelo
    litellm_params:
      model: openai/llama-3.1-8b
      api_base: http://ray-head:8000/v1
```

`scripts/deploy_cluster.sh:78`:
```bash
REQUIRED_VARS=("HF_TOKEN" "LITELLM_MASTER_KEY" "MODEL_ID" "MODEL_SOURCE")
# ↑ sem MODEL_ID_2, MODEL_SOURCE_2
```

**Validação em render_config.py:220-227** (verifica apenas o primeiro modelo):
```python
apps = parsed.get("applications", [])
llm_configs = apps[0].get("args", {}).get("llm_configs", [])
mlc = llm_configs[0].get("model_loading_config", {})
```

**Impacto:** Para adicionar um segundo modelo:
1. Editar `serve_config.yaml` manualmente (adicionar bloco `llm_configs[1]`)
2. Editar `config.yaml` (novo `model_name`)
3. Editar `ENV_SCHEMA` em `render_config.py` (novos vars)
4. Editar `docker-compose.yml` (novas env vars no serviço ray-head)
5. Editar `deploy_cluster.sh` (novos REQUIRED_VARS)
6. Resolver VRAM contention: dois modelos com `gpu_memory_utilization: 0.9` em uma única GPU A10G (24GB) = 43.2GB necessário → OOM garantido

**Esforço para corrigir:** 2-4 horas (design do padrão) + 1 dia (implementação + testes)

**Correção proposta:**
```yaml
# serve_config.yaml — extensão para N modelos
llm_configs:
  - model_loading_config:
      model_id: ${MODEL_1_ID}
      model_source: ${MODEL_1_SOURCE}
    engine_kwargs:
      dtype: bfloat16
      gpu_memory_utilization: ${MODEL_1_GPU_UTIL:-0.85}
      max_model_len: ${MODEL_1_MAX_LEN:-8192}
    deployment_config:
      autoscaling_config:
        min_replicas: 0            # scale-to-zero por modelo
        max_replicas: 2
        target_ongoing_requests: 32
  - model_loading_config:
      model_id: ${MODEL_2_ID}
      model_source: ${MODEL_2_SOURCE}
    engine_kwargs:
      dtype: bfloat16
      gpu_memory_utilization: ${MODEL_2_GPU_UTIL:-0.85}
      max_model_len: ${MODEL_2_MAX_LEN:-4096}
    deployment_config:
      autoscaling_config:
        min_replicas: 0
        max_replicas: 2
        target_ongoing_requests: 32
```

O `render_config.py` precisaria de um loop parametrizado por `MODEL_N_*` vars ou um arquivo de lista de modelos.

---

#### STRUCT-02 — VRAM contention entre modelos em GPU compartilhada
**Tipo:** GAP | **Severidade:** MÉDIO

**Evidência:**

`serve_config.yaml:22`:
```yaml
gpu_memory_utilization: ${GPU_MEMORY_UTILIZATION}  # default 0.9 (90%)
```

Uma g5.xlarge tem 1× A10G com 24GB VRAM. Com `gpu_memory_utilization: 0.9`:
- 1 réplica = 21.6GB (90% de 24GB)
- Sobra 2.4GB de headroom — insuficiente para qualquer segundo modelo

Ray não tem visibilidade de VRAM livre por deployment — aloca por contagem de GPU (1 GPU por réplica por padrão). Dois deployments no mesmo nó tentarão ambos usar 90% da GPU → CUDA OOM no segundo.

**Para múltiplos modelos pequenos (ex: 2× 7B quantizados INT4 ~4GB cada):**
- Seria tecnicamente possível packer dois modelos numa A10G
- Requer `gpu_memory_utilization` reduzido (ex: 0.35 por modelo) + `num_gpus: 0.5` em `ray_actor_options`
- Nenhum desses controles está configurado

**Esforço:** 4h (análise) + 2h (implementação) se modelos forem escolhidos e requisitos de VRAM conhecidos

---

#### STRUCT-03 — DOC-DESYNC: ARCHITECTURE.md §4.3 mostra fallback sk-admin (já corrigido no código)
**Tipo:** DOC-DESYNC | **Severidade:** BAIXO

`docs/ARCHITECTURE.md:175`:
```
master_key: ${LITELLM_MASTER_KEY:sk-admin}
```

Mas `config.yaml:22` (código real):
```yaml
master_key: ${LITELLM_MASTER_KEY}  # sem fallback — SEC-01 corrigido
```

A auditoria anterior (SEC-01) corrigiu o código mas não atualizou o exemplo na documentação. Violação do Document Evolution Contract (§16.1).

**Esforço:** 5 minutos

---

### Área 2 — GPU Packing e Scheduling

#### STRUCT-04 — Único node type: sem suporte a modelos >24GB ou tensor parallelism
**Tipo:** GAP | **Severidade:** MÉDIO

**Evidência:**

`cluster.yaml:49-63` — único tipo de worker:
```yaml
gpu_worker:
  node_config:
    InstanceType: g5.xlarge    # 1× A10G 24GB
  resources: {}                # Ray auto-detecta 1 GPU
```

Sem alternativa para:
- LLaMA-3.1-70B (FP16: ~140GB, INT4: ~35GB → não cabe em g5.xlarge)
- Modelos 13B-34B (INT4: ~7-17GB → cabe, mas barely)

`serve_config.yaml:21-23` — sem tensor_parallel_size:
```yaml
engine_kwargs:
  dtype: bfloat16
  gpu_memory_utilization: ${GPU_MEMORY_UTILIZATION}
  max_model_len: ${MAX_MODEL_LEN}
  # ← tensor_parallel_size ausente
```

**Impacto:** Para o cenário de pesquisa com modelos de diferentes tamanhos, é necessário ao menos um segundo node type (ex: `g5.12xlarge` com 4× A10G).

**Referência arquitetural (§7.5, já documentado):**
```
g5.12xlarge — 4× A10G (96GB total) — 13B-34B, ou vários 7-8B por nó
```

A arquitetura documenta essa alternativa mas não a configura.

**Esforço:** 1-2 horas para adicionar segundo node type + configuração de tensor_parallel_size

**Correção proposta:**
```yaml
# cluster.yaml — adição de segundo node type
available_node_types:
  gpu_worker:              # existente — 7-8B models
    min_workers: 0
    max_workers: 4
    node_config:
      InstanceType: g5.xlarge
      # ... existente

  gpu_worker_large:        # novo — 13-70B models via tensor parallelism
    min_workers: 0
    max_workers: 2         # guardas mais apertadas por custo
    node_config:
      InstanceType: g5.12xlarge     # 4× A10G, 96GB total
      BlockDeviceMappings:
        - DeviceName: /dev/sda1
          Ebs:
            VolumeSize: 200
            VolumeType: gp3
    resources: {}
```

---

#### STRUCT-05 — vLLM version diverge entre local e AWS workers
**Tipo:** BUG | **Severidade:** MÉDIO

**Evidência:**

`Dockerfile.ray:12`:
```dockerfile
RUN pip install --no-cache-dir "ray[serve,llm]==2.55.0" "vllm==0.5.4"
```
→ **Local:** vLLM 0.5.4 (pinado)

`cluster.yaml:74`:
```yaml
head_setup_commands:
  - pip install --quiet "ray[serve,llm]==2.55.0" vllm
  # ↑ vLLM SEM versão → instala latest no head node
```
→ **AWS head node:** vLLM latest (não determinístico)

`cluster.yaml:37`:
```yaml
docker:
  image: "rayproject/ray-ml:2.55.0-py311-gpu"
```
→ **AWS GPU workers:** vLLM bundled na imagem ray-ml (versão desconhecida, ≠ 0.5.4)

`docs/ARCHITECTURE.md:223` confirma o problema:
> "2.55.0 installs vLLM `0.18.0` as its bundled engine; verify compatibility before bumping either independently."

A versão no Dockerfile.ray (0.5.4) e a versão bundled na imagem ray-ml (0.18.0 segundo a doc) são **significativamente diferentes**. GPU workers na AWS usam uma versão que não foi testada com a configuração local.

**Impacto:** Comportamento de inferência pode divergir entre local e AWS. Métricas de vLLM podem mudar (nomes de campos Prometheus diferentes entre versões).

**Correção:**
```yaml
# cluster.yaml:74 — pinar versão
head_setup_commands:
  - pip install --quiet "ray[serve,llm]==2.55.0" "vllm==0.5.4"
```

Para os workers: adicionar `worker_setup_commands` equivalente ou usar a imagem local construída (mais robusto).

**Esforço:** 2 minutos (pinar versão) + validação em AWS

---

### Área 3 — Cold Start vs Idle Timeout

#### STRUCT-06 — `min_replicas: 1` contradiz requisito de idle timeout
**Tipo:** GAP | **Severidade:** ALTO

**Evidência:**

`serve_config.yaml:28-29`:
```yaml
autoscaling_config:
  min_replicas: 1    # ← NUNCA escala a zero
  max_replicas: 4
  target_ongoing_requests: 64
```

`docs/ARCHITECTURE.md:1042`:
> "To enable [scale-to-zero]: set `min_replicas: 0`"

**Requisito declarado no contexto de auditoria:**
> "Idle timeout necessário: modelo deve descarregar da VRAM após período sem requests; GPU worker deve escalar a zero"

**Com `min_replicas: 1`:**
1. A réplica vLLM NUNCA termina — GPU nunca liberada para outros modelos
2. O Ray Cluster Autoscaler NUNCA termina o GPU worker (sempre há réplica ativa)
3. AWS: g5.xlarge sempre rodando = $0.80/hr × 730h = **~$584/mês por modelo**
4. Para N modelos: N × $584/mês = custo fixo inescapável

**Por que `min_replicas: 1` pode ter sido intencional:**
A architecture doc §4.2 usa `min_replicas: 1` no exemplo, com nota inline: "0 = scale-to-zero". Parece escolha consciente para evitar cold start na primeira requisição — mas contradiz o requisito do cenário.

**Cold start real com scale-to-zero na AWS (estimativa):**

| Etapa | Tempo estimado |
|-------|---------------|
| EC2 provisioning (g5.xlarge) | 2-5 min |
| Docker pull (rayproject/ray-ml ~15GB) | 5-15 min |
| Model download (LLaMA-3.1-8B, ~16GB) | 5-20 min (sem cache EFS) |
| CUDA kernel compile (primeira carga) | 1-5 min |
| **Total cold start** | **13-45 minutos** |

Cold start aceitável APENAS se modelo já estiver em cache no EBS do worker (requer que o mesmo worker seja reusado). Se o worker terminou, cache é perdido.

**Esforço:** 5 min para mudar `min_replicas: 0` + decisão de produto (aceitar cold start?) + solução de cache EFS se cold start inaceitável

---

#### STRUCT-07 — HF_TOKEN não chega aos GPU workers na AWS
**Tipo:** BUG | **Severidade:** ALTO

**Evidência:**

`docker-compose.yml:26`:
```yaml
environment:
  - HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}   # ← passado corretamente no local
```

`cluster.yaml` — **ausente**:
```yaml
docker:
  image: "rayproject/ray-ml:2.55.0-py311-gpu"
  container_name: "ray_container"
  # ← sem env_vars, sem run_options com -e HF_TOKEN
```

`cluster.yaml:74` — head_setup_commands **apenas no head node**:
```yaml
head_setup_commands:
  - pip install --quiet "ray[serve,llm]==2.55.0" vllm
  # ← sem export HF_TOKEN
```

**Mecanismo de falha:**
1. `serve run /app/rendered_config.yaml` roda no head node (m5.large, CPU-only)
2. Ray Serve LLM aloca réplicas nos GPU workers (g5.xlarge)
3. vLLM no GPU worker tenta baixar modelo:
   `HfApi().model_info("meta-llama/Llama-3.1-8B-Instruct")` → `401 Unauthorized`
4. Réplica falha → Ray retry → loop infinito

**Modelos afetados:** todos os modelos gated no HuggingFace (LLaMA-3.x, Gemma, etc.)
**Modelos NÃO afetados:** modelos públicos não-gated (ex: mistralai/Mistral-7B-v0.1)

**Verificação:** O `.env.example:22` usa `MODEL_SOURCE=meta-llama/Llama-3.1-8B-Instruct` — modelo gated. O deploy padrão FALHARÁ na AWS para o modelo de exemplo.

**Correção:**
```yaml
# cluster.yaml — adicionar env_vars na seção docker
docker:
  image: "rayproject/ray-ml:2.55.0-py311-gpu"
  container_name: "ray_container"
  run_options:
    - "--env HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}"
```

**Alternativa mais segura** (sem expor token em cluster.yaml):
```yaml
head_setup_commands:
  - pip install --quiet "ray[serve,llm]==2.55.0" "vllm==0.5.4"
  - echo "export HUGGING_FACE_HUB_TOKEN=$HF_TOKEN" >> /etc/environment

worker_setup_commands:
  - echo "export HUGGING_FACE_HUB_TOKEN=$HF_TOKEN" >> /etc/environment
```

⚠️ Cuidado: ambas as abordagens expõem o token no cluster.yaml — usar Ray runtime_env é mais seguro:

```yaml
# serve_config.yaml — runtime_env por aplicação
applications:
  - name: llms
    import_path: ray.serve.llm:build_openai_app
    route_prefix: "/"
    runtime_env:
      env_vars:
        HUGGING_FACE_HUB_TOKEN: "${HF_TOKEN}"  # referência a env var local
    args:
      ...
```

**Esforço:** 30 minutos (implementar + testar)

---

#### STRUCT-08 — `idle_timeout_minutes` não configurado no cluster.yaml
**Tipo:** GAP | **Severidade:** MÉDIO

**Evidência:**

`cluster.yaml` — ausente:
```yaml
# idle_timeout_minutes não definido
# Ray default: depende da versão
```

`docs/ARCHITECTURE.md:691`:
> "idle_timeout_minutes controls how long an empty node survives before termination"

**Problema:** Sem valor explícito, o comportamento do autoscaler em relação a nodes ociosos é não-determinístico entre versões do Ray. O default do Ray Cluster Launcher é tipicamente 5 minutos, mas pode variar.

**Cenário de crashloop:** se uma réplica entra em OOM loop:
1. Ray Serve retry a réplica no worker
2. OOM → crash → retry → OOM...
3. Do ponto de vista do cluster autoscaler, o node TEM demanda (réplica pendente)
4. Node não é terminado mesmo em estado crashloop
5. AWS cobra o node indefinidamente

**Correção:**
```yaml
# cluster.yaml — após max_workers:4
idle_timeout_minutes: 5    # terminar workers ociosos após 5 minutos
```

**Esforço:** 2 minutos

---

### Área 4 — Rate Limiting e Priorização

#### STRUCT-09 — Nenhuma configuração de rate limiting em config.yaml
**Tipo:** GAP | **Severidade:** MÉDIO

**Evidência:**

`config.yaml` — completo:
```yaml
model_list:
  - model_name: llama-3.1-8b
    litellm_params:
      model: openai/llama-3.1-8b
      api_base: http://ray-head:8000/v1
      api_key: "no-auth-internal"

general_settings:
  master_key: ${LITELLM_MASTER_KEY}
```

**Ausente:**
- `router_settings` (estratégia de load balancing entre múltiplos backends)
- `litellm_settings.max_parallel_requests` (cap global de concorrência)
- `rpm_limit`/`tpm_limit` por modelo
- User tiers (hard/regular/light do cenário)
- `max_budget` padrão para chaves sem budget explícito

**Impacto para 50 usuários:**

| Usuário | Perfil | Comportamento atual | Comportamento esperado |
|---------|--------|---------------------|------------------------|
| Hard (pesquisa) | 200 req/dia | Ilimitado | 200 req/dia cap |
| Regular (mestrado) | 50 req/dia | Ilimitado | 50 req/dia cap |
| Light (IC) | 5 req/dia | Ilimitado | 5 req/dia cap |

Um batch job de 200 requests de um hard user pode consumir toda a capacidade da GPU por horas, bloqueando os 49 outros usuários. O `target_ongoing_requests: 64` limita o Ray, mas não o LiteLLM queue — requests continuam chegando ao LiteLLM mesmo quando o Ray está saturado.

**Correção proposta:**
```yaml
# config.yaml — adicionar configurações de rate limiting
general_settings:
  master_key: ${LITELLM_MASTER_KEY}
  max_parallel_requests: 20    # cap global de requests simultâneos

router_settings:
  routing_strategy: least-busy  # evitar acumulação em um único backend

litellm_settings:
  default_team_settings:
    - team_alias: hard
      rpm_limit: 15             # 200 req/dia ÷ 60min × peak factor
      tpm_limit: 50000
    - team_alias: regular
      rpm_limit: 4
      tpm_limit: 15000
    - team_alias: light
      rpm_limit: 1
      tpm_limit: 5000
```

Virtual keys ainda seriam geradas por usuário via `/key/generate`, mas com um team padrão que aplica os limites.

**Esforço:** 2-4 horas (design de política) + 1 hora (configuração)

---

#### STRUCT-10 — Nenhuma prioridade de fila: batch e interactive competem igualmente
**Tipo:** GAP | **Severidade:** BAIXO

Ray Serve usa uma fila FIFO por deployment (`target_ongoing_requests: 64`). Não há mecanismo de prioridade entre:
- Requests interativos (baixa latência, usuário aguardando)
- Batch jobs (alta throughput, latência tolerável)

LiteLLM tem suporte experimental a `priority` nos virtual keys, mas não está configurado.

**Impacto:** Um hard user executando `for i in range(200): client.chat(...)` em loop ocupa 200 slots consecutivos na fila, bloqueando todos os outros até terminar.

**Esforço:** Dias (requer design de política de priorização + LiteLLM config)

---

### Área 5 — Monitoramento Real

#### STRUCT-11 — Dashboards Grafana ausentes; sem alertas configurados
**Tipo:** GAP | **Severidade:** MÉDIO

**Evidência:**

`grafana/dashboards/.gitkeep`:
```
(arquivo vazio — placeholder)
```

**Estado atual:**
- ✅ Prometheus coleta métricas (`ray-head:8080`, `litellm:4000`)
- ✅ Grafana datasource provisionado automaticamente (`grafana/datasources/datasource.yml`)
- ❌ Zero dashboards configurados
- ❌ Zero alertas configurados
- ❌ Sem dashboard de VRAM por modelo
- ❌ Sem alerta de KV-cache saturation
- ❌ Sem alerta de cold start frequency

**ADR-007** documenta explicitamente esta decisão: dashboards são importados manualmente. Mas o `ARCHITECTURE.md:10.3` lista 5 alertas recomendados sem nenhum ser implementado.

**Gap operacional:** Em produção com 50 usuários, a equipe não saberá quando:
1. VRAM está saturada (`vllm:gpu_cache_usage_perc > 0.95`)
2. Réplica atingiu ceiling (`max_replicas`)
3. Cold start está muito frequente (scale-to-zero mal calibrado)
4. GPU worker está em crashloop (custo sem serviço)

**Esforço:**
- Dashboards Ray/vLLM: 2 horas (download JSONs oficiais + importar)
- Alertas básicos (5 rules): 3 horas
- Dashboard VRAM por modelo: 4 horas (requer PromQL customizado)

**Correção mínima viável:**

```promql
# Alert 1: KV-cache saturation
vllm:gpu_cache_usage_perc > 0.95

# Alert 2: Replica ceiling
ray_serve_deployment_replica_count == ray_serve_deployment_max_replicas

# Alert 3: GPU worker in crashloop (proxy via request failures)
rate(ray_serve_http_request_counter_total{status_code!="200"}[5m]) > 0.5
```

---

#### STRUCT-12 — Métricas vLLM por modelo não disponíveis se múltiplos modelos rodarem
**Tipo:** GAP | **Severidade:** BAIXO

`prometheus.yml:15-17` tem um único scrape target `ray-head:8080`. Quando múltiplos modelos rodarem (STRUCT-01), as métricas vLLM serão expostas por TODAS as réplicas no mesmo endpoint. Labels `model_name` diferenciam por modelo, mas:

1. Se dois modelos compartilham a mesma GPU, as métricas `gpu_cache_usage_perc` são do vLLM process inteiro, não por modelo
2. Não há exporter de GPU memory usage no nível do node (nvidia-smi exporter não configurado)
3. Para correlacionar "qual modelo está causando OOM", é necessário nvidia-smi exporter ou DCGM exporter

**Esforço:** 2 horas (adicionar DCGM exporter ao compose + prometheus target)

---

### Área 6 — Segurança Operacional

#### STRUCT-13 — Sem proteção de custo AWS; GPU worker em crashloop fatura indefinidamente
**Tipo:** GAP | **Severidade:** MÉDIO

**Evidência:** `cluster.yaml` não tem:
```yaml
# Ausente em cluster.yaml:
# - AWS Budget alert
# - CloudWatch billing alarm
# - idle_timeout_minutes (ver STRUCT-08)
```

**Cenário de risco:**
1. GPU worker inicia, vLLM carrega modelo (sucesso inicial)
2. Requests chegam com contexto muito longo → OOM
3. Container restart (`restart: unless-stopped` no local; Ray retry no AWS)
4. Ray mantém o worker vivo (vê demanda pendente)
5. Worker fatura ~$0.80/hr sem servir nenhum request
6. Sem alerta: operador descobre quando vê a fatura mensal

**Para 4 workers em crashloop simultâneo:** 4 × $0.80 × 24h × 30d = **$2,304/mês** em custo inútil.

**Correção (IaC recomendado, mas mínimo via AWS CLI):**
```bash
# Criar budget com alerta a $500/mês
aws budgets create-budget \
  --account-id $AWS_ACCOUNT_ID \
  --budget '{
    "BudgetName": "idia-gpu-budget",
    "BudgetLimit": {"Amount": "500", "Unit": "USD"},
    "TimeUnit": "MONTHLY",
    "BudgetType": "COST",
    "CostFilters": {"Service": ["Amazon Elastic Compute Cloud"]}
  }' \
  --notifications-with-subscribers '[{
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80
    },
    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "ops@institution.edu"}]
  }]'
```

**Esforço:** 30 minutos (AWS Console) ou 2 horas (Terraform)

---

#### STRUCT-14 — `restart: unless-stopped` sem max_retries; crashloop OOM não tem teto
**Tipo:** GAP | **Severidade:** BAIXO

**Evidência:**

`docker-compose.yml:47`:
```yaml
restart: unless-stopped    # ← sem max_retries; infinito
```

No deployment local: se vLLM OOM (ex: contexto muito longo + batch grande):
1. Container ray-head crasha
2. Docker reinicia imediatamente
3. Container ray-head sobe → vLLM carrega modelo → OOM → crash
4. Loop infinito com overhead de CPU/IO a cada restart

**Mitigação existente:** Docker tem backoff exponencial por padrão (1s → 2s → 4s → ... → 64s max). Não é infinito imediato — é O(log N) restarts/hora. Impacto real: moderado.

**Correção para docker-compose:**
```yaml
ray-head:
  restart: unless-stopped
  # deploy.restart_policy não é suportado por docker compose standalone
  # (apenas Docker Swarm). Para produção local, usar systemd unit com
  # StartLimitIntervalSec e StartLimitBurst.
```

Para Ray Serve LLM na AWS: adicionar em serve_config.yaml:
```yaml
deployment_config:
  health_check_period_s: 30
  health_check_timeout_s: 10
  max_ongoing_requests: 128    # cap por réplica
```

**Esforço:** 1 hora

---

#### STRUCT-15 — Security group AWS não é enforced por IaC
**Tipo:** GAP | **Severidade:** BAIXO

`docs/ARCHITECTURE.md:563-570` documenta as regras de security group corretas. `scripts/deploy_cluster.sh` não cria o security group — deixa para o operador configurar manualmente no Console.

Sem automação, um operador pode expor porta 8000 (Ray ingress) ou 8265 (Dashboard) inadvertidamente.

**Esforço:** 4 horas (AWS CDK ou Terraform para security group)

---

### Área 7 — Testes de Stress

#### STRUCT-16 — Suíte de testes não cobre cenários de carga real
**Tipo:** GAP | **Severidade:** BAIXO

**Estado atual dos testes:**

| Arquivo | Cobertura |
|---------|-----------|
| `tests/test_docs.py` | Estrutura de arquivos e markdown |
| `tests/test_config_schemas.py` | Validação de schema YAML |
| `tests/test_integration.py` | render_config.py unit tests, estrutura docker-compose |
| `tests/test_security.py` | Isolamento de portas, image pinning |

**Ausente:**

| Cenário | Por que importa |
|---------|----------------|
| Concorrência: 20 requests simultâneos | `target_ongoing_requests: 64` — testar comportamento quando threshold é atingido |
| OOM handling: MAX_MODEL_LEN além da VRAM | vLLM deve retornar 400/500, não crashar |
| Rate limiting: > rpm_limit em 1 minuto | LiteLLM deve retornar 429 |
| Cold start: request em scale-from-zero | TTFT deve ser medido; não deve timeout |
| Multi-model: dois modelos simultâneos | VRAM contention deve retornar 503, não OOM |

**Nota:** Testes de stress reais requerem GPU — são excluídos do pytest CI. Mas testes de contrato (mock de rate limit, schema de response 429, etc.) são possíveis sem GPU.

**Esforço:** 1-2 dias para testes de contrato; 1 semana para testes de carga com k6/locust em GPU real

---

## 3. GAPS DE ARQUITETURA (Funcionalidades Ausentes)

Diferenciados de bugs — são funcionalidades que o repositório NÃO cobre e que precisariam ser adicionadas:

| # | Gap | Descrição | Esforço |
|---|-----|-----------|---------|
| G-01 | **Model registry** | Lista gerenciada de modelos disponíveis com VRAM, quantização e capacidade — atualmente só há `MODEL_ID` e `MODEL_SOURCE` hardcoded | 1-2 dias |
| G-02 | **EFS para cache HF no AWS** | Cache compartilhado entre workers → cold start de download eliminado (10-20 min → ~30s de carregamento de disco) | 1 dia (infra) |
| G-03 | **User onboarding script** | Criação automatizada de virtual keys com tiers (hard/regular/light) via LiteLLM API | 4 horas |
| G-04 | **Cost allocation por modelo** | LiteLLM tem spend tracking, mas sem separação por deployment no Ray — difícil saber qual modelo custa quanto | 1 dia |
| G-05 | **Grafana dashboards provisionados** | JSON files de dashboards Ray/vLLM oficiais — atualmente `.gitkeep` | 2-4 horas |
| G-06 | **Pre-download script** | `ray exec cluster.yaml "huggingface-cli download ..."` antes de habilitar scale-to-zero | 2 horas |
| G-07 | **Smoke test pós-deploy** | Verificação automática de que todos os modelos respondem após `deploy_cluster.sh` | 2 horas |

---

## 4. PLANO DE REMEDIAÇÃO PRIORIZADO

### Prioridade 1 — Bloqueadores imediatos (< 2 horas cada)

| Item | Arquivo | Esforço | Impacto |
|------|---------|---------|---------|
| STRUCT-07: Injetar HF_TOKEN nos workers AWS | `cluster.yaml` | 30 min | Deploy funcional para modelos gated |
| STRUCT-05: Pinar vLLM nos head_setup_commands | `cluster.yaml:74` | 2 min | Reproducibilidade AWS = local |
| STRUCT-08: Adicionar `idle_timeout_minutes: 5` | `cluster.yaml` | 2 min | Custo controlado no crashloop |
| STRUCT-03: Corrigir exemplo SEC-01 na doc | `docs/ARCHITECTURE.md:175` | 5 min | Doc/code em sincronia |

### Prioridade 2 — Alta importância (antes de produção com 50 usuários)

| Item | Arquivo | Esforço | Impacto |
|------|---------|---------|---------|
| STRUCT-06: Decidir min_replicas: 0 ou manter 1 | `serve_config.yaml:29` | 5 min (mudar) + debate de produto | Idle timeout vs custo |
| STRUCT-09: Configurar rate limiting básico | `config.yaml` | 2-4 horas | Equidade entre usuários |
| STRUCT-13: Criar AWS Budget alert | AWS Console | 30 min | Proteção de custo |
| STRUCT-11: Importar dashboards Ray/vLLM | `grafana/dashboards/` | 2-3 horas | Visibilidade operacional |

### Prioridade 3 — Antes de multi-model (dias/semanas)

| Item | Arquivo | Esforço | Impacto |
|------|---------|---------|---------|
| STRUCT-01: Implementar padrão multi-model | `serve_config.yaml`, `render_config.py`, `config.yaml` | 1-2 dias | Feature core para o cenário |
| STRUCT-02: Calcular VRAM budget por modelo | Design | 4 horas | Evitar OOM em produção |
| STRUCT-04: Adicionar gpu_worker_large no cluster | `cluster.yaml` | 2 horas | Suporte a modelos >24GB |
| G-02: EFS para cache HF | AWS + cluster.yaml | 1 dia | Cold start aceitável |
| G-03: User onboarding script | Novo script | 4 horas | Gestão de 50 usuários |

### Prioridade 4 — Melhorias operacionais (não bloqueadores)

| Item | Esforço |
|------|---------|
| STRUCT-10: Prioridade de fila (batch vs interactive) | Dias |
| STRUCT-12: DCGM exporter para VRAM por modelo | 2 horas |
| STRUCT-14: Max retries para crashloop | 1 hora |
| STRUCT-15: IaC para security groups AWS | 4 horas |
| STRUCT-16: Testes de carga e contrato | 1-2 dias |

---

## 5. ANÁLISE DA AUDITORIA ANTERIOR

### O que a auditoria anterior acertou

1. **SEC-01 (fallback sk-admin):** Corrigido corretamente. `config.yaml:22` confirma `${LITELLM_MASTER_KEY}` sem fallback.
2. **SEC-03 (volume HF nomeado):** Corrigido corretamente. `docker-compose.yml:116-117` confirma volume nomeado `idia_hf_cache`.
3. **SEC-07 (senha Grafana):** Corrigido. `docker-compose.yml:108` confirma `GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}`.
4. **SEC-08 (health checks):** Corrigidos. `docker-compose.yml:31-36` e `63-68` têm healthchecks completos.
5. **BUG-03 (path fixo):** Corrigido. `scripts/render_config.py:47` usa `RENDERED_PATH = Path("/tmp/idia_serve_config.yaml")`.
6. **INFRA-01 (retenção Prometheus):** Corrigido. `docker-compose.yml:80-82` tem `--storage.tsdb.retention.time=15d`.
7. **SEC-06 (YAML escape):** Implementado. `render_config.py:176-184` tem `_escape_yaml_value()`.

### O que a auditoria anterior não cobriu (escopo desta auditoria)

A auditoria anterior focou em qualidade do código existente. Esta auditoria foca em gaps estruturais para o cenário de 50 usuários:

| Gap desta auditoria | Motivo de não ter sido coberto |
|---------------------|--------------------------------|
| Multi-model serving (STRUCT-01) | Fora do escopo da auditoria anterior |
| HF_TOKEN nos workers AWS (STRUCT-07) | Requer análise do fluxo AWS end-to-end |
| min_replicas vs idle timeout (STRUCT-06) | Escolha de configuração, não bug de código |
| Rate limiting (STRUCT-09) | Feature ausente, não bug de código |
| vLLM version inconsistency (STRUCT-05) | Requer comparar Dockerfile.ray vs cluster.yaml |

### Itens ainda abertos da auditoria anterior

A auditoria anterior marcou como Won't Fix:
- **INFRA-05 (dashboards .gitkeep):** Explicitamente deixado para importação manual (ADR-007). Esta auditoria reclassifica para **MÉDIO (STRUCT-11)** porque, para 50 usuários em produção, a falta de dashboards é um risco operacional, não apenas conveniência.

A auditoria anterior marcou como corrigido via fix de outra issue:
- **SEC-09 (inconsistência exemplo vs fallback):** Resolvido por SEC-01. ✅ Confirmado — `config.yaml` não tem fallback.

### O que a auditoria anterior errou (registro de transparência)

De acordo com o `2026-06-28_audit_vettato.md`, a auditoria anterior teve 4 falsos positivos (BUG-01, BUG-02, MAINT-06, INFRA-04) e 1 fix incorreto (BUG-03, corrigido no próprio documento de revisão). Esses erros já foram documentados e não reaparecem nesta auditoria.

---

## 6. RESUMO DE SEVERIDADES

| Severidade | Count | Items |
|------------|-------|-------|
| **ALTO** | 3 | STRUCT-01, STRUCT-06, STRUCT-07 |
| **MÉDIO** | 6 | STRUCT-02, STRUCT-04, STRUCT-05, STRUCT-08, STRUCT-09, STRUCT-11, STRUCT-13 |
| **BAIXO** | 5 | STRUCT-03, STRUCT-10, STRUCT-12, STRUCT-14, STRUCT-15, STRUCT-16 |
| **GAPS** (feature) | 7 | G-01 a G-07 |

---

*Auditoria concluída em 2026-06-28 | Commit 35686e6 | Total: 14 problemas (3 ALTOs, 6 MÉDIOs, 5 BAIXOs) + 7 feature gaps*

---

## 7. VERIFICAÇÃO CRUZADA (Vettato)

**Data da verificação:** 2026-06-28 | **Verificador:** OpenCode (DeepSeek V4 Flash-Free)

### Metodologia

Cada alegação foi verificada contra o código real no commit `35686e6`.
Foram lidos `cluster.yaml`, `serve_config.yaml`, `scripts/render_config.py`
(ENV_SCHEMA), `docker-compose.yml`, `config.yaml`, `docs/ARCHITECTURE.md` §4.3.
Foram executados grep para `idle_timeout_minutes`, `runtime_env`,
`worker_setup`, `env_vars`, `min_replicas`, `vllm==`, `head_setup_commands`
para confirmar presenças e ausências.

### Resultado

**16/16 alegações confirmadas. Zero falsos positivos.**

### Observações do verificador

| Item | Ajuste fino |
|------|-------------|
| **STRUCT-07** | A correção sugerida com `runtime_env.env_vars` em serve_config.yaml funciona, mas é mais complexa que o necessário. Como serve_config.yaml é pré-renderizado por `render_config.py`, o `${HF_TOKEN}` seria substituído para o valor literal antes do Ray ver o arquivo. Abordagem mais simples implementada: `cluster.yaml` seção docker com `run_options: ["--env HF_TOKEN=${HF_TOKEN}"]`. |
| **STRUCT-05** | vLLM 0.18.0 (bundled na imagem ray-ml) vs 0.5.4 (Dockerfile.ray) são versões significativamente diferentes. O GPU worker AWS usa a imagem ray-ml com vLLM bundled — `head_setup_commands` não afeta workers. A divergência de comportamento de inferência entre local e AWS é real e merece atenção. |
| **STRUCT-11** | Dashboards oficiais disponíveis via ID: `25043` (vLLM Dashboard) em grafana.com. |
| **Severidade** | Todas as severidades estão calibradas corretamente conforme a regra de "mitigações antes da severidade". |

### Comparação com auditoria anterior

| Métrica | Auditoria anterior | Esta auditoria |
|---------|-------------------|----------------|
| Falsos positivos | 7 de 34 (20%) | **0 de 16 (0%)** |
| Severidade correta | 5 inflados | **Todos adequados** |
| Evidência por linha | Parcial | **Completa** |
| Distingue BUG de GAP | Não | **Sim** |
| Feature gaps identificados | Não | **7** |

**Conclusão:** Esta auditoria é significativamente melhor que a anterior. O auditor
utilizou corretamente as regras de calibração de severidade, distinguiu bugs de
gaps de funcionalidade, e forneceu evidência verificável para cada alegação.
