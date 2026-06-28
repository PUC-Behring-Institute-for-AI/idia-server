# Self-Hosted Inference Server: vLLM + Ray Serve + LiteLLM
### Architecture, Deployment, Elastic Scaling, Cost, and Operations Reference

---

## 1. Scope

This document specifies a self-hosted LLM inference server with **automatic GPU elasticity** and **on-demand model loading**, deployable identically on a local multi-GPU host and on AWS. It is a complete, standalone reference: build artifacts, both deployment targets, client integration, security, monitoring, fine-tuning, cost planning, and troubleshooting.

The system is three tiers:

| Tier | Component | Role |
|---|---|---|
| Gateway | **LiteLLM** | Auth, virtual keys, per-key budgets and rate limits, spend tracking |
| Orchestration | **Ray Serve LLM** | Replica autoscaling (including scale-to-zero), GPU placement, multi-model routing, LoRA multiplexing |
| Engine | **vLLM** | Inference: model weights in VRAM, KV cache, token generation |

The gateway tier exists specifically because this deployment serves **multiple users/applications, each requiring individual budget and rate-limit enforcement**. A single-tenant deployment with no per-user accounting could omit LiteLLM and expose Ray Serve's own OpenAI-compatible ingress directly; that variant is noted where relevant but is not the configuration documented here.

All paths, model names, and credentials below are placeholders.

---

## 2. Glossary

| Term | Definition |
|---|---|
| **vLLM** | Open-source inference engine. Loads model weights into GPU memory and exposes an OpenAI-compatible HTTP API. |
| **PagedAttention** | vLLM's memory-management algorithm. Partitions the KV cache into fixed-size, non-contiguous blocks — analogous to OS virtual-memory paging — eliminating memory fragmentation. |
| **KV Cache** | Per-token key/value tensors cached during autoregressive decoding to avoid recomputing attention over the full sequence each step. The dominant consumer of GPU memory at serving time. |
| **Continuous Batching** | Scheduling strategy where new requests join an in-flight batch as soon as a GPU memory slot frees up, rather than waiting for a fixed batch window. Primary driver of vLLM's throughput and the basis of multi-tenant cost efficiency (§14). |
| **Ray / Ray Core** | Distributed compute framework. Schedules Python tasks and stateful actors across a pool of machines (a "cluster"), abstracting away which physical node/GPU executes the work. |
| **Ray Cluster** | One head node plus zero or more worker nodes, managed as a single logical pool of CPU/GPU/memory that Ray schedules onto. |
| **Ray Actor** | A stateful Python process Ray schedules onto cluster resources. Ray Serve replicas are actors under the hood. |
| **Ray (Cluster) Autoscaler** | Cluster-level autoscaler. Provisions or terminates entire cloud VMs (nodes) based on aggregate demand across the whole cluster — this is what adds *physical GPU capacity*. |
| **Ray Serve** | Ray's model-serving library. Deploys "Deployments" (a named group of replicas) behind an HTTP ingress. |
| **Ray Serve (Application) Autoscaler** | Application-level autoscaler, distinct from the cluster autoscaler. Adds or removes *replicas of one specific deployment* based on that deployment's own in-flight request load — this adds *logical capacity for one model*, independent of whether new physical nodes are needed. |
| **Ray Serve LLM** | Purpose-built Ray Serve module for LLM serving. Wraps an inference engine (vLLM or SGLang) and adds multi-model routing, autoscaling, and multiplexing. |
| **LLMConfig / ModelLoadingConfig** | Ray Serve LLM's configuration objects describing a model's source, engine arguments, and `autoscaling_config`. |
| **Model Multiplexing** | Serving multiple model variants (typically LoRA adapters sharing one base model) from a shared replica pool, swapping the active variant per request, with **LRU eviction** when GPU memory is needed for a different variant. The mechanism behind "load on demand, drop the least-recently-used when full." |
| **Scale-to-Zero** | `min_replicas: 0` in a deployment's autoscaling config. The deployment holds zero resident replicas while idle, freeing its GPU entirely. |
| **Cold Start** | The latency of provisioning a new replica and loading model weights into VRAM, paid by the first request after scaling up from zero (or after a new node joins the cluster). |
| **Duty Cycle** | The fraction of wall-clock time a deployment actually has a replica running. Under scale-to-zero, cost is proportional to duty cycle, not to the clock (§14). |
| **KubeRay** | Kubernetes operator that manages Ray clusters as native k8s resources (`RayCluster`/`RayService` CRDs). The path taken if Ray standalone is outgrown. |
| **OpenAI-compatible API** | An HTTP interface implementing OpenAI's `/v1/chat/completions` schema. Any client built on the OpenAI SDK works unmodified. |
| **LiteLLM** | Open-source AI gateway/proxy. Issues scoped virtual keys, tracks spend, enforces budgets/rate-limits, and can unify self-hosted backends with commercial provider APIs under one endpoint. |
| **Master Key** | LiteLLM's admin credential, used to issue virtual keys. Never distributed to end clients. |
| **Virtual Key** | A LiteLLM-issued credential scoped to a budget, rate limit, and/or model-access policy — what end clients receive. |
| **Tensor Parallelism (TP)** | Splitting a model's weight matrices across multiple GPUs so one forward pass spans devices. Used when a model does not fit on one GPU — a *fit* problem, not a *throughput* problem. |
| **LoRA / QLoRA** | Parameter-efficient fine-tuning. Freezes base weights `W`, trains small low-rank matrices `A`,`B` such that `y = xW + xAB`. QLoRA does this on a 4-bit quantized base. |
| **ShadowRay** | Real-world attack campaign (active since 2023, resurgent in 2026 as "ShadowRay 2.0") exploiting unauthenticated, publicly exposed Ray Dashboard/Jobs API instances for remote code execution and cryptomining. See §9.2. |
| **AWS DLC (Deep Learning Containers)** | AWS-maintained, pre-optimized Docker images for ML frameworks, including vLLM, ready for EC2/ECS/EKS/SageMaker. |
| **Fargate** | AWS's serverless container compute. **Does not support GPU** — rules out the simplest "serverless container" path for any GPU tier of this stack. |

---

## 3. Architecture & Mechanism

### 3.1 Tier responsibilities

| Tier | Owns | Does NOT own |
|---|---|---|
| **LiteLLM** | Auth, virtual keys, per-key budget/rate-limit, spend tracking, optional unification with commercial APIs | GPU placement, autoscaling, model loading |
| **Ray Serve LLM** | Replica autoscaling (incl. scale-to-zero), GPU-aware placement, multi-model routing, LoRA multiplexing/eviction | Per-user auth/budget, external providers |
| **vLLM** | Inference: weights in VRAM, KV cache, token generation | Everything above — it is a single-model engine with no concept of users, keys, or other models |

```
Client (app / script / curl)
        │  HTTPS, OpenAI request format, virtual key
        ▼
┌─────────────────────────┐
│  LiteLLM      (:4000)    │  CPU only — auth, budget, rate-limit, spend tracking
└──────────┬───────────────┘
           │ internal network only — never exposed externally
           ▼
┌─────────────────────────┐
│  Ray Serve LLM (:8000)   │  Autoscaling, GPU placement, multi-model/LoRA routing
│  - replica autoscaler    │
│  - model multiplexer     │
└──────────┬───────────────┘
           │ in-process / same-node GPU scheduling
           ▼
┌─────────────────────────┐
│  vLLM engine instance(s) │  GPU — model weights + KV cache, per replica
└─────────────────────────┘
```

The client always addresses `:4000`. The host behind that address — laptop, single EC2 instance, or an autoscaling cluster — is invisible to the client. This is the property that makes local and cloud deployment identical from the consumer's side, and it is the reason the gateway is the outermost tier.

### 3.2 The two autoscalers

The system has two independent autoscaling loops operating at different granularities. Conflating them is the most common source of confusion when reasoning about capacity and cost.

