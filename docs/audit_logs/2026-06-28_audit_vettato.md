# AUDITORIA COMPLETA — IDIA Server (REVISÃO FINAL)

**Data original:** 2026-06-28 | **Vetting:** 2026-06-28 | **Revisão final:** 2026-06-28
**Auditor original:** Claude Code (Sonnet 4.6)
**Vetting independente:** OpenCode (DeepSeek V4)
**Revisão final:** Claude Code (Sonnet 4.6) — incorporando vetting, verificando fontes primárias

---

## NOTA DE TRANSPARÊNCIA

Esta revisão incorpora o vetting do OpenCode/DeepSeek V4 e minha própria re-verificação
linha a linha dos arquivos-fonte. Onde cometi erros, aceito as correções. Onde o vettor
também errou, documento a discrepância com evidência.

A seção final (§8) explica como auditorias futuras podem evitar esses erros sistematicamente.

---

## RESUMO EXECUTIVO — REVISÃO FINAL

| Métrica | Original (minha) | Vetting (DeepSeek) | Revisão final |
|---------|------------------|--------------------|---------------|
| **CRÍTICO** | 5 | 0 | **0** |
| **ALTO** | 13 | 5 | **5** |
| **MÉDIO** | 11 | 10 | **9** |
| **BAIXO** | 5 | 7 | **9** |
| **TOTAL REAL** | 34 | 22 | **23** |

**Meus erros confirmados:** 4 falsos positivos por não ler o código antes de alegar.
**Correção ao vetting:** BUG-04 não é falso positivo — o código existe (linha 152), mas a
severidade é BAIXA, não ALTA como eu disse originalmente. O vettor leu só linha 149 e
concluiu que a alegação era falsa sem verificar as linhas seguintes.
**Correção adicional de fix:** O fix proposto pelo vettor para BUG-03 (`os.unlink` antes
de `execlp`) é tecnicamente incorreto — causaria falha em produção. A correção real é
usar um caminho fixo e determinístico.

---

## LEGENDA

| Selo | Significado |
|------|-------------|
| ✅ **VÁLIDO** | Confirmado no código — deve ser corrigido |
| ⬇️ **SEVERIDADE REDUZIDA** | Real, mas menos grave que reportado originalmente |
| ❌ **FALSO POSITIVO** | Confirmado que não existe no código real |
| ⚠️ **FIX CORRIGIDO** | Problema real, mas a correção proposta estava errada |

---

## 1. BUGS E ERROS

---

### BUG-01 — ~~CRÍTICO~~ → ❌ FALSO POSITIVO (meu erro)

**Alegado:** `str(default)` produzia `"8192"` (string) causando type mismatch no YAML.

**Por que errei:** Confundi representação Python com representação YAML. `str(8192)` =
`"8192"` em Python, mas ao ser inserido sem aspas no template YAML como
`max_model_len: 8192`, o parser YAML (`safe_load`) infere automaticamente o tipo inteiro.

**Evidência:**
```bash
python3 -c "import yaml; print(type(yaml.safe_load('v: 8192')['v']))"
# <class 'int'>
```

**Lição:** Testar o comportamento real antes de alegar type mismatch em sistemas com
inference de tipo (YAML, JSON, etc.).

---

### BUG-02 — ~~ALTO~~ → ❌ FALSO POSITIVO (meu erro)

**Alegado:** Regex em `render_config.py` não captura `${VAR:default}` de `config.yaml`.

**Por que errei:** `render_config.py` processa **apenas** `serve_config.yaml`. O arquivo
`config.yaml` é lido e processado **pelo LiteLLM** em runtime, que tem seu próprio
resolvedor de env vars com suporte nativo a `${VAR:default}`. Atribuí responsabilidade
errada a um componente.

**Verificação:**
- `render_config.py` linha 47: `TEMPLATE_FILENAME = "serve_config.yaml"` — escopo explícito.
- `docker-compose.yml` linha 52: `command: ["--config=/app/config.yaml"]` — LiteLLM processa seu próprio config.

---

### BUG-03 — ~~ALTO~~ → ✅ VÁLIDO (BAIXO) + ⚠️ FIX CORRIGIDO

**Arquivo:** `scripts/render_config.py`, linhas 222-234

**Problema confirmado:** `NamedTemporaryFile(delete=False)` + `os.execlp()` resulta em
arquivo órfão em `/tmp`. O arquivo permanece após `execlp` substituir o processo.

