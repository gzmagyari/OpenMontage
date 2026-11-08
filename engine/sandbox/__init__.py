"""Each video job's agent runs in its own box (bubblewrap), not in the API's process space.

A job's Claude CLI, and everything it starts (Bash, Python, ffmpeg, Remotion), sees only:
the system and the repository read-only, its own project folder and a private temp folder,
its own process list, and no network. It reaches the outside through three sockets the
API serves for that job only:

- ``gateway``: its Claude traffic. The box holds a token valid for this job while it runs;
  the gateway adds the real Anthropic key on the way out (``engine/sandbox/gateway.py``).
- ``desk``: paid tools. A call of a tool that needs keys or the network is handed to the
  desk, which checks the budget, runs the tool in a short-lived provider box that has the
  keys but sees only this project, and records the cost (``engine/sandbox/desk.py``).
- ``web``: public websites for WebFetch and downloads, never private or internal
  addresses (``engine/sandbox/webproxy.py``).

No provider key, no other job's files and nothing of the server's environment is inside a
box. ``OPENMONTAGE_AGENT_SANDBOX=required`` (the Docker image's setting) fails a job that
cannot get a box; ``off`` runs the agent unboxed, for development hosts and tests only.
"""