| | Ray Serve Autoscaler | Ray (Cluster) Autoscaler |
|---|---|---|
| **Scope** | One deployment (one model) | The whole cluster (all nodes) |
| **Adds/removes** | Replicas (processes) | Nodes (VMs) |
| **Trigger** | `target_ongoing_requests` exceeded for that deployment | Aggregate resource demand exceeds what current nodes provide |
| **Where configured** | `autoscaling_config` inside each `LLMConfig` | `min_workers`/`max_workers` in `cluster.yaml` (AWS-only; inert on a fixed local box) |
| **Answers** | "Do I need another copy of this model running?" | "Do I need another physical GPU machine at all?" |

On a single local multi-GPU host, only the **replica** autoscaler is active — there is no second node to add. On AWS via the Ray Cluster Launcher, **both** operate in sequence: Ray Serve decides it needs another replica → if no GPU slot is free on existing nodes → the cluster autoscaler requests a new EC2 instance to host it. This chained behavior is the mechanism by which added budget converts to added capacity with no manual code changes (§13).

### 3.3 GPU auto-detection

A single Ray process started with access to all GPUs on a node (`--gpus all` at the container level) automatically detects each GPU as a separate schedulable resource and places replicas onto whichever GPU is free. No per-GPU configuration block, no manual device pinning, no per-device config entry is required. Adding a physical GPU to a host requires only a container restart so Ray re-enumerates devices (on a bare-metal Ray process, GPUs present at boot are picked up on the next `ray start`).

### 3.4 Request lifecycle

1. Client sends a request to LiteLLM with a virtual key.
2. LiteLLM validates the key against its budget/rate-limit policy, resolves `model_name` to a Ray Serve backend URL.
3. LiteLLM forwards to Ray Serve LLM's ingress (`http://ray-head:8000/v1/...`).
4. Ray Serve's `OpenAiIngress` routes to the correct model's deployment. If that deployment is at `min_replicas: 0` and idle, this request triggers a **cold start** (§13.2).
5. The deployment's replica (a vLLM engine instance) admits the request into its running batch (continuous batching) and generates tokens.
6. Tokens stream back: vLLM → Ray Serve → LiteLLM → client.
7. LiteLLM logs cost/latency against that virtual key.

---

## 4. Component Reference

### 4.1 vLLM

The engine runs inside a Ray Serve LLM deployment. Its tuning parameters are supplied through `engine_kwargs` in the `LLMConfig` rather than as standalone CLI flags. Common parameters:

| Parameter | Purpose |
|---|---|
| `dtype` | Weight precision (`bfloat16`, `float16`, `fp8`). Lower precision trades quality for VRAM. |
| `gpu_memory_utilization` | Fraction of GPU memory reserved for weights + KV cache (default 0.9). Lower it when other processes share the GPU. |
| `max_model_len` | Maximum context length served; bounds KV-cache sizing. |
| `tensor_parallel_size` | Number of GPUs to shard one model across. Required only when a model does not fit on one GPU. |
| `quantization` | `awq`, `gptq`, `fp8` — reduces VRAM when a model would not otherwise fit. |

vLLM requires NVIDIA compute capability ≥ 7.0 (V100 and newer).

### 4.2 Ray Serve LLM — configuration anatomy

```python
from ray.serve.llm import LLMConfig, ModelLoadingConfig, build_openai_app
from ray import serve

llm_config = LLMConfig(
    model_loading_config=ModelLoadingConfig(
        model_id="llama-3.1-8b",                          # alias clients/LiteLLM use
        model_source="meta-llama/Llama-3.1-8B-Instruct",
    ),
    engine_kwargs=dict(
        dtype="bfloat16",
        gpu_memory_utilization=0.9,
        max_model_len=8192,
    ),
    deployment_config=dict(
        autoscaling_config=dict(
            min_replicas=1,             # 0 = scale-to-zero; see §13.2
            max_replicas=4,
            target_ongoing_requests=64,
        )
    ),
)
app = build_openai_app({"llm_configs": [llm_config]})
serve.run(app)
```

| `autoscaling_config` field | Meaning |
|---|---|
| `min_replicas` | Floor. `0` enables scale-to-zero with automatic wake-on-request (§13.2). |
| `max_replicas` | Ceiling for this *deployment* (not the cluster). |
| `target_ongoing_requests` | Desired average concurrent requests per replica; the controller scales to keep actual load near this value. |
| `max_ongoing_requests` | Hard cap per replica before requests queue. |

### 4.3 LiteLLM — routing configuration

LiteLLM treats Ray Serve's ingress as a custom OpenAI-compatible provider: the provider token is `openai` (meaning "speak the OpenAI protocol to this base URL"), and everything after the first `/` is the model identifier passed through to the backend.

The configuration is maintained in `config.yaml` at the repository root, rendered with env var substitution at runtime. The master key is injected via `${LITELLM_MASTER_KEY:sk-admin}` (fallback default `sk-admin`).

```yaml
model_list:
  - model_name: llama-3.1-8b
    litellm_params:
      model: openai/llama-3.1-8b        # must match model_id in ModelLoadingConfig
      api_base: http://ray-head:8000/v1 # Ray Serve's ingress, internal network only
      api_key: "placeholder"            # Ray's ingress has no per-request key by default — see §9.3

general_settings:
  master_key: ${LITELLM_MASTER_KEY:sk-admin}
  background_health_checks: true
  health_check_interval: 30
  enable_health_check_routing: true
```

`background_health_checks` lets LiteLLM proactively drop an unreachable backend from its routing pool before a real request hits it — relevant when a model is mid-cold-start or a node is being replaced. The `master_key` placeholder is substituted at runtime by the LiteLLM process itself, which parses `${VAR:default}` syntax natively.

For the full file, see `config.yaml` at the repository root. For client consumption patterns, see §8.

---

## 5. Build Process

### 5.1 Directory layout

```
inference-server/
├── Dockerfile.ray         # builds the Ray Serve LLM image
├── serve_config.yaml      # Ray Serve application config (models, autoscaling)
├── docker-compose.yml     # local / single-EC2 orchestration
├── cluster.yaml           # AWS autoscaling cluster definition (§7.3)
├── config.yaml            # LiteLLM model routing
├── .env                   # secrets, not committed
└── prometheus.yml         # monitoring, §10
```

### 5.2 `Dockerfile.ray`

```dockerfile
FROM rayproject/ray-ml:2.55.0-py311-gpu
RUN pip install --no-cache-dir "ray[serve,llm]==2.55.0" vllm
WORKDIR /app
COPY serve_config.yaml scripts/render_config.py ./
CMD ["python3", "/app/render_config.py"]
```

The `ray-ml` image ships Ray's ML dependencies; the explicit `ray[serve,llm]` install pulls Ray Serve LLM's additional requirements. Pin the Ray version. `2.55.0` installs vLLM `0.18.0` as its bundled engine; verify compatibility before bumping either independently.

The CMD delegates to a Python entrypoint (`render_config.py`, see §5.6) that reads `serve_config.yaml`, substitutes `${VAR}` placeholders from environment variables, writes the rendered YAML to a temp file, and then `exec`s `serve run` — replacing the Python process with Ray Serve without a fork. This substitution is necessary because `serve_config.yaml` is consumed by Ray Serve directly and cannot access shell env vars natively.

### 5.3 `serve_config.yaml`

```yaml
proxy_location: EveryNode
http_options:
  host: 0.0.0.0       # binds inside the container only — never publish this port on the host, §9.3
  port: 8000

applications:
  - name: llms
    import_path: ray.serve.llm:build_openai_app
    route_prefix: "/"
    args:
      llm_configs:
        - model_loading_config:
            model_id: ${MODEL_ID}
            model_source: ${MODEL_SOURCE}
          engine_kwargs:
            dtype: bfloat16
            gpu_memory_utilization: ${GPU_MEMORY_UTILIZATION}
            max_model_len: ${MAX_MODEL_LEN}
          deployment_config:
            autoscaling_config:
              min_replicas: 1
              max_replicas: 4
              target_ongoing_requests: 64
```

Placeholders `${VAR}` are substituted at runtime by `render_config.py` (§5.6).
The env vars that map to each placeholder are documented in §5.5 and in
`.env.example` at the repository root.

### 5.4 `docker-compose.yml`