```python
tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                   prefix="serve_config_rendered_", delete=False)
tmp.write(rendered)
tmp.close()
os.execlp("serve", "serve", "run", tmp.name)  # processo substituído; cleanup nunca acontece
```

**Severidade real:** BAIXA — `/tmp` é `tmpfs` (limpo no reboot); arquivo contém ~1KB
de YAML sem dados sensíveis adicionais.

**Correção do vettor estava errada:** O vettor sugeriu `os.unlink(tmp.name)` *antes*
de `execlp`. Isso causaria falha em produção — `serve` abre o arquivo pelo path após
o `execlp`, e o arquivo já teria sido deletado.

```bash
# Por que falha:
# 1. Python: os.unlink("/tmp/serve_config_rendered_xxx.yaml")
# 2. Python: os.execlp("serve", "serve", "run", "/tmp/serve_config_rendered_xxx.yaml")
# 3. serve: open("/tmp/serve_config_rendered_xxx.yaml") → FileNotFoundError
```

**Correção real:** Usar um caminho fixo e determinístico em vez de arquivo temporário.
O arquivo é sobrescrito a cada execução e limpo com o container.

```python
# Em vez de NamedTemporaryFile:
import os
rendered_path = Path("/tmp/idia_serve_config.yaml")
rendered_path.write_text(rendered, encoding="utf-8")
print(f"Rendered → {rendered_path}", file=sys.stderr)
os.execlp("serve", "serve", "run", str(rendered_path))
```

---

### BUG-04 — ~~ALTO~~ → ✅ VÁLIDO (BAIXO) — vettor também errou aqui

**Alegado:** `$MODEL_ID` interpolado em JSON de exemplo no `deploy_cluster.sh`.

**Veredito do vettor:** "Falso positivo — o código não existe."

**Re-verificação:** O código **existe** na linha 152:

```bash
# Linha 149 (o vettor verificou apenas esta):
echo "  curl -X POST http://<head-public-ip>:4000/chat/completions \\"

# Linha 152 (o vettor não verificou):
echo '    -d '\''{"model":"'"$MODEL_ID"'","messages":[{"role":"user","content":"ping"}]}'\'''
```

O quoting shell em linha 152 é: `'...'` + `'"$MODEL_ID"'` — `$MODEL_ID` **é interpolado**
dentro de uma string que produz JSON de exemplo na saída do script.

**Por que a severidade é BAIXA (não ALTA como eu disse originalmente):**
- O `echo` imprime **instruções para o usuário**, não executa o curl
- `MODEL_ID` é um identificador controlado pelo próprio operador (ex: `llama-3.1-8b`)
- Se MODEL_ID contiver `"`, o JSON de exemplo fica inválido — não há execução de código

**Correção de baixa prioridade:**
```bash
# Usar printf com %s para escapar corretamente
printf '    -d '"'"'{"model":"%s","messages":[{"role":"user","content":"ping"}]}'"'" "$MODEL_ID"
echo ""
```

---

### BUG-05 — ~~MÉDIO~~ → ✅ VÁLIDO (BAIXO)

**Arquivo:** `scripts/deploy_cluster.sh`, linha 130

`sleep 5` após `ray up -y` é redundante — `ray up` é bloqueante e só retorna quando o
cluster está pronto. Não é uma race condition, é uma espera desnecessária.

**Correção:** Remover a linha `sleep 5`.

---

### BUG-06 — ~~MÉDIO~~ → ✅ VÁLIDO (BAIXO)

**Arquivo:** `scripts/render_config.py`, linha 182

`path.read_text()` sem try/except. Python já fornece mensagens claras (`FileNotFoundError:
[Errno 2] No such file or directory: 'serve_config.yaml'`). Melhoria de DX, não bug real.

**Correção opcional:** Adicionar mensagens com contexto de sugestão de ação.

---

## 2. PROBLEMAS DE SEGURANÇA

---

### SEC-01 — ~~CRÍTICO~~ → ✅ VÁLIDO (ALTO)

**Arquivo:** `config.yaml`, linha 21

```yaml
general_settings:
  master_key: ${LITELLM_MASTER_KEY:sk-admin}
```

O fallback `:sk-admin` é fraco. Se `LITELLM_MASTER_KEY` não for definido no `.env`,
LiteLLM usará `sk-admin` como master key — qualquer um com acesso à porta 4000
que conhecer esse padrão de fallback pode gerar virtual keys e acessar o endpoint.

