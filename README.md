# runc-edge-api：OT 邊緣設備的 Linux 原生容器 REST API 管理工具

[![CI](https://github.com/http418imateapot/runc-edge-api/actions/workflows/ci.yml/badge.svg)](https://github.com/http418imateapot/runc-edge-api/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/http418imateapot/runc-edge-api)](https://github.com/http418imateapot/runc-edge-api/releases/latest)

## 簡介

使用 Linux 原生容器機制 (runc / cgroups)，示範 OT 程式系統資源安全性限制，並提供通過認證的輕量容器管理 REST API，為嵌入式 OT2IT 單板電腦產品提供低系統資源需求的輕量容器化方案。

> **v2.0 改善重點**：API Key 認證、container_id 輸入驗證、User Namespace 隔離、runc create/start 非阻塞啟動、實際 cgroup 資源更新（runc update）、Graceful Shutdown、完整錯誤碼、systemd 服務支援。
> 完整設計說明請見 [.github/SDD.md](.github/SDD.md)。

## 專案架構

```
runc-edge-api/
├── .github/
│   └── SDD.md              # 軟體設計文件
├── app/
│   └── sample.sh           # OT2IT 範例心跳程式
├── runc_edge_api/
│   ├── __init__.py         # 套件版本資訊
│   ├── __main__.py         # CLI / console entry point
│   └── api.py              # 容器管理 REST API
├── container_rootfs/       # Guest OS RootFS (用 Buildroot/Yocto 生成)
├── systemd/
│   └── runc-edge-api.service # systemd 服務設定
├── tests/
│   ├── __init__.py
│   └── test_api.py         # 單元測試
├── api.py                  # 舊版匯入相容 shim
├── pyproject.toml          # Python 套件與建置設定
├── VERSION                 # 專案版本號
├── CHANGELOG.md            # 版本變更紀錄
├── SECURITY.md             # 漏洞通報政策
├── CONTRIBUTING.md         # 貢獻指南
├── config.json             # runc OCI 容器設定
├── requirements.txt        # 執行期相依套件
├── requirements-dev.txt    # 開發/測試相依套件
└── README.md
```

## Linux 版本需求

* Kernel：建議 4.19 以上，需支援 cgroups v1 或 v2
* 核心功能：必須啟用 cgroups、User Namespace（`CONFIG_USER_NS`）、PID Namespace、Mount Namespace
* 適用平台：嵌入式工控設備，如 Raspberry Pi 4、x86 工控主機
* 其他：python3 (3.9+)、python3-pip、runc、cgroup-tools

---

## 安裝步驟

### 1. 下載專案

```bash
git clone https://github.com/http418imateapot/runc-edge-api.git
cd runc-edge-api
```

### 2. 安裝原生容器工具

```bash
sudo apt-get install -y runc cgroup-tools
```

### 3. 設定 User Namespace 的 UID/GID 映射

`config.json` 使用 User Namespace，讓容器內 uid=0 映射到主機 uid=100000（非 root），需在主機設定 subuid/subgid：

```bash
# 以 API 服務帳號（例如 ot-api）為例
sudo usermod --add-subuids 100000-165536 ot-api
sudo usermod --add-subgids 100000-165536 ot-api

# 確認設定
grep ot-api /etc/subuid /etc/subgid
```

### 4. 準備 Guest OS RootFS（非必要，可先跳過）

如果尚未準備好 RootFS，保持 `container_rootfs/` 目錄為空即可（用 `.gitkeep` 佔位）。

或使用 Buildroot/Yocto 裁剪最小化 Linux image，將 RootFS 解壓縮到 `container_rootfs/` 目錄中：

```bash
# 範例：使用 Docker 快速建立最小 RootFS (僅限測試)
docker export $(docker create alpine) | tar -C container_rootfs -xf -
```

### 5. 安裝 REST API 套件

```bash
sudo apt-get install -y python3-full python3-pip
python3 -m venv venv
source venv/bin/activate
pip install .
```

### 6. 生成 API Key 並設定環境變數

**API Key 是必要設定**，未設定時服務會拒絕所有請求。

```bash
# 生成隨機 Key
export API_KEY=$(openssl rand -hex 32)
echo "API_KEY=$API_KEY"   # 請妥善保存此金鑰

# 設定 Bundle 路徑（包含 config.json 的目錄）
export BUNDLE_PATH=$(pwd)
```

### 7. 啟動 REST API 服務

```bash
source venv/bin/activate
runc-edge-api --host 127.0.0.1 --port 8000
```

Swagger UI 文件：http://127.0.0.1:8000/docs

---

## API 使用說明

所有 `/api/*` 端點均需在 HTTP Header 帶入 `X-API-Key`。

### 啟動容器

```bash
curl -X POST http://127.0.0.1:8000/api/containers/start \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container_id": "OT2IT-Sample"}'
```

### 停止容器（含優雅停機）

```bash
curl -X POST http://127.0.0.1:8000/api/containers/stop \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container_id": "OT2IT-Sample", "grace_period": 10}'
```

### 動態調整容器資源

```bash
# 調整 CPU 與記憶體限制（無需重啟容器）
curl -X PATCH http://127.0.0.1:8000/api/containers/OT2IT-Sample/resources \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"cpu_shares": 1024, "memory_limit": 268435456}'
```

### 列出所有容器

```bash
curl http://127.0.0.1:8000/api/containers \
  -H "X-API-Key: $API_KEY"
```

### 查詢指定容器狀態

```bash
curl http://127.0.0.1:8000/api/containers/OT2IT-Sample \
  -H "X-API-Key: $API_KEY"
```

### 健康檢查（不需要 API Key）

```bash
curl http://127.0.0.1:8000/health
```

---

## API 端點總覽

| 方法 | 路徑 | 功能 | 認證 |
|------|------|------|------|
| `GET` | `/health` | 服務健康檢查 | 不需要 |
| `POST` | `/api/containers/start` | 建立並啟動容器 | 需要 |
| `POST` | `/api/containers/stop` | 優雅停止並刪除容器 | 需要 |
| `PATCH` | `/api/containers/{id}/resources` | 動態更新資源限制 | 需要 |
| `GET` | `/api/containers` | 列出所有容器 | 需要 |
| `GET` | `/api/containers/{id}` | 取得容器狀態 | 需要 |

---

## systemd 自動啟動設定

### 建立服務帳號與設定目錄

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ot-api
sudo mkdir -p /etc/runc-edge-api
sudo bash -c "echo 'API_KEY=$(openssl rand -hex 32)' > /etc/runc-edge-api/env"
sudo chmod 600 /etc/runc-edge-api/env
sudo chown ot-api:ot-api /etc/runc-edge-api/env
```

### 部署專案

```bash
sudo cp -r . /opt/runc-edge-api
sudo chown -R ot-api:ot-api /opt/runc-edge-api
cd /opt/runc-edge-api
sudo -u ot-api python3 -m venv venv
sudo -u ot-api venv/bin/pip install .
```

### 啟用服務

```bash
sudo cp systemd/runc-edge-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable runc-edge-api
sudo systemctl start runc-edge-api
sudo systemctl status runc-edge-api
```

---

## 執行測試

```bash
pip install -e .[dev]
pytest tests/ -v
```

---

## 套件與版本資訊

- 套件安裝名稱：`runc-edge-api`
- 啟動指令：`runc-edge-api --host 127.0.0.1 --port 8000`
- 版本單一來源：[`VERSION`](VERSION)
- 變更紀錄：[`CHANGELOG.md`](CHANGELOG.md)
- 安全政策：[`SECURITY.md`](SECURITY.md)
- 貢獻指南：[`CONTRIBUTING.md`](CONTRIBUTING.md)

---

## 設定 config.json

可根據需求調整 `config.json` 的資源欄位：

| 欄位 | 說明 |
|------|------|
| `linux.resources.memory.limit` | 容器最大記憶體（bytes） |
| `linux.resources.cpu.shares` | CPU 相對權重（預設 1024，512 = 一半） |
| `linux.uidMappings[].hostID` | User Namespace 映射的主機 UID offset（建議 ≥ 100000） |
| `linux.namespaces` | 啟用的 namespace 清單 |

---

## 環境變數參考

| 變數 | 必要 | 預設值 | 說明 |
|------|------|--------|------|
| `API_KEY` | **必要** | — | API 認證金鑰 |
| `BUNDLE_PATH` | 選擇性 | `.` | runc bundle 目錄（含 config.json） |
| `RUNC_TIMEOUT` | 選擇性 | `30` | runc 操作逾時秒數 |
| `STOP_GRACE_PERIOD` | 選擇性 | `10` | 停止容器 SIGTERM→SIGKILL 等待秒數 |