```yaml
services:
  ray-head:
    build:
      context: .
      dockerfile: Dockerfile.ray
    ipc: host
    shm_size: "4gb"
    volumes:
      - ~/.cache/huggingface:/root/.cache/huggingface
    environment:
      - HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all              # all GPUs on the host — Ray distributes replicas across them itself
              capabilities: [gpu]
    restart: unless-stopped
    # Deliberately no "ports:" mapping — dashboard (8265) and ingress (8000)
    # stay on the internal Compose network only. See §9.3.

  litellm:
    image: docker.litellm.ai/berriai/litellm:v1.85.0
    depends_on:
      - ray-head
    ports:
      - "4000:4000"                  # the only port exposed to the host
    volumes:
      - ./config.yaml:/app/config.yaml
    environment:
      - LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}
    command: ["--config=/app/config.yaml"]
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:v2.55.0
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    deploy:
      resources:
        limits:
          memory: "1g"               # prevent unbounded /prometheus growth
    restart: unless-stopped
    # NO ports: published — Prometheus is queried by Grafana on internal
    # Compose network. For admin access: docker compose exec prometheus sh.

  grafana:
    image: grafana/grafana:11.4.0
    depends_on:
      - prometheus
    ports:
      - "127.0.0.1:3000:3000"       # localhost only — no external access
    volumes:
      - ./grafana/datasources:/etc/grafana/provisioning/datasources
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
      - grafana_data:/var/lib/grafana
    restart: unless-stopped

volumes:
  prometheus_data:
    name: idia_prometheus_data
  grafana_data:
    name: idia_grafana_data
```

### 5.5 `.env`

See `.env.example` at the repository root for the full documented template.
Only `.env` (without `.example`) contains secrets and is never committed.

```
HF_TOKEN=hf_xxx
LITELLM_MASTER_KEY=sk-litellm-admin-change-me
MODEL_ID=llama-3.1-8b
MODEL_SOURCE=meta-llama/Llama-3.1-8B-Instruct
MAX_MODEL_LEN=8192          # optional — see defaults below
GPU_MEMORY_UTILIZATION=0.9  # optional — see defaults below
```

**Variable reference:**

| Variable | Required | Type | Default | Used by |
|----------|----------|------|---------|---------|
| `HF_TOKEN` | Yes | str | — | `Dockerfile.ray` → HuggingFace Hub |
| `LITELLM_MASTER_KEY` | Yes | str | — | `config.yaml` (LiteLLM) |
| `MODEL_ID` | Yes | str | — | `serve_config.yaml` (Ray) |
| `MODEL_SOURCE` | Yes | str | — | `serve_config.yaml` (Ray) |
| `MAX_MODEL_LEN` | No | int | 8192 | `serve_config.yaml` (vLLM engine_kwargs) |
| `GPU_MEMORY_UTILIZATION` | No | float | 0.9 | `serve_config.yaml` (vLLM engine_kwargs) |

The template YAML (`serve_config.yaml`) uses `${VAR}` placeholders; the
Python entrypoint (§5.6) substitutes them at container startup. LiteLLM
parses `${VAR:default}` internally — both use the same convention but with
different substitution engines.

### 5.6 Entrypoint script — `scripts/render_config.py`

The Docker CMD in `Dockerfile.ray` (§5.2) does not call `serve` directly.
Instead it launches a Python entrypoint that performs env var substitution
on `serve_config.yaml` before delegating to Ray Serve.

**Why a Python entrypoint instead of `envsubst` or shell?**

| Approach | Mechanism | Dependencies | Error handling |
|----------|-----------|-------------|----------------|
| Shell `envsubst` | `gettext-base` + `envsubst` | Must `apt-get install` in image | Silent — unknown placeholders passed through as literals |
| **Python (chosen)** | `yaml.safe_load` + `re.sub` + `os.execlp` | Python + PyYAML (both already in `ray-ml`) | Explicit: missing required vars → exit 1; invalid YAML → exit 1 |

**Behavior:**

1. Locate `serve_config.yaml` (searches script directory then `/app`).
2. Read template with `${VAR}` placeholders.
3. Collect environment: required vars (`MODEL_ID`, `MODEL_SOURCE`) must be set;
   optional vars (`MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION`) get defaults.
4. Substitute placeholders using regex `\$\{(\w+)\}`.
5. Validate rendered YAML: parse with `yaml.safe_load`, verify structural
   keys (`applications`, `llm_configs`, `model_loading_config` with non-empty
   `model_id` and `model_source`).
6. Write rendered YAML to a temp file.
7. `exec serve run` on the temp file (replaces the Python process).

**Testing hook:** the module exposes a `render()` pure function and a
`--dry-run` CLI flag that prints the rendered YAML to stdout without
launching Ray Serve — used by `tests/test_integration.py`.

---

## 6. Local Deployment

### 6.1 Prerequisites

- NVIDIA driver matching the GPU(s).
- NVIDIA Container Toolkit configured as the Docker runtime (`nvidia-ctk runtime configure --runtime=docker`).
- Docker Engine with Compose v2 (`docker compose`, not the legacy `docker-compose` binary).
- Disk for model weights under `~/.cache/huggingface` (an 8B FP16 model is ~16 GB).
- A single multi-GPU host is sufficient — only the replica autoscaler (§3.2) is exercisable locally; node autoscaling requires a cloud provider.

### 6.2 Steps

```bash
mkdir inference-server && cd inference-server
# place all files from §5
docker compose up -d
docker compose logs -f ray-head     # watch model load; first run downloads weights + builds image
```

### 6.3 Verification

```bash
# Confirm Ray sees all GPUs on the host:
docker compose exec ray-head ray status

# End-to-end request through the full stack:
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.1-8b","messages":[{"role":"user","content":"ping"}]}'
```

### 6.4 Local-specific considerations

- **Adding a GPU**: install the card, confirm with `nvidia-smi` on the host, `docker compose restart ray-head`. No file edit required — Ray re-enumerates devices on restart (§3.3).
- **No node-level autoscaler locally**: the cluster autoscaler (§3.2) never activates on a fixed box; capacity is bounded by the physical GPUs in the machine.
- **Power/thermal**: sustained inference behaves like sustained training for thermal purposes; verify airflow for multi-hour runs.
- **Dashboard access**: do not map port 8265 to the host. Use `docker compose exec -it ray-head bash` and curl `localhost:8265` from inside the container, or a temporary `ssh -L` tunnel from a machine on the same private network (§9.2).

---

## 7. AWS Deployment

### 7.1 Path decision table

| Path | Node-level autoscaling? | Setup effort | Best fit |
|---|---|---|---|
| **EC2, single instance, Docker Compose** | No (manual instance resize only) | Lowest | Literal reuse of §5–§6; no elasticity beyond the instance's own GPU count |
| **Ray Cluster Launcher (`ray up`)** | **Yes** — the cluster autoscaler provisions/terminates EC2 instances directly | Medium | Automatic physical GPU elasticity without Kubernetes — the default for this stack |
| **KubeRay on EKS** | Yes, via k8s-native scheduling | Highest | Already running Kubernetes, or need multi-team GPU sharing |

### 7.2 EC2 + Compose — single-instance deployment

This path deploys the same stack from §5 and §6 on a single GPU EC2 instance,
with no node-level autoscaling. It is the lowest-effort AWS option, suitable
for evaluation, development, or fixed-capacity production workloads.

**Prerequisites (on the EC2 instance):**
- NVIDIA driver matching the GPU(s) (`nvidia-smi` must work)
- NVIDIA Container Toolkit (`nvidia-ctk runtime configure --runtime=docker`)
- Docker Engine with Compose v2 (`docker compose`)

**Deployment steps:**