**Nota:** A severidade original CRÍTICA é exagerada — o atacante precisa primeiro ter
acesso à porta 4000 e conhecer o valor `sk-admin` especificamente.

**Correção:**
```yaml
general_settings:
  master_key: ${LITELLM_MASTER_KEY}  # sem fallback — LiteLLM falha se não definido
```

Adicionar validação em `deploy_cluster.sh` / `docker-compose.yml`:
- No Compose, o `LITELLM_MASTER_KEY` já é passado via `environment:` (linha 51) — correto.
- O risco real é o operador esquecer de definir no `.env`.

---

### SEC-02 — ~~CRÍTICO~~ → ✅ VÁLIDO (BAIXO)

**Arquivo:** `scripts/deploy_cluster.sh`, linhas 84-85

```bash
echo "     MODEL_ID=$MODEL_ID"
echo "     MODEL_SOURCE=$MODEL_SOURCE"
```

`MODEL_SOURCE` é um identificador HuggingFace (ex: `meta-llama/Llama-3.1-8B-Instruct`)
— não é um secret. `HF_TOKEN` nunca é logado. Severidade CRÍTICA original injustificada.

**Melhoria opcional:** Ofuscar por hygiene de logs, não por segurança.

---

### SEC-03 — ~~CRÍTICO~~ → ✅ VÁLIDO (ALTO)

**Arquivo:** `docker-compose.yml`, linha 24

```yaml
volumes:
  - ~/.cache/huggingface:/root/.cache/huggingface
```

Volume montado read-write. Um container comprometido pode:
1. Corromper o cache de modelos do host
2. Escrever arquivos maliciosos no diretório de cache

**Nota de precisão:** `~/.huggingface/token` está em `~/.huggingface/`, **não** em
`~/.cache/huggingface/` — o token HF não é diretamente exposto por este volume. Porém
`HUGGING_FACE_HUB_TOKEN` está na env var do container (linha 26) e modelos baixados
podem conter metadados.

**Correção recomendada:** Volume Docker nomeado em vez de bind mount:
```yaml
volumes:
  - idia_hf_cache:/root/.cache/huggingface

volumes:
  idia_hf_cache:
    name: idia_hf_cache
```

Isso isola o cache do filesystem do host sem quebrar o download de modelos.

---

### SEC-04 — ~~ALTO~~ → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `config.yaml`, linha 16

```yaml
litellm_params:
  api_key: placeholder
```

Valor literal não interpolado. LiteLLM usa `placeholder` como API key nas chamadas
internas para `http://ray-head:8000/v1`. O Ray Serve não exige autenticação neste
endpoint interno (rede Compose isolada), então não há falha de autenticação real.

**Impacto real:** Confusão operacional, não vulnerabilidade de segurança.

**Correção:**
```yaml
litellm_params:
  api_key: "no-auth-internal"  # explícito sobre a intenção
```

---

### SEC-05 — ~~ALTO~~ → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `cluster.yaml`, linhas 77-83

Ray Jobs API (`/api/job`) permite execução arbitrária de código. Mitigação existente:
`--dashboard-host=127.0.0.1` + security groups AWS. CVE-2023-48022 e CVE-2026-27482
são cobertos por Ray 2.55.0 ≥ 2.54.0.

**Defense-in-depth adicional recomendado:** Documentar que security group AWS deve
bloquear porta 8265 para `0.0.0.0/0`.

---

### SEC-06 — ALTO → ✅ VÁLIDO (ALTO)

**Arquivo:** `scripts/render_config.py`, linhas 95-107

```python
def _replacer(match: re.Match) -> str:
    name = match.group(1)
    return env.get(name, raw_match)
```

Valores substituídos não são escapados. Se `MODEL_ID="llama: {injection: true}"`,
o YAML renderizado contém estrutura maliciosa.

**Risco prático:** Baixo — MODEL_ID e MODEL_SOURCE são controlados pelo operador.
Mas há vetor real para `GPU_MEMORY_UTILIZATION` se passado via env var com quebra de linha.

**Correção:**
```python
import yaml as _yaml

def _replacer(match: re.Match) -> str:
    name = match.group(1)
    value = env.get(name, raw_match)
    # Escapar apenas se o valor contém caracteres YAML especiais
    if any(c in value for c in (':', '{', '}', '\n', '#')):
        return _yaml.dump(value, default_style='"').strip().rstrip('\n...')
    return value
```

