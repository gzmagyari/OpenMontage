"""OpenMontage video-generator service core.

Job plumbing for the HTTP API in ``api/``: a disk-backed job store, FIFO
resource limits and an async job manager. Videos themselves are produced by
OpenMontage's standard agent-driven pipelines (AGENT_GUIDE.md, pipeline_defs/,
skills/, tools/) - this package deliberately contains no production logic.
"""

__version__ = "2.0.0"
