# ASI Embedding (Placeholder Name)

ASI Embedding is a python-based wrapper for QM/QM embedding simulations using the ASI (Atomic Simulation Interface). The wrapper handles communication between QM code library calls through C-based callbacks, and imports/exports relevant matices such as density matrices and hamiltonians required for embedding schemes such as Projection-Based Embedding (PbE). Atomic information is communicated through Atomic Simulation Environment (ASE) atoms objects.

# Supported QM Pakage(s)

- FHI-aims

# Requires

- Python >=3.8
- ASE <=2.3.1
- asi4py (Bespoke version currently)
- Shared-object library for relevant QM driver