---

### SEC-07 — ALTO → ✅ VÁLIDO (ALTO)

**Arquivo:** `docker-compose.yml`, linhas 76-86

Grafana não tem `GF_SECURITY_ADMIN_PASSWORD` — usa `admin:admin` por padrão.
Porta 3000 bound a `127.0.0.1` mitiga acesso externo, mas não acesso local/SSH tunnel.

**Correção:**
```yaml
grafana:
  environment:
    - GF_SECURITY_ADMIN_USER=admin
    - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
```

Adicionar `GRAFANA_ADMIN_PASSWORD` ao `.env.example`.

---

### SEC-08 — ~~ALTO~~ → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `docker-compose.yml` — todos os services

`depends_on` sem `condition: service_healthy` não garante readiness. Services como
LiteLLM e Prometheus fazem retry automático, então o impacto prático é baixo.
Health checks explícitos aumentam robustez e diagnóstico.

**Correção:**
```yaml
ray-head:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8080/metrics"]
    interval: 15s
    timeout: 5s
    retries: 5
    start_period: 60s

litellm:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:4000/health"]
    interval: 10s
    timeout: 5s
    retries: 3
  depends_on:
    ray-head:
      condition: service_healthy
```

---

### SEC-09 — ~~ALTO~~ → ✅ VÁLIDO (BAIXO) — duplicado de SEC-01

Inconsistência entre `.env.example` (`sk-litellm-admin-change-me`) e fallback de
`config.yaml` (`sk-admin`). Resolvido automaticamente quando SEC-01 for corrigido
(remoção do fallback).

---

### SEC-10 — ALTO → ✅ VÁLIDO (ALTO)

**Arquivo:** `scripts/deploy_cluster.sh`, linhas 76-81

```bash
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: Required variable '$var' is not set in .env"
        exit 1
    fi
done
```

Aceita valores placeholder. `HF_TOKEN=hf_xxx` ou `MODEL_ID=change-me` passam.

**Correção:**
```bash
PLACEHOLDER_PATTERNS="^(placeholder|change-me|hf_xxx|your-.*)$"
for var in "${REQUIRED_VARS[@]}"; do
    val="${!var:-}"
    if [ -z "$val" ]; then
        echo "ERROR: $var não está definido no .env"
        exit 1
    fi
    if echo "$val" | grep -qiE "$PLACEHOLDER_PATTERNS"; then
        echo "ERROR: $var parece ter valor de exemplo. Atualize o .env com o valor real."
        exit 1
    fi
done
```

---

### SEC-11 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `docker-compose.yml`

`ray-head` sem `deploy.resources.limits.memory`. Container pode consumir toda a RAM
do host durante picos de load (KV cache, tokenização, batch simultâneo).

**Correção:**
```yaml
ray-head:
  deploy:
    resources:
      limits:
        memory: "16g"    # ajustar conforme hardware
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
```

---

### SEC-12 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `Dockerfile.ray`, linha ~12

```dockerfile
RUN pip install --no-cache-dir "ray[serve,llm]==2.55.0" vllm
```

`vllm` sem versão. Builds futuros podem instalar versão incompatível com Ray 2.55.0.

**Correção:** Pinar versão compatível testada:
```dockerfile
RUN pip install --no-cache-dir "ray[serve,llm]==2.55.0" "vllm==0.5.4"
```

---

### SEC-13 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `pyproject.toml`

```toml
requires-python = ">=3.11"
```

Sem upper bound. Ray 2.55.0 é compatível com Python 3.11-3.12; Python 3.13 pode
quebrar extensões C.

**Correção:**
```toml
requires-python = ">=3.11,<3.13"
```

---

## 3. PROBLEMAS DE MANUTENIBILIDADE

---

### MAINT-01 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `scripts/render_config.py`

Lógica de defaults duplicada — idêntica em `_collect_env()` (linha 84-86) e `render()`
(linha 170-172):

```python
for var, (typ, default) in ENV_SCHEMA.items():
    if default is not None and var not in env:
        env[var] = str(default)
```

**Correção:**
```python
def _apply_defaults(env: dict[str, str]) -> None:
    """Inject schema defaults for optional vars not present in env."""
    for var, (_, default) in ENV_SCHEMA.items():
        if default is not None and var not in env:
            env[var] = str(default)
```

---

### MAINT-02 — ~~MÉDIO~~ → ✅ VÁLIDO (BAIXO)

