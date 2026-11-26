# Fleet

A volunteer-computing style job system: a **Broker** hands out **Jobs** to **Workers** running on many
**Backends**, so GPU work (first, Metaphor's cutscene descriptions) runs wherever compute is free.

## Language

### Work

**Job**:
One unit of work the **Broker** hands out: an input, a model and a prompt version, plus an optional **Context**.
_Avoid_: Task, message

**Data class**:
Who may receive a **Job**'s input: `footage` (video, audio or frames from the game), `derived` (text produced
from the game or its footage, such as descriptions, the script table or the story bible) or `open` (material
that may be shared). Every **Job** states one.
_Avoid_: Job class, sensitivity, tier

**Clearance**:
The **Data classes** a worker token may receive, decided by the **Broker**, never declared by the **Worker**.
_Avoid_: Permission, access level, scope

**Capability**:
A prompt version a **Worker**'s **Runner** implements, declared by the **Worker** when it asks for a **Lease**,
together with the models its **Model server** holds.
_Avoid_: Skill, feature, job class

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

**Cancelled**:
The state of a **Job** the maintainer stopped with `POST /jobs/{job_id}/cancel` while it was queued or leased. It
is never leased again, and its **Lease** holder's heartbeat and completion are refused like any lost lease. Final;
a done or dead job cannot be cancelled. The other states are queued, leased, done and dead.
_Avoid_: Deleted, aborted, killed

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
