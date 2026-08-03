# Legacy worker environment

The target runtime is reconstructed from the checked-in Conda, pip, external
source, and baseline-wheel locks. The standalone worker remains compatible
with Python 3.7 and communicates with the modern controller through protocol
version 1 JSON files.

The public image is built only by the trusted tagged workflow and consumed by
immutable digest. Process execution against an independently reconstructed
environment remains the reproducible fallback.