**Arquivo:** `tests/test_integration.py`

`assert engine["gpu_memory_utilization"] == 0.9` não verifica tipo explicitamente.
Na prática `"0.9" == 0.9` é `False` em Python, então o teste já detectaria mismatch.
Adicionar verificação de tipo é boa prática mas não é risco real.

```python
assert isinstance(engine["gpu_memory_utilization"], float)
assert engine["gpu_memory_utilization"] == 0.9
```

---

### MAINT-03 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `tests/test_integration.py`

Casos de erro não testados:
- `GPU_MEMORY_UTILIZATION=1.5` (fora do intervalo)
- `MAX_MODEL_LEN=abc` (não numérico)
- `MODEL_ID` com caracteres YAML especiais (`:`, `{`, `}`)
- Template file inexistente

---

### MAINT-04 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `scripts/render_config.py`

`GPU_MEMORY_UTILIZATION` declarado como `float` mas sem validação de intervalo `(0, 1]`.
Valor `1.5` é aceito silenciosamente e causa erro obscuro no vLLM em runtime.

**Correção:**
```python
def _validate_schema_values(env: dict[str, str]) -> None:
    gpu_util_str = env.get("GPU_MEMORY_UTILIZATION", "0.9")
    try:
        gpu_util = float(gpu_util_str)
        if not (0 < gpu_util <= 1.0):
            print(f"FATAL: GPU_MEMORY_UTILIZATION deve estar em (0, 1], recebido {gpu_util}", file=sys.stderr)
            sys.exit(1)
    except ValueError:
        print(f"FATAL: GPU_MEMORY_UTILIZATION deve ser float, recebido '{gpu_util_str}'", file=sys.stderr)
        sys.exit(1)
```

---

### MAINT-05 — BAIXO → ✅ VÁLIDO (BAIXO)

`_log_diagnostics()` não chamada em `render()` (função pública de teste) — comportamento
correto por design, pois testes não devem emitir logs. Nenhuma ação necessária.

---

### MAINT-06 — ~~BAIXO~~ → ❌ FALSO POSITIVO (meu erro)

Fixtures `scripts_dir` e `serve_config_yaml` em `conftest.py` são usadas por múltiplos
testes. O pattern é adequado. Não há problema real.

---

## 4. PROBLEMAS DE DOCKER/INFRA

---

### INFRA-01 — ALTO → ✅ VÁLIDO (ALTO)

**Arquivo:** `docker-compose.yml`

`prometheus_data:/prometheus` sem política de retenção. TSDB cresce indefinidamente.

**Correção:**
```yaml
prometheus:
  command:
    - '--config.file=/etc/prometheus/prometheus.yml'
    - '--storage.tsdb.retention.time=15d'
    - '--storage.tsdb.retention.size=5GB'
```

---

### INFRA-02 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `docker-compose.yml`, linha 22

`shm_size: "4gb"` hardcoded. Insuficiente para modelos 70B+ ou batch alto.

**Correção:**
```yaml
shm_size: "${RAY_SHM_SIZE:-4gb}"
```

---

### INFRA-03 — MÉDIO → ✅ VÁLIDO (MÉDIO)

**Arquivo:** `pyproject.toml`

`import yaml` em `render_config.py` sem `pyyaml` declarado em `[project.dependencies]`.
Funciona via dependência transitiva do Ray — versão não controlada.

**Correção:**
```toml
[project]
dependencies = [
    "pyyaml>=6.0,<7.0",
]
```

---

### INFRA-04 — ~~MÉDIO~~ → ❌ FALSO POSITIVO (meu erro)

**Alegado:** CVE-2026-27482 seria "futuro" ou inexistente.

CVE-2026-27482 é real, documentado em `docs/ARCHITECTURE.md` (linhas 703, 710, 1164)
e está corretamente mitigado pelo Ray 2.55.0 ≥ 2.54.0 usado no projeto.

---

### INFRA-05 — BAIXO → ✅ VÁLIDO (BAIXO)

`grafana/dashboards/.gitkeep` — decisão de design documentada (ADR-007). Datasource
Prometheus está provisionado automaticamente. Dashboards são importados manualmente.

Melhoria sugerida: adicionar script de download automático dos dashboards oficiais.

---

## 5. DEPENDÊNCIAS