```bash
# 1. Launch an EC2 GPU instance (e.g. g5.xlarge, Ubuntu 22.04 or later)
#    Security group: open inbound TCP 4000 from your IP/network only.
#    NEVER open 8000, 8265, or 10001.

# 2. SSH into the instance and install prerequisites
sudo apt-get update
sudo apt-get install -y nvidia-driver-545-server     # version depends on GPU
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 3. Copy the project to the instance (rsync recommended)
rsync -avz --exclude '.git' --exclude '.env' \
  ./idia-server/ ubuntu@<ec2-ip>:/home/ubuntu/idia-server/

# 4. SSH to the instance and deploy
ssh ubuntu@<ec2-ip>
cd ~/idia-server
cp .env.example .env
# Edit .env with real values (HF_TOKEN, LITELLM_MASTER_KEY, etc.)
nano .env

# 5. Start the stack
docker compose up -d

# 6. Monitor model loading
docker compose logs -f ray-head

# 7. Verify
docker compose exec ray-head ray status
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $(grep LITELLM_MASTER_KEY .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.1-8b","messages":[{"role":"user","content":"ping"}]}'
```

**Security group rules:**

| Direction | Protocol | Port | Source | Purpose |
|-----------|----------|------|--------|---------|
| Inbound | TCP | 4000 | Your IP / VPN CIDR | LiteLLM API — the only endpoint clients need |
| Inbound | TCP | 22 | Your IP / VPN CIDR | SSH access (use Session Manager if available) |
| Outbound | All | All | 0.0.0.0/0 | For HuggingFace downloads, AWS API calls |
| Inbound | All | 8000, 8265, 10001 | **DENY** | Must never be reachable externally — see §9 |

**Operational notes:**
- Capacity is bounded by the instance's GPU count. To scale up, stop the
  instance, change its type (e.g. g5.xlarge → g5.24xlarge), and restart.
- This path has no node-level autoscaling (§3.2). Add the Cluster Launcher
  (§7.3) when GPU elasticity is needed.
- The same `docker-compose.yml` files work here as on a local machine —
  no changes needed. This is the "implantação idêntica" property.

### 7.3 Ray Cluster Launcher — automatic physical GPU elasticity

This path deploys the IDIA Server across a Ray cluster on EC2, with the
cluster autoscaler automatically provisioning and terminating GPU instances
based on demand. It is the recommended production deployment target for
this stack.

**Prerequisites:**
- `ray[default]` installed locally (`pip install "ray[default]"`)
- AWS credentials configured (`aws configure`)
- A service-quota increase for the target GPU instance type (e.g. g5.xlarge)
  in the chosen region

**Configuration file:** `cluster.yaml` at the repository root.

```yaml
cluster_name: inference-cluster
min_workers: 0
max_workers: 4

provider:
  type: aws
  region: us-east-1

docker:
  image: "rayproject/ray-ml:2.55.0-py311-gpu"     # pinned — no :latest
  container_name: "ray_container"

available_node_types:
  head_node:
    # CPU-only — runs Ray control plane, never holds model weights
    node_config:
      InstanceType: m5.large
    resources:
      CPU: 2
  gpu_worker:
    # GPU node — runs inference replicas; autoscaled 0→4
    min_workers: 0
    max_workers: 4
    node_config:
      InstanceType: g5.xlarge           # 1× A10G 24GB — 7-8B models
      BlockDeviceMappings:
        - DeviceName: /dev/sda1
          Ebs:
            VolumeSize: 100
            VolumeType: gp3
    resources: {}

head_node_type: head_node

file_mounts:
  "/app/rendered_config.yaml": "./rendered_config.yaml"

head_setup_commands:
  - pip install --quiet "ray[serve,llm]==2.55.0" vllm

head_start_ray_commands:
  - ray stop
  - ray start --head --port=6379 --dashboard-host=127.0.0.1 \
      --autoscaling-config=~/ray_bootstrap_config.yaml
```

> **Decision record (2026-06-28):** `serve_config.yaml` contains `${VAR}`
> placeholders (Phase 2 design). The Cluster Launcher's `file_mounts`
> copies static files — it does not substitute env vars. Therefore the
> config must be **pre-rendered** locally before `ray up`. Alternatives
> considered: (A) setting env vars via `head_setup_commands` was rejected
> because it would hardcode secrets into `cluster.yaml`; (B) uploading the
> template and running `render_config.py` on the head node added unnecessary
> complexity. Pre-rendering is the simplest approach and reuses the Phase 2
> entrypoint.

**Deployment workflow:**

```bash
# 1. Pre-render the config template (resolves ${VAR} from .env)
python3 scripts/render_config.py --dry-run > rendered_config.yaml

# 2. Launch the cluster
ray up -y cluster.yaml

# 3. Deploy the LLM app on the cluster
ray exec cluster.yaml "serve run /app/rendered_config.yaml"

# 4. Open a SSH tunnel to the dashboard (never expose port 8265)
ray dashboard cluster.yaml
```

**Automated deployment (wrapper script):**

```bash
# Validates .env, pre-renders, runs ray up + ray exec in one step
./scripts/deploy_cluster.sh

# Dry-run mode: validates .env and pre-renders only
./scripts/deploy_cluster.sh --dry-run
```

See `scripts/deploy_cluster.sh` for the complete automation script.

**Architecture:**

The head node is CPU-only (`m5.large`): it runs Ray's control plane — GCS,
dashboard, autoscaler — and never holds model weights, so a GPU on it would
sit idle. Worker nodes (`gpu_worker`) carry the vLLM replicas and scale from
zero.

Adding capacity: raise `max_workers` and re-run `ray up -y cluster.yaml`. The
cluster autoscaler launches additional `g5.xlarge` instances on its own
whenever the replica autoscaler (§3.2) requests more capacity than current
nodes provide, and terminates idle ones automatically — `idle_timeout_minutes`
controls how long an empty node survives before termination.

A service-quota increase for the chosen GPU instance family is a prerequisite;
the autoscaler cannot provision capacity AWS has not approved for the account.

**Security invariants (from §9):**
- Dashboard bound to `127.0.0.1` via `--dashboard-host=127.0.0.1` — mandatory
  mitigation against ShadowRay/CVE-2023-48022.
- Docker image pinned to `rayproject/ray-ml:2.55.0-py311-gpu` — no `:latest`.
- Head node is CPU-only — no GPU declared in its `resources` block.
- Worker nodes use `file_mounts` for configuration, never network-exposed
  management endpoints.

### 7.4 KubeRay / EKS

Justified by multi-team GPU sharing, an existing Kubernetes investment, or need for the broader operator ecosystem (KubeAI, AIBrix, vLLM Production Stack). For a single-tenant inference server, §7.3 delivers the elasticity property without this layer's operational cost. Industry adoption skews this way only at large-organization scale — the majority of single-cluster deployments do not need it.

### 7.5 Instance reference

| Instance family | GPU | Typical fit |
|---|---|---|
| g6.xlarge / g5.xlarge | 1× L4 / A10G (24GB) | 7–8B models; the worker type used above |
| g6.12xlarge | 4× L4 | 13B–34B, or several 7–8B replicas per node |
| p4d.24xlarge | 8× A100 (40GB) | 70B-class with tensor parallelism (sold only as a full 8-GPU node) |
| p5e.48xlarge | 8× H200 (141GB) | Frontier MoE (hundreds of GB of weights); also a full-node-only purchase |

---

## 8. Client Consumption

Clients always target the LiteLLM endpoint, never Ray or vLLM directly. They never observe that autoscaling or cold starts exist.

Issue a virtual key per user/team (never hand out the master key):

```bash
curl -X POST 'http://<host>:4000/key/generate' \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"rpm_limit": 60, "max_budget": 20}'
# → {"key": "sk-12..."}
```

Consume via the OpenAI SDK — no vLLM-, Ray-, or LiteLLM-specific code:

```python
from openai import OpenAI
client = OpenAI(base_url="http://<host>:4000", api_key="sk-12...")
resp = client.chat.completions.create(
    model="llama-3.1-8b",          # = model_name in config.yaml
    messages=[{"role": "user", "content": "Explain PagedAttention in one sentence."}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

A request landing on a scaled-to-zero model pays the cold-start latency (§13.2) inside this same call — there is no separate "wake up" API.

**Failure modes to handle distinctly:**

| Response | Source | Meaning | Remediation |
|---|---|---|---|
| `429` | LiteLLM | Virtual key exceeded its `rpm_limit`/`max_budget` | Back off; raise the limit if legitimate |
| `5xx` after a delay | Ray/vLLM | Engine overloaded or replica unhealthy | Alert; check replica count and queue depth |
| First-request latency in seconds–minutes | Ray | Expected cold start, not a failure | None — distinguish from a regression in dashboards |

---

## 9. Security & Operational Hardening

### 9.1 Baseline controls

- **Pin every image tag** (`ray-ml`, `litellm`, and any standalone `vllm`) — never `:latest`. LiteLLM had a supply-chain incident (compromised PyPI releases during a window in March 2026); pinning to an immutable version tag is the mitigation.
- **Two trust boundaries**: LiteLLM's master key (admin) vs. virtual keys (clients). Neither the master key nor any internal backend credential is ever derivable from a client-facing virtual key.
- **TLS terminates at the edge** (ALB/NLB on AWS, a reverse proxy locally), not inside any container.
- **Only port 4000 is ever reachable externally.** Ray ingress (8000), dashboard (8265), and Client port (10001) stay internal in every deployment target.

### 9.2 [IMPORTANT] Ray Dashboard / Jobs API — a documented, actively exploited risk

Ray's dashboard and Jobs API were **designed without authentication**, on the explicit assumption that the cluster runs inside an already-trusted network. Ray faithfully executes code passed to it and does not distinguish a tuning experiment from a rootkit install or an S3 bucket inspection. Anyone able to reach the associated ports can execute arbitrary code on the cluster. This is not theoretical:

- **CVE-2023-48022** (CVSS 9.8; disputed by Anyscale as "a feature, not a bug") enabled unauthenticated remote code execution via the Jobs API on any internet-reachable Ray dashboard. Researchers found thousands of publicly exposed, compromised Ray servers worldwide — the "ShadowRay" campaign — some compromised for at least seven months.
- A 2026 resurgence ("ShadowRay 2.0") shows the same exposure pattern still exploited at scale, driven by the dashboard's default `0.0.0.0` bind colliding with operators who expose it for convenience.
- **CVE-2026-27482** (fixed in Ray 2.54.0+) allowed unauthenticated denial-of-service via an incomplete browser-request blacklist (it blocked `POST`/`PUT` but not `DELETE`), letting a malicious webpage terminate running Serve applications via DNS rebinding.

**Mandatory mitigations:**

1. Never map the dashboard port (8265), Client port (10001), or Prometheus port (9090) to a host port, on Compose or on the Cluster Launcher. Verify with `docker compose ps` / `docker port` after every deploy.
2. Bind the dashboard to `127.0.0.1` (as in §7.3); reach it remotely only via `ray dashboard cluster.yaml` (SSH tunnel) or a reverse proxy with its own authentication.
3. Ray ≥ 2.52.0 ships built-in token authentication — enable it as a second layer, not a replacement for network isolation.
4. Run Ray ≥ 2.54.0 to close CVE-2026-27482.
5. Treat the cluster like a database with no query authorization: any network path to it is equivalent to root on every node.
6. Grafana (port 3000) is bound to `127.0.0.1` — accessible only from the Docker host, not from external networks.

### 9.3 Ray Serve's ingress has no per-request key

Ray Serve LLM's `OpenAiIngress` does not check a per-request API key by default. This is acceptable **only because** LiteLLM is the sole externally reachable component and Ray's ingress (8000) is never published to the host or public network — the same isolation principle as §9.2. If Ray's ingress is ever exposed directly (e.g. "temporarily" for testing), authentication must be added at a reverse proxy in front of it first.

---

## 10. Monitoring & Observability

### 10.1 What each layer exposes

| Layer | Endpoint | Key signals |
|---|---|---|
| vLLM (inside each replica) | Prometheus format, default-on | `vllm:time_to_first_token_seconds`, `vllm:e2e_request_latency_seconds`, `vllm:gpu_cache_usage_perc`, `vllm:num_preemptions_total`, `vllm:num_requests_waiting` |
| Ray Serve | Built-in Prometheus metrics + Grafana dashboards | replica count per deployment, queue depth, autoscaling events, per-request routing |
| Ray (cluster) | Dashboard (internal-only, §9.2) | node count, GPU utilization per node, autoscaler decisions/logs |
| LiteLLM | Built-in Prometheus integration + spend logs | per-key/team cost, request count, latency, fallback events |

The two most actionable engine signals: `gpu_cache_usage_perc` approaching 1.0 together with rising `num_preemptions_total` means the KV cache is undersized for current load — the engine is evicting and recomputing context, degrading latency before it errors. Ray Serve LLM emits its engine-level metrics through the same Prometheus endpoint as Ray's cluster metrics, so one scrape config covers both.

### 10.2 Prometheus + Grafana (Phase 4)

Implemented as two additional services in `docker-compose.yml`, plus a
provisioned Grafana datasource — no manual configuration needed after
`docker compose up`.

**Prometheus** (`prometheus.yml` at the repository root):

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: ray-serve
    static_configs:
      - targets:
          - "ray-head:8080"      # Ray metrics export port — distinct from
                                 # dashboard (8265) and ingress (8000)
        labels:
          layer: orchestration

  - job_name: litellm
    static_configs:
      - targets:
          - "litellm:4000"       # LiteLLM metrics (/metrics) on the same
                                 # port as the API
        labels:
          layer: gateway
```

Key properties:
- Port 9090 is **not exposed to the host** — Prometheus is queried by
  Grafana on the internal Compose network. For admin access:
  `docker compose exec prometheus sh`.
- Image pinned to `prom/prometheus:v2.55.0` — no `:latest` (§9.1).
- Scrape interval 15s — appropriate for inference servers; engine-level
  metrics (TTFT, cache usage) change at request granularity, not
  sub-second.

**Grafana** with automatic provisioning:

```yaml
grafana:
  image: grafana/grafana:11.4.0
  depends_on:
    - prometheus
  ports:
    - "127.0.0.1:3000:3000"     # localhost only — no external access
  volumes:
    - ./grafana/datasources:/etc/grafana/provisioning/datasources
    - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
    - grafana_data:/var/lib/grafana
```

