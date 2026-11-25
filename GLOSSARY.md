# Fleet

A volunteer-computing style job system: a **Broker** hands out **Jobs** to **Workers** running on many
**Backends**, so GPU work (first, Metaphor's cutscene descriptions) runs wherever compute is free.

## Language

### Work

**Job**:
One unit of work the **Broker** hands out: an input, a model and a prompt version, plus an optional **Context**.
_Avoid_: Task, message

**Context**:
Everything besides the input that the prompt depends on, such as Metaphor's story bible and earlier descriptions.
_Avoid_: Background, history

**Result**:
The bytes a **Job** produces, stored under its **Result key**.
_Avoid_: Output, answer

**Result key**:
The content address of a **Result**, derived from everything that determines it.
_Avoid_: Result id, cache key

### Coordination

**Broker**:
The service that queues **Jobs** and grants **Leases** on them. It never runs a model.
_Avoid_: Server, queue, coordinator

**Lease**:
A **Worker**'s time-limited right to run one **Job**, proven by its lease token.
_Avoid_: Lock, claim, reservation

### Execution

**Worker**:
The program that takes **Leases** from the **Broker**, runs **Jobs** with a **Runner**, and stores their
**Results**.
_Avoid_: Agent, client, node

**Runner**:
The part of a **Worker** that turns one **Job** into its **Result**, usually by asking a **Model server**.
_Avoid_: Handler, executor, model

**Model server**:
The process that loads and serves a model (Ollama), separate from the **Worker** and reached over HTTP.
_Avoid_: Backend, inference engine, model runtime

**Backend**:
A place a **Worker** runs: the local PC, a free notebook, Cloud Run, Kubernetes or a homelab VM.
_Avoid_: Host, platform, provider