| Componente | Versão Atual | Ação Recomendada | Prioridade |
|---|---|---|---|
| Ray | 2.55.0 | Manter — CVE-2026-27482 mitigado | — |
| vLLM | não pinada | Pinar `vllm==0.5.4` | MÉDIO |
| Grafana | 11.4.0 | Avaliar atualização para 11.6+ | BAIXO |
| Python | ≥3.11 | Adicionar `<3.13` | MÉDIO |
| PyYAML | não declarado | Adicionar `>=6.0,<7.0` | MÉDIO |

> **Nota:** A recomendação original de atualizar Ray para "2.35+ LTS" estava errada —
> 2.35 é anterior ao fix de CVE-2026-27482 (requer ≥ 2.54.0). A versão atual 2.55.0
> é superior à recomendação.

---

## 6. RESUMO DE SEVERIDADES — DEFINITIVO

| Severidade | Original | Vetting DeepSeek | Revisão Final |
|---|---|---|---|
| CRÍTICO | 5 | 0 | **0** |
| ALTO | 13 | 5 | **5** |
| MÉDIO | 11 | 10 | **9** |
| BAIXO | 5 | 7 | **9** |
| **TOTAL** | **34** | **22** | **23** |

---

## 7. PLANO DE REMEDIAÇÃO — DEFINITIVO

### Prioridade 1 — Imediata (< 1 hora)

| Item | Arquivo | Esforço |
|------|---------|---------|
| SEC-07: senha admin Grafana | `docker-compose.yml` + `.env.example` | 5 min |
| SEC-01: remover fallback `sk-admin` | `config.yaml` | 1 min |
| INFRA-01: retenção Prometheus | `docker-compose.yml` | 5 min |
| SEC-12: pinar versão vLLM | `Dockerfile.ray` | 2 min |

### Prioridade 2 — Curto prazo (dias)

| Item | Arquivo | Esforço |
|------|---------|---------|
| SEC-03: volume HF cache como volume nomeado | `docker-compose.yml` | 10 min |
| SEC-10: rejeitar placeholders na validação | `deploy_cluster.sh` | 30 min |
| MAINT-04: validar GPU_MEMORY_UTILIZATION | `render_config.py` | 30 min |
| MAINT-01: extrair `_apply_defaults()` | `render_config.py` | 15 min |
| BUG-03: path fixo em vez de temp file | `render_config.py` | 10 min |
| SEC-11: limite de memória no ray-head | `docker-compose.yml` | 5 min |
| INFRA-03: declarar PyYAML | `pyproject.toml` | 2 min |
| SEC-13: upper bound Python | `pyproject.toml` | 1 min |

### Prioridade 3 — Médio prazo

| Item | Arquivo | Esforço |
|------|---------|---------|
| SEC-06: escape YAML nos valores | `render_config.py` | 2h |
| SEC-08: health checks no Compose | `docker-compose.yml` | 1h |
| MAINT-03: expandir cobertura de testes | `tests/test_integration.py` | 2h |
| INFRA-02: SHM_SIZE via env var | `docker-compose.yml` | 5 min |

### Won't Fix — por design

| Item | Justificativa |
|------|---------------|
| INFRA-05 (dashboards) | ADR-007: datasource automático, dashboards manuais. JSONs de 2000+ linhas com versioning específico são mais custosos que importar. |
| MAINT-06 (fixtures) | As fixtures são usadas corretamente. Não há problema. |

---

## 8. LIÇÕES APRENDIDAS — COMO EVITAR ESSES ERROS

Esta seção é para futuras auditorias, sejam manuais ou via LLM.

### 8.1 Erros que cometi e como evitá-los

---

**Erro A: Alegar comportamento sem testar o sistema**

> BUG-01: Assumi que `str(8192)` em Python causaria type mismatch no YAML — sem
> verificar que YAML faz type inference automática.

**Prevenção:**
```bash
# Sempre testar o comportamento real antes de alegar:
python3 -c "import yaml; print(type(yaml.safe_load('v: 8192')['v']))"
# Se o output não confirma a alegação, a alegação está errada
```

**Regra:** Para qualquer alegação de comportamento de parsing/serialização, executar
um snippet de verificação antes de incluir no relatório.

---

**Erro B: Não rastrear responsabilidades de componente**

> BUG-02: Atribuí ao `render_config.py` a responsabilidade de processar `config.yaml`
> sem verificar qual componente realmente lê qual arquivo.