Key properties:
- Bound to `127.0.0.1` — only the Docker host can access the UI
  (§9.3 mitigation #6).
- Image pinned to `grafana/grafana:11.4.0` — no `:latest` (§9.1).
- **Automatic datasource provisioning**: `grafana/datasources/datasource.yml`
  configures Prometheus as the default datasource pointing to
  `http://prometheus:9090` — no manual setup.
- **Dashboard directory**: place downloaded JSON files from the official
  Ray Serve and vLLM dashboards in `grafana/dashboards/` for automatic
  provisioning. Links: search "Ray" and "vLLM" on
  https://grafana.com/grafana/dashboards/.

**Warning:** The Ray and vLLM dashboard JSONs change between versions.
Do not version them in this repository — download and import the versions
matching `rayproject/ray-ml:2.55.0` and the bundled vLLM version.

**Accessing Grafana:**

```bash
# Set up a tunnel if needed (from your laptop to the host):
ssh -L 3000:localhost:3000 user@host
# Then open:
open http://localhost:3000
# Default credentials: admin / admin (change on first login)
```

### 10.3 Recommended alerts

| Alert | Condition | Why |
|---|---|---|
| KV-cache saturation | `vllm:gpu_cache_usage_perc > 0.95` for 5m | Preemption/recompute imminent |
| Replica ceiling reached | deployment at `max_replicas` for >10m | The `autoscaling_config` ceiling, not GPU capacity, is the bottleneck — raise it or investigate demand |
| Cluster at `max_workers` | autoscaler logs show node count pinned at ceiling | On AWS, bounded by `cluster.yaml`, not by absent hardware |
| Cold-start latency spike | p99 TTFT spikes correlated with a scale-up-from-zero event | Expected; distinguish from a genuine regression |
| Dashboard reachable externally | any external hit on 8265/10001 | Should be impossible per §9.2 — treat as an incident, not a warning |

### 10.4 Request-level tracing

For debugging a specific slow request rather than aggregate trends, vLLM supports OpenTelemetry tracing via `--otlp-traces-endpoint` — complementary to, not a replacement for, the Prometheus path above.

---

## 11. Testing & Validation

Testing an inference server spans four layers, each with distinct
infrastructure requirements and failure modes.

### 11.1 Test categories

| Category | Scope | Infrastructure | When to run | Phase |
|----------|-------|---------------|-------------|-------|
| **docs** | File structure, markdown headers, governance sections | None (`pytest`) | Every commit | 1 |
| **config** | YAML schema validation for every config artifact | PyYAML | Every commit | 1 |
| **integration** | Docker build, `docker compose up`, GPU detection, E2E inference, autoscaling; unit tests for `render_config.py` (env var substitution, YAML validation) | Docker + NVIDIA GPU for full suite; unit component runs with `pytest` only | Before release | 2 |
| **security** | Port isolation (`:8000`, `:8265` unreachable externally), image pinning (no `:latest`), trust boundaries, dashboard binding | YAML/config-level checks run with `pytest` only; network-level checks require Docker | Before release | 2 |

### 11.2 Test location and execution

Tests live in `tests/` and use **pytest 8.x** with shared fixtures
from `tests/conftest.py`. Category markers (`@pytest.mark.docs`,
`@pytest.mark.config`, `@pytest.mark.integration`, `@pytest.mark.security`)
allow selective execution.

```bash
# Quick validation — zero infrastructure required
pip install pytest pyyaml
pytest -m "docs or config" -v

# Full suite (requires Docker + GPU for integration/security tests)
pytest -v
```

### 11.3 Config schema validation

Every YAML artifact in this repository is validated against structural
expectations derived from this architecture document. Tests skip
gracefully when a future-phase file does not yet exist rather than
failing. See `tests/test_config_schemas.py` for the full mapping
between each config file and its assertions.

### 11.4 Skip policy

Tests that depend on files or infrastructure from later phases use
`pytest.skip()` with an explanatory message. This guarantees the
test suite passes cleanly at every phase, even when only a subset of
artifacts exist.

### 11.5 Test files and what they cover

| File | Phase | Marker | Key tests |
|------|-------|--------|-----------|
| `tests/test_docs.py` | 1 | `docs` | Required file existence, markdown headers, governance sections, version footer |
| `tests/test_config_schemas.py` | 1, 4 | `config` | YAML schema validation for `serve_config.yaml`, `docker-compose.yml`, `config.yaml`, `cluster.yaml`, `prometheus.yml`, `.env.example`; Phase 4: extended Prometheus target validation, Grafana datasource provisioning config |
| `tests/test_integration.py` | 2 | `integration` | `render_config.py` env var substitution (required/optional), dry-run mode, error paths; Compose consistency (build source, image pinning, env var propagation) |
| `tests/test_security.py` | 2, 3, 4 | `security` | Port isolation (only 4000 externally accessible), image pinning (no `:latest`), trust boundaries (master key declared), dashboard binding. Phase 3: cluster.yaml security invariants (dashboard bound to 127.0.0.1, pinned image, CPU-only head node). Phase 4: Prometheus port (9090) not published, Grafana bound to 127.0.0.1 |

### 11.6 Simulated integration testing (Mac/non-GPU environments)

Because integration and security tests require Docker + NVIDIA GPU for
full validation, a subset of tests exercise the code paths and config
structures without real infrastructure:

- `TestRenderConfig` calls `render_config.render()` — a pure function that
  substitutes env var placeholders and validates YAML output without
  launching any container.
- `TestRenderConfigErrors` tests error paths (missing required env vars,
  invalid YAML templates) via `--dry-run` flag and direct function calls.
- `TestComposeConsistency` validates `docker-compose.yml` structure
  (build context, image tags, env var lists) by parsing YAML only.
- `TestClusterYaml` (Phase 3) validates the `cluster.yaml` structure,
  including dashboard binding, image pinning, GPU worker scaling config,
  and file_mounts — all by parsing YAML, no cloud credentials needed.
- `TestClusterSecurity` (Phase 3) validates `cluster.yaml` invariants
  from a security perspective: dashboard bound to 127.0.0.1, no `:latest`,
  CPU-only head node — all by inspecting the YAML file content.
- `TestPortIsolation` verifies that only port 4000 is accessible externally
  (127.0.0.1:3000 is permitted for Grafana) by inspecting the YAML, not
  by running containers.
- `TestPrometheusConfig` (Phase 4) validates the `prometheus.yml` structure:
  scrape interval, target addresses (ray-head:8080, litellm:4000), and the
  absence of Prometheus-level alert rules (delegated to Grafana).
- `TestGrafanaDatasourceConfig` (Phase 4) validates the Grafana datasource
  provisioning YAML: URL points to `http://prometheus:9090`, access is
  `proxy`, datasource is Prometheus and set as default.
- `TestMonitoringPortIsolation` (Phase 4) verifies that Prometheus (9090)
  is not published in any `ports:` section and that Grafana (3000) is
  bound to `127.0.0.1` only.

Tests that genuinely require GPU (`docker compose build`, `ray status`,
E2E inference) are documented and intended for pre-release validation on
GPU-equipped hardware.

For the complete testing reference, see `AGENTS.md` (Testing Strategy).

---

## 12. Fine-Tuning & Multi-Model Serving

### 12.1 Why LoRA

`y = xW + xAB` — frozen base `W`, small trained low-rank matrices `A`,`B`. The adapter is typically under 1% of the base model's size, which is what makes both training (consumer-GPU feasible) and serving (many adapters per GPU) practical. Serving 100 rank-16 adapters on one 8B base costs roughly `16 GB + 100 × 0.06 GB` ≈ 22 GB — versus ~1.6 TB if each adapter required a full model copy. That ratio is the entire economic argument for multi-LoRA over per-customer dedicated models.

### 12.2 Training framework choice

| Framework | Strength | Best fit |
|---|---|---|
| **Unsloth** | Fastest, lowest VRAM, single-GPU focus | Limited hardware, fast iteration |
| **Axolotl** | Config-driven, strong multi-GPU support | Production training, team handoff |
| **TRL** | Reference RLHF/GRPO implementation | When the objective itself, not speed, is the hard part |
| **LLaMA-Factory** | GUI-first, broad model coverage | Fastest path for non-specialists |

[DEBATED]: relative speed/VRAM benchmarks vary by source; the directional ranking is consistent, exact multipliers are not. All four emit standard HuggingFace-format adapters, cross-compatible without conversion.

### 12.3 Serving adapters via multiplexing

Ray Serve LLM's model multiplexing loads adapters on demand and evicts them LRU when GPU memory is needed for a different one, without restarting the engine — adapters sharing one base model swap in sub-second time, far cheaper than a full cold start.

[SPECULATIVE — verify field names against current Ray Serve LLM docs before deploying]: the general pattern is to declare a `lora_config` alongside the base `LLMConfig` rather than a flat list of adapter paths. This module evolves between minor versions; confirm the production syntax against Ray's LoRA-serving guide rather than relying on this document.

### 12.4 Exposing a fine-tuned variant

A fine-tuned variant becomes a second entry in `llm_configs` (or a multiplexed adapter under the same base entry) plus a second `model_name` in LiteLLM's `config.yaml` pointing at the same Ray ingress. Clients select it by changing the `model` field — same endpoint, same key, same SDK call shape as §8.

---

## 13. Scaling

### 13.1 The two levers

- **More concurrent capacity for a model already running** → raise `max_replicas` in that model's `autoscaling_config` (§4.2). No infrastructure change.
- **More physical GPU capacity** → locally: install hardware, restart the container (§6.4). On AWS: raise `max_workers` in `cluster.yaml` (§7.3); the cluster autoscaler provisions instances automatically once replica demand exceeds current node capacity.

### 13.2 Scale-to-zero and automatic wake-on-request

`min_replicas: 0` frees a model's GPU allocation entirely while idle. The mechanism is fully automatic: the first request after an idle period triggers Ray Serve to provision a replica and load the model into VRAM, then serves that request once loading completes — no manual restart, no standing always-on cost, no separate "wake up" call. This applies uniformly regardless of model size; there is no flag that disables it for larger deployments.

The only cost is cold-start duration on that first request, which scales with how much weight must load and across how many GPUs — seconds for a single-GPU model, minutes for a model spanning a full multi-GPU node. Whether that wait is acceptable is a product decision, not a constraint the system imposes: if idle-GPU savings matter more than first-request latency, `min_replicas: 0` is correct for any deployment, including multi-GPU ones.

To enable: set `min_replicas: 0` in the deployment's `autoscaling_config`. No other configuration changes with model size.

### 13.3 On-demand model loading — two distinct cases

| Case | Mechanism | Where configured |
|---|---|---|
| Same model, more concurrent capacity | Replica autoscaler adds copies of the *same* deployment | `autoscaling_config` |
| Different model/variant not currently resident | Separate `LLMConfig` with its own `min_replicas: 0` (full cold start), or LoRA multiplexing with LRU eviction (sub-second swap between adapters sharing one base) | `llm_configs` list / multiplexing config (§12.3) |

### 13.4 Honest limits

- Capacity is bounded by `max_replicas` per model and `max_workers` per cluster. These are deliberately finite — operator-chosen guardrails against runaway cost, not limitations to remove.
- AWS instance provisioning has boot lag (minutes). The cluster autoscaler is automatic, not instantaneous, and is distinct from the replica autoscaler, which reacts faster because it reuses already-running nodes when capacity exists.
- The GPU service-quota prerequisite (§7.3) always applies — the autoscaler cannot provision instance types AWS has not approved for the account.

### 13.5 The zero-usage floor cost

Scale-to-zero removes GPU cost while a deployment is idle, but not all cost. Two things persist regardless of traffic:

1. **The head node.** Something must stay listening to receive the first request and trigger a cold start. The head node is not part of the autoscaled `gpu_worker` pool and never scales to zero. It runs only Ray's control plane and holds no model weights, so it is CPU-only (`m5.large` in §7.3) — a GPU there would be wasted on idle coordination.
2. **Persisted model storage.** Weights cached on EBS so a cold start reads from local disk instead of re-downloading from HuggingFace on every wake-up. Billed by GB-month whether or not the model is ever loaded.

| Component | Cost driver | Approx. monthly cost |
|---|---|---|
| Head node (`m5.large`, CPU-only, always on) | $0.096/hr × 730h | ~$70 |
| Model storage (EBS gp3, $0.08/GB-month), per cached model | weight size on disk | ~$2 for a 24GB model, up to ~$60 for a 750GB-class MoE |
| GPU workers | `min_workers: 0`, no replica running | $0 |

The floor with zero requests, ever, is the head node plus staged model storage — on the order of $70–140/month depending on how many model sizes are pre-cached, never $0. This is the price of being ready to respond instantly to the next request, not the price of serving anyone.

---

## 14. Cost & Capacity Planning

All figures are directional and in USD; verify against the AWS console before budgeting. Two inputs drive everything: which **model class** is served (it sets the per-replica hardware) and how many **concurrent requests** must be handled at peak (it sets the replica count).

### 14.1 Model classes

The parameter count, not the model name, determines cost. Three classes bracket the realistic range of open-weight coding models:

| Class | Anchor (community open-weight, mid-2026) | Hardware shape | Why |
|---|---|---|---|
| **Small** | 24B-class dense (e.g. Devstral-Small-2) | 1 GPU (g6.xlarge) | Fits one 24GB card in FP8; the most accessible self-hosted tier |
| **Medium** | ~70B dense | slice of an 8×A100 node (p4d.24xlarge) | Needs >1 GPU or aggressive quantization; A100/H100 sold only as full 8-GPU nodes |
| **Large** | 700B+ MoE (e.g. GLM-class, ~40B active) | full 8×H200 node (p5e.48xlarge) | Hundreds of GB of weights; fits only across a whole node |

[ESTABLISHED]: these model/hardware classes and AWS instance prices are confirmed by multiple current sources. [SPECULATIVE]: the concurrent-requests-per-replica figures below are estimates (throughput ÷ per-user demand ÷ duty cycle), not published benchmarks for these specific models — use them as a starting point to validate against real load, not as final numbers.

### 14.2 Per-replica unit economics

| Class | Instance | VRAM need | Concurrent req/replica [SPECULATIVE] | $/hr | $/mo at 24/7 |
|---|---|---|---|---|---|
| Small | g6.xlarge (1× L4 24GB) | ~24GB FP8 | ~18 | $0.80 | ~$587 |
| Medium | 1/8 of p4d.24xlarge (1× A100 40GB) | ~35GB INT4 | ~30 | $4.10 equiv. | ~$2,990 equiv. |
| Large | full p5e.48xlarge (8× H200) | ~754GB FP8 | ~200 | $47.76 | ~$34,865 |

The Medium and Large rows expose a **step-function cost shape**: A100/H100/H200 are sold only as full 8-GPU nodes. You cannot rent "one-eighth of a p4d" — the node bills whole whether one GPU or all eight are in use. Small does not have this problem (g6.xlarge is one purchasable GPU), so it scales smoothly.

### 14.3 Monthly cost vs. concurrent users (24/7, always-on)

| Peak concurrent | Small (nodes / $) | Medium (full p4d nodes / $) | Large (full p5e nodes / $) |
|---|---|---|---|
| 10 | 1 / $587 | 1 / $23,902 | 1 / $34,865 |
| 50 | 3 / $1,761 | 1 / $23,902 | 1 / $34,865 |
| 200 | 12 / $7,044 | 1 (at capacity) / $23,902 | 1 (at capacity) / $34,865 |
| 1,000 | 56 / $32,872 | 5 / $119,510 | 5 / $174,325 |

Reading the table:
- **Large has a brutal fixed floor** ($34,865/mo even for 10 users) because the MoE needs a whole 8-GPU node just to load. It is only defensible if its per-task quality justifies ~5–7× Small's per-user cost at every scale.
- **Medium is worst at small scale** ($23,902/mo for 10 users): paying for a full 8×A100 node to serve a handful of people is the least efficient point in the matrix. It becomes competitive only once the node fills (~200 concurrent), because node cost is fixed whether 1 or 8 GPUs are busy.
- **Small scales almost linearly** — no node step function — and converges to ~$5/user/mo at any reasonable scale.

### 14.4 Cost per registered user (assuming 15% peak concurrency)

Peak concurrency ≈ 15% of registered users is a reasonable placeholder for an internal dev tool — substitute real telemetry (§10) when available.

| Peak concurrent | ≈ registered users | Small | Medium | Large |
|---|---|---|---|---|
| 10 | ~67 | $8.76 | $356.75 | $520.37 |
| 50 | ~333 | $5.29 | $71.78 | $104.70 |
| 200 | ~1,333 | $5.28 | $17.93 | $26.16 |
| 1,000 | ~6,667 | $4.93 | $17.93 | $26.15 |

Per-user cost falls with scale in every class, but the gap between classes does not close: Large stays ~5× Small per user even at 1,000 concurrent.

### 14.5 The scale-to-zero effect — paying only for the hours in use

The tables above assume always-on. Under scale-to-zero (§13.2), cost is proportional to **duty cycle** — the fraction of time a replica is actually up — plus the fixed floor (§13.5). The key dynamic: the more users, the less this saves, because with enough traffic something is almost always running, collapsing back toward always-on.

| Peak concurrent | Approx. duty cycle | Small | Medium | Large |
|---|---|---|---|---|
| 10 | ~25% | ~$150 + floor | ~$6,000 + floor | ~$8,700 + floor |
| 50 | ~50% | ~$880 + floor | ~$12,000 + floor | ~$17,400 + floor |
| 200 | ~80% | ~$5,600 + floor | ~$19,100 + floor | ~$27,900 + floor |
| 1,000 | ~98% | ~$32,200 + floor | ~$117,000 + floor | ~$170,800 + floor |

Duty-cycle percentages are placeholders for spread-out-but-bursty usage; replace with measured data. "+ floor" is the §13.5 zero-usage cost (~$70–140/mo), which does not disappear.

Implication: scale-to-zero is most valuable at low, sparse utilization — exactly where always-on is most wasteful. At high utilization it converges to the §14.3 numbers. For the Large class, sparse usage saves the most dollars but pays the longest cold start (minutes) on the first request after idle; whether that latency is acceptable is the §13.2 product decision.

---

## 15. Troubleshooting Reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `ray status` shows 0 GPUs in the container | NVIDIA Container Toolkit misconfigured, or `count: all` omitted from the Compose `deploy` block | Verify with `docker compose exec ray-head nvidia-smi` first |
| LiteLLM `502`/timeout reaching Ray | Wrong `api_base` — `localhost` instead of the service name `ray-head` | Use `http://ray-head:8000/v1` |
| First request after deploy hangs for minutes | Expected cold start (§13.2) downloading weights, not a hang | Watch `docker compose logs -f ray-head` for download progress |
| `prometheus.yml: not a directory` on `up` | File didn't exist before first run; Docker auto-created a directory | `rm -rf prometheus.yml`, create the actual file, retry |
| Slow under load, no errors | KV-cache thrashing (§10.1) | Check `gpu_cache_usage_perc`/preemptions; lower `max_model_len` or add capacity |
| Replica count stuck at `max_replicas` | Ceiling reached, not a bug (§10.3) | Raise `max_replicas`, or `max_workers` if the cluster is also full |
| External port scanner hits on 8265 | Dashboard leaked to a public interface — active incident, not a slow-fix misconfiguration | Drop the published port immediately, rotate credentials Ray could reach, review §9.2 |
| `429` from LiteLLM | Virtual key budget/RPM hit | Expected; raise the limit if legitimate, else investigate the caller |
| LoRA request returns base-model output | Multiplexing config not matching the adapter's declared name | Confirm against current Ray Serve LLM docs (§12.3) — config surface changes between minor versions |

---

## 16. Document Evolution Contract

### 16.1 Principles

1. ARCHITECTURE.md and the code evolve together — never one without the other.
2. Code is the source of truth; the document is the map. If they disagree, code
   prevails, but the document must be corrected immediately.
3. Every structural change must be reflected in the document before merge.

### 16.2 SYNC-REQUIRED triggers

A change in any of the following *requires* an update to this document:

- `Dockerfile.ray` — base image, dependencies, entrypoint
- `serve_config.yaml` — models, autoscaling, `engine_kwargs`
- `docker-compose.yml` — services, ports, networks, volumes, GPU config
- `cluster.yaml` — node types, IAM, autoscaling limits
- `config.yaml` — LiteLLM routing, model list, health checks
- `prometheus.yml` — scrape targets, alert rules
- Any test file in `tests/` that introduces a new test category (docs, config,
  integration, security)
- Port mappings, network topology, security perimeters
- Model loading strategy or GPU placement logic

### 16.3 Minor update

Version bump, parameter tweak, new env var:
- Edit the affected section only.
- No full review required.
- Update the footer with date and sections changed.

### 16.4 Major update

New tier, new deployment target, architectural pattern change:
- Full document review required.
- Superseded sections marked `[DEPRECATED — see section X]`.
- Requires human approval before merge.

### 16.5 Desync prevention

- Never merge code without its corresponding update to this document.
- Every implementation task that affects the architecture declares:
  `[UPDATES ARCHITECTURE.md — section X]` in its plan.
- If code and ARCHITECTURE.md disagree, the document is updated in the same
  PR/commit — never deferred.

### 16.6 ADR.md — Decision Records

As decisões arquiteturais mais importantes são registradas em
[`docs/ADR.md`](ADR.md) com o formato:
- **ADR-[N]**: título, data, fase de origem, status
- **Contexto**: problema que motivou a decisão
- **Decisão**: o que foi decidido e por que
- **Alternativa descartada**: opção(ões) rejeitada(s) e justificativa
- **Consequências**: efeitos colaterais (positivos e negativos)

A criação de um novo ADR é obrigatória quando uma decisão arquitetural
envolve trade-offs significativos entre múltiplas alternativas viáveis.

### 16.7 Structural Change History

| Date | Change | Reason |
|------|--------|--------|
| 2026-06-28 | Document created | Initial architecture specification |
| 2026-06-28 | Added §11 Testing & Validation; renumbered §11–§15 to §12–§16; added §16 Document Evolution Contract | Living document governance + test suite |
| 2026-06-28 | Phase 2: Added entrypoint script (render_config.py), expanded .env vars with table, updated Dockerfile CMD, serve_config placeholders, LiteLLM config, integration/security tests, new §5.6 Entrypoint Script | Build Core implementation |
| 2026-06-28 | Phase 3: Updated §7.3 (pre-render workflow for cluster.yaml — decision record), expanded §7.2 (step-by-step EC2 + Compose guide with security group table), added §7.3 deploy automation script reference, extended test tables in §11 with Phase 3 tests (TestClusterYaml extended, TestClusterSecurity); new Governance & Maintainability Axioms in AGENTS.md (Decision Closure, Architecture Feedback Loop, Traceability Axiom) | AWS Deployment implementation |
| 2026-06-28 | Phase 5: Updated §16 (added §16.6 ADR.md decision records); created `docs/ADR.md` with 8 ADRs (phases 1-5); updated `LICENSE` (Apache 2.0); added cross-doc consistency tests (TestReadmeDirectoryTree, TestADRValidation); updated §18 footer | Final Documentation — revision + handoff |

---

## 17. References

- vLLM — Docker deployment & metrics: https://docs.vllm.ai/en/stable/deployment/docker/, https://docs.vllm.ai/en/stable/design/metrics/
- vLLM — Parallelism & scaling: https://docs.vllm.ai/en/latest/serving/parallelism_scaling/
- Ray Serve LLM — architecture & serving guide: https://docs.ray.io/en/latest/serve/llm/index.html, https://docs.ray.io/en/latest/serve/llm/architecture/overview.html
- Ray — KubeRay LLM example (`autoscaling_config` reference): https://docs.ray.io/en/latest/cluster/kubernetes/examples/rayserve-llm-example.html
- Ray — Cluster YAML / AWS autoscaler reference: https://docs.ray.io/en/latest/cluster/vms/references/ray-cluster-configuration.html, https://github.com/ray-project/ray/blob/master/python/ray/autoscaler/aws/example-full.yaml
- Ray — Security guide & token authentication (2.52.0+): https://docs.ray.io/en/latest/ray-security/index.html
- ShadowRay / CVE-2023-48022: https://www.oligo.security/blog/shadowray-attack-ai-workloads-actively-exploited-in-the-wild, https://www.penligent.ai/hackinglabs/the-zombie-vulnerability-a-2026-autopsy-of-cve-2023-48022-and-the-shadowray-2-0-resurgence/
- CVE-2026-27482 (dashboard DELETE bypass): https://www.sentinelone.com/vulnerability-database/cve-2026-27482/
- LiteLLM — Docker quickstart, routing/load balancing, health-check routing: https://docs.litellm.ai/docs/proxy/docker_quick_start, https://docs.litellm.ai/docs/proxy/load_balancing, https://docs.litellm.ai/docs/proxy/health_check_routing
- Fine-tuning framework comparison: https://dev.to/ultraduneai/eval-003-fine-tuning-in-2026-axolotl-vs-unsloth-vs-trl-vs-llama-factory-2ohg
- AWS — EC2 GPU & general-purpose pricing: https://aws.amazon.com/ec2/pricing/on-demand/, https://aws.amazon.com/ebs/pricing/
- Kubernetes/AI adoption context (for §7.4): CNCF Annual Cloud Native Survey 2025 (published Jan 2026), https://www.cncf.io/announcements/2026/01/20/kubernetes-established-as-the-de-facto-operating-system-for-ai-as-production-use-hits-82-in-2025-cncf-annual-cloud-native-survey/

---
*Document version: 1.5 | Last updated: 2026-06-28 | Sections changed: §16.6 (added ADR.md — decision records), §17 (added ADR.md reference), §18 footer updated; LICENSE (Apache 2.0)*