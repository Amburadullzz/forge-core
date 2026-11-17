# Forge — a GitHub-compatible git host in one process

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/Amburadullzz/forge-core/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/Amburadullzz/forge-core/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

One Python 3.13 process, no UI, no outside service, standard library only:

- **git smart HTTP** — `info/refs`, `git-upload-pack`, `git-receive-pack`, with its own packfile
  writer/reader (OFS and REF deltas, thin-pack bases) and v0 + v2 advertisements
- **REST v3** — repositories, issues, comments, labels, pulls, contents, Link pagination
- **GraphQL v4** — queries, mutations, introspection, batching, aliases, inline fragments
- **MCP** — JSON-RPC 2.0, `server/discover` and legacy `initialize`, 14 tools
- **Store** — its own JSON snapshot plus an object log, deterministic byte for byte

```
python3 main.py                      # binds an ephemeral port, registers, serves
python3 main.py --port 8080          # fixed port
python3 tests/smoke.py               # 54 end-to-end checks
```

Bounty: https://make.fun/bounty/github-core-clone-01
