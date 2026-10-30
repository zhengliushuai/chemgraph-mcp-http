# chemgraph-mcp-http

把 [ChemGraph](https://github.com/argonne-lcf/ChemGraph) 的 MCP 工具用 **HTTP** 暴露出去，并让它产生的计算产物（xyz / traj / json / csv / log）能**回传到客户端机器**。

> 这不是 ChemGraph 的分支，也没有修改 ChemGraph 的任何源码。它是几个外挂脚本：
> 装 PyPI 上的 `chemgraph`，用本仓库的启动器起服务，工具就多出一个 `read_artifact`，
> 同一端口还多出 `GET /artifacts/<name>` 下载通道。

## 它解决的两个问题

1. **上游的 MCP server 只能 stdio**，别的机器连不上；而 FastMCP 的 app 默认只允许
   `127.0.0.1 / localhost / [::1]` 作为 Host，远程请求一律 `421 Invalid Host header`。
2. **产物留在服务端**。远端 agent 的 `run_ase` 结果里只有文件路径，字节拿不回来，没法画图和后处理。

## 能力

| 通道 | 用途 |
| --- | --- |
| MCP 工具 `read_artifact(path, force_base64=false, offset=0, length=0)` | 小文件直接进 agent 上下文：`{filename, size_bytes, sha256, content_base64, mime, server_path, range, sha256_scope, note}` |
| `GET /artifacts/{path}` | 大文件用 curl 拉，流式、支持 `Range`/206、`ETag`/304、`?sha256=1` 只查校验和 |
| `fetch_artifact.py` | 客户端一条命令下载 + 校验 sha256（纯标准库） |

安全与边界，全部**报错而不是静默处理**：

- 路径白名单：只能是 `CHEMGRAPH_DIR`（缺省 `CHEMGRAPH_LOG_DIR`，再缺省进程 CWD）下的文件。
  `..` 逃逸、绝对路径跑到别处、符号链接/junction 指向外部 —— 一律 `PermissionError` / HTTP 403。
- 单文件上限 **1 GiB**，超限报 `file too large` / HTTP 413，不截断。
- base64 内联上限默认 **8 MiB**，超过只返回元数据 + 提示（`content_base64: null`），
  因为工具返回值会进**客户端**的 LLM 上下文；要硬拿就 `force_base64=true`，或按
  `offset`/`length` 分块读。

## 安装

服务端（跑计算的那台）：

```bash
uv venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
uv pip install chemgraph             # 国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

然后把本仓库的文件放到那个目录（和 `.venv` 同级），或任意位置配合环境变量使用。
`chemgraph` 已要求 Python ≥ 3.11；`uvicorn` / `mcp` / `starlette` 都由 chemgraph 带入，无需额外安装。

## 启动

```bash
# 目录 = 计算产物落地目录，也是白名单根
export CHEMGRAPH_DIR=/path/to/artifacts
python mcp_http_server.py --module chemgraph.mcp.mcp_tools --host 0.0.0.0 --port 9003
```

Windows 直接双击 `start_mcp_http.bat`（自动使用同目录的 `.venv\Scripts\python.exe`）。

启动日志会打印白名单根，例如：

```
[mcp-http] DNS-rebinding host check disabled
[mcp-http] artifact root: /path/to/artifacts (GET /artifacts/<name>, 1 GiB ceiling)
[mcp-http] serving 'ChemGraph General Tools' on http://0.0.0.0:9003/mcp
```

别的 MCP server 模块也能这样起，换个 `--module` 换个端口即可：
`chemgraph.mcp.ase_mcp_hpc`、`mace_mcp_hpc`、`data_analysis_mcp`、`hpc_misc_mcp` …

`--allow-host 10.0.0.5:9003`（可重复）可以打开 DNS-rebinding 校验但只放行指定 Host；
不加则完全不校验 Host。

## 客户端接入

```bash
# 让远端 ChemGraph 用这台机器的工具（-v 会打印 "MCP tools: 5 loaded"）
chemgraph run -m <model> --base-url <llm-endpoint> \
  --mcp-url http://<server-ip>:9003/mcp -v

# 大文件：直接下载
curl -OJ http://<server-ip>:9003/artifacts/ethanol_opt.traj

# 下载 + 校验（推荐）
python fetch_artifact.py http://<server-ip>:9003 ethanol_opt.traj -o ./
```

`fetch_artifact.py` 先查 `?sha256=1` 拿服务端大小与哈希，再流式下载到 `.part`，
校验不过就删掉临时文件并 `exit 1`，所以半截文件不会看起来像下载成功。

## 测试

```bash
export CHEMGRAPH_DIR=/path/to/artifacts
export MCP_BASE=http://127.0.0.1:9003
python test_artifacts.py        # 工具：正常读、路径逃逸、缺失、目录、>1GiB、8MiB 守卫、分块
python test_e2e_artifact.py     # 端到端：用 MCP 生成 xyz，再经 read_artifact 读回并比对 sha256
```

## 已知限制

- **没有认证**。局域网内任何人可连 MCP 端点、可下载白名单目录下的文件。只适合可信内网，
  别直接映射到公网；需要时在前面加反向代理 + token。
- 白名单是单一根目录；ChemGraph 把产物写进进程 CWD，所以把 CWD 设成白名单根最省事。
- `read_artifact` 的 `sha256` 是**返回区间**的哈希（整文件读时即整文件哈希），字段 `sha256_scope` 会标明。
- 只在 Windows（Python 3.12、chemgraph 0.7.0、mcp 1.28.1）上实测过；Linux/macOS 路径处理用的是
  `os.path.realpath` + `normcase`，理论上等价，未实测。

## 与上游的关系 / License

- 上游：<https://github.com/argonne-lcf/ChemGraph>（Apache-2.0），本仓库不含其源码。
- 本仓库同样以 Apache-2.0 发布，见 `LICENSE` 与 `NOTICE`。
- 未与 Argonne 关联，非官方组件。

## 目录结构

```
artifact_tools.py      路径白名单、1 GiB 上限、sha256、read_artifact 工具注册
http_artifacts.py      GET /artifacts/{path} 路由（Range / ETag / ?sha256=1）
mcp_http_server.py     启动器：关掉 Host 校验 → 注册工具 → 插路由 → uvicorn
fetch_artifact.py      客户端下载 + 校验（仅标准库）
start_mcp_http.bat     Windows 启动脚本
test_artifacts.py      工具验收测试
test_e2e_artifact.py   端到端验收测试
```