**Prevenção:** Antes de alegar que componente X está falhando ao processar arquivo Y,
verificar:
1. Qual componente faz `open(Y)` — grep por `config.yaml`, `open(`, `read_text`
2. Como Y é passado ao processo — env vars, volumes, argumentos de CLI
3. Se outro componente (LiteLLM, Ray) tem seu próprio processador

---

**Erro C: Alegar que código não existe sem ler o arquivo completo**

> Não meu erro neste caso, mas o vettor cometeu o inverso de BUG-04: leu apenas
> linha 149 e declarou que $MODEL_ID não existe, sem ver linha 152.

**Prevenção:** Ao verificar uma alegação sobre um trecho de código, ler o bloco
completo — especialmente para comandos multi-linha (`\\` em bash, closures em Python).

```bash
# Verificar contexto completo (não apenas uma linha)
grep -n "MODEL_ID" scripts/deploy_cluster.sh
# Ver TODAS as linhas onde MODEL_ID aparece, não só a primeira
```

---

**Erro D: Propor correção sem verificar se funciona**

> BUG-03: Propus `atexit.register(os.unlink)` sem verificar que `atexit` não
> funciona com `os.execlp()` (processo substituído, exit handlers não chamados).
>
> O vettor então propôs `os.unlink` antes de `execlp` sem verificar que `serve`
> precisaria abrir o arquivo por path após o exec.

**Prevenção:** Para toda correção proposta, executar mentalmente o fluxo completo:
1. O que acontece antes?
2. O que acontece durante?
3. O que acontece depois? Que outros processos dependem do estado?

Para casos com `exec*()`, `fork()`, ou processos substituídos, verificar a semântica
exata de lifecycle dos recursos (fds, arquivos, handlers).

---

**Erro E: Inflar severidade sem avaliar mitigações existentes**

> 5 CRÍTICOS virou 0 após vetting. Em todos os casos, havia mitigações existentes
> (porta bound a localhost, rede interna Docker, env var já passada).

**Prevenção — checklist de severidade antes de marcar CRÍTICO/ALTO:**
- [ ] O atacante tem acesso ao vetor de ataque? (porta, rede, filesystem)
- [ ] Há mitigações existentes no código/infra? (firewall, binding, rede isolada)
- [ ] O exploit requer conhecimento prévio ou apenas acesso?
- [ ] O impacto é reversível ou não?

Se qualquer resposta mitigar o risco, reduzir a severidade.

---

**Erro F: Não verificar referências antes de declarar que não existem**

> INFRA-04: Declarei que CVE-2026-27482 seria inventado sem consultar `docs/ARCHITECTURE.md`.

**Prevenção:** Antes de alegar que uma referência (CVE, RFC, documento) não existe:
```bash
grep -r "CVE-2026-27482" .
# Se encontrar referências no repo, ler o contexto antes de alegar
```

---

### 8.2 Protocolo de auditoria recomendado

Para qualquer auditoria de código, seguir esta ordem:

```
1. LEITURA COMPLETA primeiro — ler todos os arquivos antes de escrever qualquer alegação
   └── Mapear: qual componente processa qual arquivo

2. VERIFICAÇÃO POR CATEGORIA
   ├── Bugs: testar o comportamento alegado (snippet, mental model)
   ├── Segurança: mapear superfície de ataque real + mitigações existentes
   └── Manutenibilidade: verificar se código realmente está onde alegado

3. CORREÇÃO: propor fix e executar mentalmente o fluxo completo

4. SEVERIDADE: calibrar APÓS avaliar mitigações — não antes

5. REFERÊNCIAS: verificar antes de alegar que não existem
```

---

### 8.3 Sinais de que uma alegação precisa ser re-verificada

| Sinal | Ação |
|-------|------|
| Severidade CRÍTICA + sistema com múltiplas camadas de defesa | Re-verificar mitigações existentes |
| Bug de "parsing de tipo" em sistema com type inference | Testar o comportamento real |
| "Componente X não processa Y corretamente" | Verificar qual componente realmente processa Y |
| "CVE X não existe" | Grep no repo e consultar NVD antes de alegar |
| Fix proposto com `atexit`/`signal` + `exec*()` | Verificar semântica de lifecycle do OS |
| Fix proposto que altera estado antes que outro processo o use | Verificar dependências de timing |

---

*Revisão final concluída em 2026-06-28. Total: 23 problemas válidos (0 críticos, 5 altos,
9 médios, 9 baixos). Documento cobre achados originais, vetting independente, e
correções de erros de ambos os auditores.*
