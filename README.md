# gpu-fleet

A compute-agnostic GPU worker fleet: a small broker hands out leased jobs and presigned storage URLs, and the same
worker runs unchanged on a local GPU, free notebooks (Kaggle, Colab), Cloud Run Jobs and Kubernetes. First workload:
cutscene audio descriptions for the Metaphor: ReFantazio accessibility mod
([#1013](https://github.com/Sour-Dev-Home/metaphor-accessibility/issues/1013),
[#1025](https://github.com/Sour-Dev-Home/metaphor-accessibility/issues/1025)).

Built as a learning project, phase by phase: see [docs/phases.md](docs/phases.md).
