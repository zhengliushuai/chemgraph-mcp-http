# chemgraph-mcp-http

把 [ChemGraph](https://github.com/argonne-lcf/ChemGraph) 的 MCP 工具用 **HTTP** 暴露出去，并让它在服务端产生的计算产物（xyz / traj / json / csv / log）能**回传到客户端机器**，用于画图和后处理。

本 README 是一份面向"**一台崭新的服务器 + 一台客户端电脑**"的完整部署手册：按章节顺序照抄命令即可，每步都给出预期输出。命令在 Windows 10 + Python 3.12 + chemgraph 0.7.0 上实测通过，Linux 命令按通用写法给出（差异处已标注）。

| 你想干什么 | 看哪章 |
| --- | --- |
| 在服务器上装 ChemGraph | 第 1、2 章 |
| 配模型（OpenAI / 本地 vLLM / Ollama） | 第 3 章 |
| 启动 MCP HTTP 服务、放行端口 | 第 4 章 |
| 让另一台电脑接入并取回产物 | 第 5 章 |
| 验收测试（逐条预期输出） | 第 6 章 |
| 报错了对号入座 | 第 7 章 |
| 参数 / 接口 / 环境变量 | 第 9 章 |

---

## 0. 这个项目是什么，不是什么

**是**：几个外挂脚本。装 PyPI 上的 `chemgraph`，用本仓库的启动器起服务，工具表里就多出一个 `read_artifact`，同一端口还多出 `GET /artifacts/<name>` 下载通道。

**不是**：ChemGraph 的分支，也没有修改 ChemGraph 的任何一行源码。`site-packages` 保持原样，`uv pip install -U chemgraph` 之后本仓库继续可用。

它解决三个上游问题：

| 问题 | 现象 | 本仓库的解法 |
| --- | --- | --- |
| 上游 MCP server 只支持 stdio | 别的机器根本连不上 | `mcp_http_server.py` 用 streamable-HTTP 起服务 |
| FastMCP 默认只信任 `127.0.0.1` / `localhost` / `[::1]` 作 Host | 远程请求一律 `421 Invalid Host header` | 启动器关掉该校验（或用 `--allow-host` 精确放行） |
| 产物留在服务端 | 远端 agent 只拿到文件路径，拿不到字节，没法画图 | `read_artifact` 工具 + `GET /artifacts/{path}` + `fetch_artifact.py` |

---

## 1. 服务器准备（跑计算的那台）

### 1.1 要求

- Python **3.11+**（实测 3.12.14）
- 磁盘 ≥ 6 GB（依赖约 193 个包，含 torch CPU 版；Mace 模型权重另占几百 MB）
- 内存 ≥ 4 GB（跑 Mace / ASE 时越宽裕越好）
- 一个**可信内网**。本服务**没有认证**，不要直接暴露到公网（见第 8 章）
- 端口 **9003**（可改）

### 1.2 安装 uv（推荐）

Linux / macOS：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version
```

不想装 uv 也可以：下文 `uv venv` 换成 `python -m venv .venv`，`uv pip install X` 换成 `pip install X`，效果相同。Windows 上把 `python` 换成 `.venv\Scripts\python.exe`、`bin/` 换成 `Scripts/`。

### 1.3 国内网络：先配镜像

`pypi.org` 在国内经常卡死（实测直连 60 s 无响应，安装会一直挂着）。**先在同一个 shell 里 export，再去装**：

```bash
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
export UV_HTTP_TIMEOUT=120
```

用 pip 的话：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`。

### 1.4 建工作目录

Linux：`/opt/chemgraph`；Windows：`D:\chemgraph`（**别放 C: 盘根目录**，依赖装完要 5 GB 以上）。

```bash
# Linux
sudo mkdir -p /opt/chemgraph && sudo chown "$USER" /opt/chemgraph
cd /opt/chemgraph
```

```powershell
# Windows
mkdir D:\chemgraph; cd D:\chemgraph
```

**这个目录同时是"产物白名单根"**：`read_artifact` 与 `/artifacts` 只允许访问它下面的文件。想让产物落在别处，就在启动前设 `CHEMGRAPH_DIR` 指过去（4.1 节）。

---

## 2. 第一步：安装 ChemGraph

### 2.1 建虚拟环境并安装

```bash
uv venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
uv pip install chemgraph
```

走镜像约 2 分钟。装完确认版本：

```bash
python -c "import importlib.metadata as m; print('chemgraph', m.version('chemgraph'))"
```

预期：`chemgraph 0.7.0`（或更新）。

### 2.2 验收：先跑通不花钱、不联网的部分

```bash
python -c "from ase.build import molecule; from ase.calculators.emt import EMT; m=molecule('H2O'); m.calc=EMT(); print('H2O EMT energy =', round(m.get_potential_energy(),4), 'eV')"
```

预期：`H2O EMT energy = 2.6198 eV`

```bash
python -c "from rdkit import Chem; from rdkit.Chem import Descriptors; print('ethanol MW =', round(Descriptors.MolWt(Chem.MolFromSmiles('CCO')),2))"
```

预期：`ethanol MW = 46.07`

```bash
chemgraph --help
```

能打印子命令列表就说明 CLI 正常。这三条过了，说明 ASE / RDKit / 依赖链都没问题，之后出问题只可能是模型配置或网络。

> 安装/启动时可能出现 `WARNING: root: fairchem is not installed`、`tblite is not installed`，这是可选项没装的正常提示，不影响使用。

### 2.3 装 chemgraph-mcp-http 本体

```bash
git clone https://github.com/zhengliushuai/chemgraph-mcp-http.git
ls chemgraph-mcp-http      # artifact_tools.py http_artifacts.py mcp_http_server.py ...
```

没有 git 就下 zip：仓库页面右上角 **Code → Download ZIP**，解压成同名目录。

推荐目录布局（脚本目录会自动加进 `sys.path`，严格说放哪都能跑，但同级最不容易搞混）：

```
/opt/chemgraph/
├── .venv/                        ← 2.1 的虚拟环境
├── chemgraph-mcp-http/           ← 本仓库
│   ├── mcp_http_server.py
│   ├── artifact_tools.py
│   ├── http_artifacts.py
│   ├── fetch_artifact.py
│   ├── start_mcp_http.bat              （Windows）
│   ├── deploy/chemgraph-mcp.service    （Linux systemd）
│   └── test_artifacts.py  test_e2e_artifact.py
└── （计算产物 ethanol.xyz、co2_opt.traj … 会落在这里）
```

---

## 3. 第二步：配置模型

**只跑 MCP 服务端的话这一章不是必需的**（MCP server 本身不调用 LLM）。但 ChemGraph 作为客户端、以及 Streamlit 网页版需要模型；如果你想在这台机器上先验证 ChemGraph 能干活，就配一下。

配置文件位置（由 `chemgraph/ui/config.py` 决定，逻辑跨平台一致）：

| 系统 | 路径 |
| --- | --- |
| Windows | `%APPDATA%\chemgraph\config.toml` |
| Linux / macOS | `${XDG_CONFIG_HOME:-~/.config}/chemgraph/config.toml` |

文件不存在时，第一次运行 `chemgraph run` 或 `chemgraph ui` 会自动生成默认版本。**改完要重启进程**（网页版点侧栏 🔄 Reload Config）。

### 3.1 三条路线，选一条

**A. 云厂商（最省事）**——只设一个环境变量，模型名用默认 `gpt-4o-mini`：

```bash
export OPENAI_API_KEY="sk-..."        # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY / GROQ_API_KEY
```

**B. 本地 vLLM / 任意 OpenAI 兼容端点**——改 `[api.vllm]`：

```toml
[general]
model = "glm-5.3-flash"      # 关键：用配置里"没见过"的模型名，才会走 [api.vllm]

[api.vllm]
base_url = "http://10.70.0.132:18005/v1"
timeout = 300                # 本地小机器推理慢，千万别用 30
api_key = "local-dummy"      # 服务端不校验也必须有非空值
```

**C. Ollama（本机 11434）**：

```toml
[general]
model = "llama3.2"

[api.local]
base_url = "http://localhost:11434"
timeout = 60
```

> ⚠️ **401 的根源**：`OPENAI_API_KEY` 是假值/空值，而模型名又正好命中内置模型目录 → 请求发往官方 OpenAI 端点，报
> `Incorrect API key provided: ... code: 'invalid_api_key'`。本地模型只用 `[api.vllm]`（或 `VLLM_BASE_URL` / `VLLM_API_KEY`），**不要**为了"占个位"乱设 `OPENAI_API_KEY`。

### 3.2 一份可用的完整 config.toml

```toml
[general]
model = "glm-5.3-flash"
workflow = "single_agent"
output = "state"
structured = false
report = false
thread = 1
recursion_limit = 20
human_supervised = false
verbose = false

[api.openai]
base_url = "https://api.openai.com/v1"
timeout = 30

[api.vllm]
base_url = "http://10.70.0.132:18005/v1"
timeout = 300
api_key = "local-dummy"

[api.local]
base_url = "http://localhost:11434"
timeout = 60

[chemistry.optimization]
method = "BFGS"
fmax = 0.05
steps = 200

[chemistry.calculators]
fallback = "emt"            # 没网 / 没装 Mace 时回落到 EMT：纯 CPU、不需要下载权重

[output.files]
directory = "./chemgraph_output"
formats = [ "xyz", "json", "html",]

[output.visualization]
enable_3d = true
viewer = "py3dmol"
```

默认计算器是 **Mace**，首次调用会联网下载模型权重；离线机器请在提示词里明确"用 EMT 计算"，或依赖上面的 `fallback = "emt"`。

### 3.3 验证模型真的通

```bash
chemgraph run -q "用 EMT 计算水分子的单点能" -m glm-5.3-flash -v
```

看到 `run_ase` 之类的工具调用 + 最终能量即成功。首次运行要加载 LangChain/ASE，**6–10 s 属正常**。

只看密钥配置、不烧 token：

```bash
chemgraph run --check-keys
```

---

## 4. 第三步：启动 MCP HTTP 服务（服务端）

### 4.1 前台启动，先确认能跑

```bash
cd /opt/chemgraph
export CHEMGRAPH_DIR=/opt/chemgraph          # 产物白名单根；不设则用当前工作目录
python chemgraph-mcp-http/mcp_http_server.py \
    --module chemgraph.mcp.mcp_tools \
    --host 0.0.0.0 --port 9003
```

Windows：双击 `chemgraph-mcp-http\start_mcp_http.bat`。它自动用同目录 `.venv\Scripts\python.exe`，`CHEMGRAPH_DIR` 缺省=当前目录；先 `set CHEMGRAPH_DIR=D:\chemgraph` 再双击也行。

**启动成功的日志**（三条都要有）：

```
[mcp-http] DNS-rebinding host check disabled
[mcp-http] artifact root: /opt/chemgraph (GET /artifacts/<name>, 1 GiB ceiling)
[mcp-http] serving 'ChemGraph General Tools' on http://0.0.0.0:9003/mcp
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--module` | `chemgraph.mcp.mcp_tools` | 换别的 server 模块，如 `chemgraph.mcp.ase_mcp_hpc`、`mace_mcp_hpc`、`data_analysis_mcp`、`hpc_misc_mcp` |
| `--host` | `0.0.0.0` | 绑定地址；只给本机用就 `127.0.0.1` |
| `--port` | `9003` | 端口 |
| `--allow-host` | 空 | 打开 Host 校验但只放行给定值，可重复：`--allow-host 10.0.0.5:9003`。**不加 = 完全不校验 Host** |

### 4.2 本机自测（先证明服务活着）

```bash
# MCP 端点：能不能正常应答（有响应即可，不是 connection refused）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:9003/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

预期：`200`。

```bash
ls "$CHEMGRAPH_DIR"                                  # 先看有什么文件
curl -s "http://127.0.0.1:9003/artifacts/<某个文件>?sha256=1"; echo
```

预期：`{"filename":"...","size_bytes":... ,"sha256":"..."}`。

### 4.3 后台常驻

**Linux，systemd（推荐）**：

```bash
sudo cp chemgraph-mcp-http/deploy/chemgraph-mcp.service /etc/systemd/system/
sudo sed -i 's#/opt/chemgraph#'"$PWD"'#g' /etc/systemd/system/chemgraph-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now chemgraph-mcp
systemctl status chemgraph-mcp --no-pager
journalctl -u chemgraph-mcp -f               # 看实时日志
```

单元文件里 `User=chemgraph` / `Group=chemgraph` 是占位：**先改成你实际跑服务的账号**，否则 `systemctl start` 会报 `status=217/USER`。改法：

```bash
sudo sed -i 's/^User=.*/User='"$USER"'/; s/^Group=.*/Group='"$(id -gn)"'/' /etc/systemd/system/chemgraph-mcp.service
```

（也可以用 `sudo useradd -r -s /sbin/nologin chemgraph && sudo chown -R chemgraph:chemgraph /opt/chemgraph` 专门建一个服务账号。）

**临时后台跑（不用 systemd）**：

```bash
nohup python chemgraph-mcp-http/mcp_http_server.py --port 9003 > mcp-http.log 2>&1 &
tail -f mcp-http.log
```

**Windows**：用 `start_mcp_http.bat` 开一个最小化窗口；要开机自启就注册"登录时运行"的计划任务。

### 4.4 开放端口

```bash
# Linux：只放行内网网段，比全开安全
sudo ufw allow from 192.168.0.0/16 to any port 9003 proto tcp
# 或 firewalld
sudo firewall-cmd --add-port=9003/tcp --permanent && sudo firewall-cmd --reload
```

Windows：控制面板 → Windows Defender 防火墙 → 高级设置 → 入站规则 → 新建规则 → 端口 → TCP 9003 → 允许。
**云服务器还要在控制台安全组里放行 9003**，否则前面怎么配都不通。

查本机 IP：Linux `ip -4 addr show`，Windows `ipconfig`。下文统一用 `<服务器IP>` 占位。

---

## 5. 第四步：客户端电脑接入

### 5.1 客户端也要装 chemgraph

和第 2 章完全一样（`uv venv` + `uv pip install chemgraph`）。**不需要**装本仓库 —— 除非要用 `fetch_artifact.py`，而它只依赖 Python 标准库，拷一个文件过去就行。

客户端自己也要能访问 LLM：把第 3 章的 `config.toml` 拷过去，或设 `OPENAI_API_KEY` 等。

### 5.2 让客户端的 ChemGraph 使用服务端的工具

```bash
chemgraph run -q "用 EMT 优化乙醇，把轨迹保存成 traj" \
  -m glm-5.3-flash \
  --mcp-url http://<服务器IP>:9003/mcp -v
```

**成功的标志**（`-v` 输出）：

```
MCP tools: 5 loaded
```

5 个 = 上游的 4 个（`molecule_name_to_smiles`、`smiles_to_coordinate_file`、`extract_output_json`、`run_ase`）+ 本仓库的 `read_artifact`。

- URL **必须**以 `/mcp` 结尾，**不要**尾斜杠（`/mcp/` 会 307 重定向）
- 首次连上慢 6–10 s，别急着 Ctrl+C
- 显示 `MCP tools: 0 loaded` → 查第 7 章

### 5.3 让 agent 把产物带回来

工具已经在工具表里，agent 会自己用。提示词里把产物名字说清楚，例如：

```bash
chemgraph run --mcp-url http://<服务器IP>:9003/mcp -m glm-5.3-flash \
  -q "在服务器上用 EMT 优化乙醇，产物命名 ethanol_opt.traj，然后用 read_artifact 把文件内容取回来并告诉我 sha256"
```

- 文件 < 8 MiB：base64 直接进 agent 上下文
- 文件 ≥ 8 MiB：工具只回元数据 + `note` 提示，此时用下面的下载通道（否则会把客户端 LLM 上下文撑爆）

### 5.4 大文件：HTTP 下载（推荐）

把 `fetch_artifact.py` 拷到客户端（纯标准库，无需 pip install）：

```bash
python fetch_artifact.py http://<服务器IP>:9003 ethanol_opt.traj
python fetch_artifact.py http://<服务器IP>:9003 ethanol_opt.traj -o ./runs/ --quiet
```

它先查 `?sha256=1` 拿服务端的大小与哈希，再流式写入 `.part`，本地哈希比对通过才改名落地；不一致就删除临时文件并 `exit 1` —— 半截文件永远不会看起来像下载成功。

没有 Python 也行：

```bash
curl -OJ http://<服务器IP>:9003/artifacts/ethanol_opt.traj           # 按服务端文件名保存
curl -H 'Range: bytes=0-1023' http://<服务器IP>:9003/artifacts/ethanol_opt.traj
```

浏览器直接打开 `http://<服务器IP>:9003/artifacts/ethanol_opt.traj` 也能下载。

---

## 6. 第五步：测试验收

测试脚本**在服务端机器上跑**（既连 MCP，又要读本地磁盘比对哈希），用第 2.1 步的 venv：

```bash
cd /opt/chemgraph
export CHEMGRAPH_DIR=/opt/chemgraph        # 必须和启动服务时的白名单根一致
export MCP_BASE=http://127.0.0.1:9003      # 换成服务端 IP 也行
```

### 6.1 工具级验收（7 项）

```bash
python chemgraph-mcp-http/test_artifacts.py
```

实测输出（逐行对照，`...` 处是具体路径/数字）：

```
TOOLS: ['molecule_name_to_smiles', 'smiles_to_coordinate_file', 'extract_output_json', 'run_ase', 'read_artifact']
[1] small: err=False name=ethanol_opt.traj size=60907 b64_bytes=60907 sha_ok=True file_sha_ok=True note=None
[2] reject '..\..\Windows\win.ini': err=True msg="Error executing tool read_artifact: path outside artifact root: ..."
[2] reject 'C:\Windows\win.ini': err=True msg="... path outside artifact root: ..."
[2] reject '../../../../etc/hosts': err=True msg="... path outside artifact root: ..."
[2] reject 'cg_logs/../../ethanol.xyz': err=True msg="... path outside artifact root: ..."
[3] missing: err=True msg="... artifact not found: 'does_not_exist.xyz' ..."
[4] directory: err=True msg="... artifact path is a directory, not a file: ..."
[5] >1GiB: err=True msg='... file too large: _big_1_1GiB.bin is 1180696576 bytes, limit is 1073741824 bytes (1 GiB). Refusi...'
[6a] 12MiB default: b64=None note='content withheld: 12582912 bytes exceeds the 8388608-byte inline limit'
[6b] 12MiB forced: err=False b64_len=16777216 roundtrip_ok=True
[7] chunk: len=64 all_A=True range={'offset': 1000, 'length': 64} scope=range
```

判读：

- `[1]` 三个 `True` = 小文件经 base64 传回后与服务端磁盘**逐字节一致**
- `[2]` 四种越权全部 `err=True` = 白名单生效（`..` 逃逸、跨盘绝对路径、Unix 根、借道子目录逃逸）
- `[5]` **必须报错**（`file too large ... Refusing`），绝不能返回截断内容 —— 这是硬性要求
- `[6a]` 超 8 MiB 默认不给 base64（保护客户端上下文）；`[6b]` `force_base64=true` 能强制取回
- `[7]` `offset`/`length` 分块读，`sha256_scope=range` 表示哈希只覆盖这一段
- 脚本会自己生成 `ethanol_opt.traj`、`cg_logs/`、`_big_1_1GiB.bin`、`_mid_12MiB.bin` 并清理，**空服务器也能直接跑**

> `[3]`/`[4]` 的报错文本在 Windows 与 Linux 上略有差别（路径分隔符、是否带绝对路径），只要 `err=True` 就算通过。

### 6.2 端到端验收（生成 → 回读）

```bash
python chemgraph-mcp-http/test_e2e_artifact.py
```

实测输出：

```
produced: {   "ok": true,   "artifact": "coordinate_file",   "path": ".../verify_ethanol.xyz",   "smiles": "CCO",   "natoms": 9 } | isError: False
read_artifact: verify_ethanol.xyz 542 B mime=chemical/x-xyz note=None
  b64 roundtrip == disk bytes: True
  sha256 tool == disk: True
  xyz head: ['9', 'Properties=species:S:1:pos:R:3 pbc="F F F"']
```

两个 `True` = "算完就能取回并校验"这条链路真的通。跑完可删 `verify_ethanol.xyz`。

### 6.3 HTTP 端点验收（curl 六条）

```bash
B=http://127.0.0.1:9003
F=verify_ethanol.xyz          # 换成任意已存在的文件

curl -s -o /dev/null -w 'GET    %{http_code} size=%{size_download}\n' "$B/artifacts/$F"
curl -s "$B/artifacts/$F?sha256=1"; echo
curl -s -o /dev/null -w 'RANGE  %{http_code}\n' -H 'Range: bytes=0-1023' "$B/artifacts/$F"
curl -s -o /dev/null -w 'BAD    %{http_code}\n' -H 'Range: bytes=99999999-' "$B/artifacts/$F"
curl -s -o /dev/null -w 'MISS   %{http_code}\n' "$B/artifacts/nope.xyz"
curl -s -o /dev/null -w 'ESCAPE %{http_code}\n' --path-as-is "$B/artifacts/../../etc/passwd"
```

预期：`GET 200`、`RANGE 206`、`BAD 416`、`MISS 404`、`ESCAPE 403`。
（Windows 上最后一条可能被 uvicorn 归一化成 404，同样是"拒绝"，算通过。）

再核对哈希一致：

```bash
curl -s "$B/artifacts/$F?sha256=1" | python -c "import sys,json;print(json.load(sys.stdin)['sha256'])"
sha256sum "$F"        # macOS: shasum -a 256 "$F"；Windows: certutil -hashfile "$F" SHA256
```

两个值必须相同。

### 6.4 客户端下载验收

在**客户端**机器上：

```bash
python fetch_artifact.py http://<服务器IP>:9003 verify_ethanol.xyz -o ./
echo $?
```

预期：

```
server says: verify_ethanol.xyz 542 bytes sha256=...
  542/542 bytes (100.0%)
OK -> verify_ethanol.xyz (542 bytes, sha256 verified)
0
```

把文件名写错再试一次，应看到 `error: server returned 404 ...` 且退出码 `1`。

### 6.5 客户端 MCP 直连验收

```bash
chemgraph run --mcp-url http://<服务器IP>:9003/mcp -m glm-5.3-flash -v \
  -q "用 molecule_name_to_smiles 查一下 methane 的 SMILES"
```

`MCP tools: 5 loaded` + 返回结果里出现 `C`，即端到端打通。

---

## 7. 出问题对号入座

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `421 Invalid Host header` | 用了上游原生 `--transport streamable_http`，没走本仓库启动器 | 用 `mcp_http_server.py` 启动；或加 `--allow-host <ip>:<port>` |
| `Connection refused` | 服务没起 / 端口错 / 防火墙 | 先本机 `curl -sI http://127.0.0.1:9003/artifacts/x`；再 `ss -ltnp \| grep 9003`（Win: `netstat -ano \| findstr 9003`）；最后查防火墙与云安全组 |
| `MCP tools: 0 loaded` | URL 少了 `/mcp`；服务端报错；版本不匹配 | URL 用 `http://ip:9003/mcp`；看服务端日志 |
| `/mcp/` 返回 307 / 404 | 尾斜杠 | 去掉尾斜杠 |
| `Incorrect API key provided ... invalid_api_key` | 假/空的 `OPENAI_API_KEY`，模型名命中官方 OpenAI 路由 | 本地模型改用 `[api.vllm]` / `VLLM_*`；清掉假的 `OPENAI_API_KEY` |
| `path outside artifact root` | 请求路径不在 `CHEMGRAPH_DIR` 下（**安全行为，不是 bug**） | 把文件放进白名单根，或改 `CHEMGRAPH_DIR` 后重启 |
| `artifact not found` | 文件名错，或产物落在别的目录 | `ls $CHEMGRAPH_DIR`；ChemGraph 默认写在**进程 CWD** |
| `file too large ... (1 GiB)` / HTTP 413 | 超过硬上限 | 用 `offset`/`length` 分块读，或先压缩 |
| 第一次工具调用卡 6–10 s | 加载 LangChain/ASE/模型 | 正常 |
| 首次 `run_ase` 卡很久 / 下载失败 | 默认计算器 Mace 在下权重 | 提示词指定"用 EMT"，或配 `[chemistry.calculators] fallback = "emt"` |
| `uv pip install` 长时间无输出 | 直连 pypi 卡住 | 设 `UV_DEFAULT_INDEX` 走镜像（1.3 节） |
| Windows `chemgraph ui` 报 401 | 网页版只读配置文件 | 改 `%APPDATA%\chemgraph\config.toml`，重启或点 🔄 Reload Config |
| `git ls-remote`/`git push` 报 `Out of memory, malloc failed (tried to allocate 947912704 bytes)` | 全局 `http.postBuffer` 被设成了几 GB | `git config --global http.postBuffer 52428800` |
| Linux 上 `python: command not found` | 没激活 venv | `source .venv/bin/activate`，或用绝对路径 `.venv/bin/python` |
| 服务起来了但别的机器连不上，日志正常 | 监听在 `127.0.0.1` | `--host 0.0.0.0` |

日志：`CHEMGRAPH_DIR/cg_logs/`；systemd 部署看 `journalctl -u chemgraph-mcp -f`。

---

## 8. 安全边界（务必读）

- **没有认证**。局域网内任何人只要知道 `IP:端口`，就能调用全部计算工具、下载白名单目录下的任何文件。设计前提是**可信内网**。
- 需要更强隔离时：① 只绑内网网卡 `--host 10.0.0.5`；② 用 `--allow-host <ip>:<port>` 保留 Host 校验；③ 前面加 nginx/Caddy 反向代理 + Basic Auth/token，并把 9003 只对本机开放。
- 白名单根是 `CHEMGRAPH_DIR` 一个目录，**别把它设成 `/`、`C:\` 或家目录**。
- 单文件 1 GiB 硬上限，工具和 HTTP 端点都执行；超限直接报错/413，**绝不截断**。
- 工具返回值会进**客户端**的 LLM 上下文：8 MiB base64 ≈ 170 万 token。所以默认超 8 MiB 只回元数据，大文件请走 HTTP。
- 计算会写文件、吃 CPU/内存；ChemGraph 官方也提醒"结果需人工复核"，别把 agent 的结论当最终数据。

---

## 9. 接口 / 参数 / 环境变量

### 9.1 MCP 工具 `read_artifact`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | str | 必填 | 相对 `CHEMGRAPH_DIR` 的路径（也接受其下的绝对路径） |
| `force_base64` | bool | `false` | 超过 8 MiB 也强制返回 base64 |
| `offset` | int | `0` | 起始字节，用于分块读 |
| `length` | int | `0` | 读取长度，`0` = 到文件尾 |

返回字段：`filename`、`size_bytes`、`sha256`、`content_base64`、`encoding`、`mime`、`server_path`、`range{offset,length}`、`sha256_scope`（`file` / `range`）、`note`。

### 9.2 HTTP 端点

| 请求 | 行为 |
| --- | --- |
| `GET /artifacts/{path}` | 流式下载（1 MB 分块），带 `Content-Disposition` / `ETag` / `Accept-Ranges` |
| `HEAD /artifacts/{path}` | 只要响应头（大小、ETag） |
| `GET /artifacts/{path}?sha256=1` | 只回 `{"filename","size_bytes","sha256"}`，不传内容 |
| `Range: bytes=a-b` | 206 部分内容；越界 416 |
| `If-None-Match: <etag>` | 304 未修改 |
| 状态码 | 404 不存在 / 403 越权 / 400 是目录非文件 / 413 超 1 GiB |

### 9.3 环境变量

| 变量 | 用在哪 | 说明 |
| --- | --- | --- |
| `CHEMGRAPH_DIR` | 服务端 | 产物白名单根；缺省回落到 `CHEMGRAPH_LOG_DIR`，再缺省用进程 CWD |
| `MCP_BASE` | 测试脚本 | 被测服务地址，默认 `http://127.0.0.1:9003` |
| `OPENBLAS_NUM_THREADS` / `OMP_NUM_THREADS` | 服务端 | 小内存机器设 `1`，避免 numpy 线程把内存吃满 |
| `XDG_CONFIG_HOME` | 客户端/服务端 | 改 `config.toml` 所在目录（Linux/macOS） |
| `VLLM_BASE_URL` / `VLLM_API_KEY` | 客户端/服务端 | 等价于 `[api.vllm]` 的 base_url / api_key |

---

## 10. 升级 / 卸载

```bash
uv pip install -U chemgraph            # 升级 ChemGraph，本仓库不受影响
cd chemgraph-mcp-http && git pull      # 升级本仓库

sudo systemctl disable --now chemgraph-mcp        # 停 systemd 服务
pkill -f mcp_http_server.py                       # 或按进程名杀

rm -rf /opt/chemgraph                             # 彻底卸载（含 venv 与产物，先备份！）
```

---

## 11. 目录结构

```
artifact_tools.py             路径白名单、1 GiB 上限、sha256、read_artifact 工具注册
http_artifacts.py             GET /artifacts/{path} 路由（Range / ETag / ?sha256=1）
mcp_http_server.py            启动器：关 Host 校验 → 注册工具 → 插路由 → uvicorn
fetch_artifact.py             客户端下载 + 校验（仅标准库）
start_mcp_http.bat            Windows 启动脚本
deploy/chemgraph-mcp.service  Linux systemd 单元
test_artifacts.py             7 项工具验收
test_e2e_artifact.py          生成 → 回读端到端验收
```

---

## 12. 与上游的关系 / License

- 上游：<https://github.com/argonne-lcf/ChemGraph>（Apache-2.0），本仓库**不含**其源码。
- 本仓库同样以 Apache-2.0 发布，见 `LICENSE` 与 `NOTICE`。
- 未与 Argonne 关联，非官方组件；ChemGraph 自身的计算结论需人工复核。
- 实测环境：Windows 10 / Python 3.12.14 / chemgraph 0.7.0 / mcp 1.28.1。Linux 命令为通用写法，路径处理用 `os.path.realpath` + `normcase`，与 Windows 版本逻辑等价但未在 Linux 实机验证。
