# 軟體設計文件 (SDD)
## OT 容器管理 REST API — v2.0

---

## 1. 文件目的與範圍

本文件描述 **OT 容器管理 REST API** 從 v1.0 升級至 v2.0 的架構設計、安全模型、API 規格，以及部署與運維指引。

目標讀者：系統整合工程師、OT/IT 邊緣設備開發者、以及使用 GitHub Copilot 維護此專案的開發人員。

---

## 2. v1.0 → v2.0 改善摘要

| 面向 | v1.0 問題 | v2.0 改善方式 |
|------|-----------|--------------|
| **認證** | 無任何認證，任何人可呼叫 | `X-API-Key` Header 強制認證 |
| **輸入驗證** | `container_id` 無驗證，可傳入路徑穿越字串 | 嚴格正則白名單 `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$` |
| **特權執行** | `sudo runc`，API 服務等同 root 操作 | 移除 `sudo`，改由 systemd 管理執行身分與能力 |
| **容器啟動** | `runc run`（前台阻塞），API 永久 hang | `runc create` + `runc start`（立即回傳） |
| **容器停止** | 直接 `SIGKILL`，無 graceful shutdown | 先 SIGTERM，等待 `grace_period` 秒，再 SIGKILL + delete |
| **資源更新** | 僅記錄 log，不實際修改 cgroup | 呼叫 `runc update`，立即生效 |
| **錯誤處理** | 所有錯誤一律 HTTP 500 | 404（找不到）、409（已存在）、400（輸入錯誤）、504（逾時） |
| **非同步** | 同步阻塞 `subprocess.run`，耗盡 worker | `asyncio.to_thread` 非阻塞執行 |
| **Timeout** | 無 timeout，runc 卡住即永久阻塞 | 所有 runc 呼叫均有 `RUNC_TIMEOUT`（預設 30 秒）|
| **健康檢查** | 無 `/health` 端點 | 新增 `GET /health`，供 systemd / 監控系統整合 |
| **容器隔離** | 僅 PID + Mount namespace，UID 映射為 root-to-root | 新增 User namespace，host UID offset = 100000 |
| **依賴版本** | fastapi 0.95 / uvicorn 0.21（2023 年，有 CVE） | fastapi 0.115 / uvicorn 0.32（最新穩定） |
| **部署** | 僅手動 `uvicorn api:app` | 提供 systemd unit file，開機自動啟動 |

---

## 3. 系統架構

### 3.1 架構圖

```
┌───────────────────────────────────────────────────────┐
│                     Edge Device (Host OS)              │
│                                                       │
│  ┌─────────────────────────────────────────────────┐  │
│  │           restful-runc.service (systemd)        │  │
│  │                                                 │  │
│  │   ┌─────────────────────────────────────────┐  │  │
│  │   │  FastAPI (uvicorn) — api.py             │  │  │
│  │   │                                         │  │  │
│  │   │  POST /api/containers/start             │  │  │
│  │   │  POST /api/containers/stop              │  │  │
│  │   │  PATCH /api/containers/{id}/resources   │  │  │
│  │   │  GET  /api/containers                   │  │  │
│  │   │  GET  /api/containers/{id}              │  │  │
│  │   │  GET  /health                           │  │  │
│  │   └────────────────┬────────────────────────┘  │  │
│  │                    │ asyncio.to_thread           │  │
│  │                    ▼                             │  │
│  │              subprocess.run(["runc", ...])       │  │
│  └────────────────────┬────────────────────────────┘  │
│                       │                               │
│    ┌──────────────────▼──────────────────┐            │
│    │  runc (OCI Runtime)                 │            │
│    │  ┌───────────────────────────────┐  │            │
│    │  │  Container (PID/Mount/User NS)│  │            │
│    │  │  app/sample.sh (OT Program)   │  │            │
│    │  └───────────────────────────────┘  │            │
│    └─────────────────────────────────────┘            │
│                                                       │
│    cgroups (/sys/fs/cgroup/)  ←── runc update         │
└───────────────────────────────────────────────────────┘
           ↑
    HTTP Client (SCADA / OT Manager / curl)
    X-API-Key: <secret>
```

### 3.2 元件說明

| 元件 | 說明 |
|------|------|
| `api.py` | FastAPI 應用程式，提供 REST 端點，通過 API Key 認證後委派給 runc |
| `runc` | Linux 原生 OCI Container Runtime，管理容器生命週期與 cgroup 資源限制 |
| `config.json` | OCI Runtime Spec，定義容器的 rootfs、namespace、cgroup、environment |
| `container_rootfs/` | 容器的 Root Filesystem（由 Buildroot/Yocto 生成） |
| `app/` | OT 應用程式目錄，掛載或打包進 rootfs |
| `systemd/restful-runc.service` | 系統服務定義，確保開機自啟、自動重啟 |

---

## 4. 安全模型

### 4.1 API 認證 — API Key

所有 `/api/*` 端點均要求 HTTP Header：

```
X-API-Key: <secret>
```

- API Key 由環境變數 `API_KEY` 提供，**不得寫入 `config.json` 或程式碼**。
- 若 `API_KEY` 未設定，服務拒絕所有請求並回傳 HTTP 503（防止意外開放）。
- 建議在部署前使用 `/usr/bin/openssl rand -hex 32` 生成隨機 Key。

> **未來強化方向**：可改用 JWT（短效 Token）或 mTLS（雙向憑證驗證），適合高安全性 OT 場域。

### 4.2 Container ID 輸入驗證

`container_id` 在傳入 `runc` 前必須通過白名單驗證：

```
Pattern: ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$
```

不符合格式的請求直接回傳 HTTP 400，不進入 runc 執行。

### 4.3 容器隔離 — Namespace 與 UID Mapping

**Namespace（config.json）：**

| Namespace | 說明 |
|-----------|------|
| `pid` | 容器內 PID 空間隔離 |
| `mount` | 掛載點隔離，容器看不到主機 mount |
| `user` | User ID 空間隔離（關鍵安全隔離） |

**User ID Mapping：**

```json
{
  "uidMappings": [{ "hostID": 100000, "containerID": 0, "size": 65536 }],
  "gidMappings": [{ "hostID": 100000, "containerID": 0, "size": 65536 }]
}
```

容器內的 `uid=0 (root)` 映射為主機的 `uid=100000`（非特權用戶），容器逃逸無法取得主機 root。

**前提條件**：主機需在 `/etc/subuid` 與 `/etc/subgid` 設定 offset（參見 README 安裝步驟）。

### 4.4 runc 執行身分

- **移除 `sudo`**：API 服務本身不應以 root 執行。
- **systemd 服務設定**：`AmbientCapabilities=CAP_SYS_ADMIN` 賦予 runc 所需核心能力，而非整個 root 權限。
- **rootless runc**（未來方向）：可進一步改為 rootless 模式，完全無需特權能力。

---

## 5. API 規格

### 5.1 端點列表

| 方法 | 路徑 | 功能 | 認證 |
|------|------|------|------|
| `GET` | `/health` | 服務健康檢查 | 不需要 |
| `POST` | `/api/containers/start` | 建立並啟動容器 | 需要 |
| `POST` | `/api/containers/stop` | 優雅停止並刪除容器 | 需要 |
| `PATCH` | `/api/containers/{container_id}/resources` | 更新容器資源限制 | 需要 |
| `GET` | `/api/containers` | 列出所有容器 | 需要 |
| `GET` | `/api/containers/{container_id}` | 取得指定容器狀態 | 需要 |

互動式 API 文件（Swagger UI）：`http://<host>:8000/docs`

### 5.2 請求/回應範例

**啟動容器：**
```http
POST /api/containers/start
X-API-Key: mysecretkey
Content-Type: application/json

{"container_id": "ot2it-01"}
```

**停止容器（含 grace period）：**
```http
POST /api/containers/stop
X-API-Key: mysecretkey
Content-Type: application/json

{"container_id": "ot2it-01", "grace_period": 15}
```

**更新資源：**
```http
PATCH /api/containers/ot2it-01/resources
X-API-Key: mysecretkey
Content-Type: application/json

{"cpu_shares": 1024, "memory_limit": 268435456}
```

### 5.3 HTTP 錯誤碼說明

| 狀態碼 | 情境 |
|--------|------|
| `400` | 輸入格式錯誤（container_id 不合法、Body 欄位缺失） |
| `403` | API Key 錯誤或缺失 |
| `404` | 容器不存在 |
| `409` | 容器已存在（啟動時） |
| `500` | runc 執行失敗（非預期錯誤） |
| `503` | 服務設定錯誤（API_KEY 未設定） |
| `504` | runc 操作逾時 |

---

## 6. 容器生命週期管理

### 6.1 啟動流程

```
Client                   API                    runc
  │   POST /start          │                      │
  │ ──────────────────────>│                      │
  │                        │  runc create --bundle │
  │                        │ ─────────────────────>│ (建立容器，狀態: created)
  │                        │  runc start           │
  │                        │ ─────────────────────>│ (啟動 init，狀態: running)
  │         200 OK         │                      │
  │ <──────────────────────│                      │
```

> **注意**：`runc create` 需指定 `--bundle <path>`，指向包含 `config.json` 與 `container_rootfs/` 的目錄。以環境變數 `BUNDLE_PATH` 設定（預設 `"."`）。

### 6.2 停止流程 (Graceful Shutdown)

```
Client                   API                    runc / Container
  │   POST /stop           │                      │
  │ ──────────────────────>│                      │
  │                        │  runc kill SIGTERM   │
  │                        │ ─────────────────────>│ (通知程式結束)
  │                        │  poll runc state      │
  │                        │ ─────────────────────>│ (每秒確認狀態)
  │                        │ (等待 grace_period 秒) │
  │                        │  runc kill SIGKILL   │ (若仍未結束則強制)
  │                        │ ─────────────────────>│
  │                        │  runc delete          │
  │                        │ ─────────────────────>│ (清除容器狀態)
  │         200 OK         │                      │
  │ <──────────────────────│                      │
```

`grace_period` 預設值由環境變數 `STOP_GRACE_PERIOD`（預設 10 秒）決定，可在請求 Body 覆寫。

### 6.3 資源動態更新

呼叫 `runc update` 直接修改 `/sys/fs/cgroup/` 對應路徑，**無需重啟容器**：

```
PATCH /api/containers/{id}/resources
  └─> runc update --cpu-shares <n> --memory <bytes> <id>
        └─> 寫入 /sys/fs/cgroup/.../cpu.shares
        └─> 寫入 /sys/fs/cgroup/.../memory.limit_in_bytes
```

---

## 7. 設定參數

### 7.1 環境變數

| 變數 | 必要 | 預設值 | 說明 |
|------|------|--------|------|
| `API_KEY` | **必要** | — | API 認證金鑰，務必設定 |
| `BUNDLE_PATH` | 選擇性 | `.` | runc bundle 目錄（含 config.json） |
| `RUNC_TIMEOUT` | 選擇性 | `30` | runc 操作逾時秒數 |
| `STOP_GRACE_PERIOD` | 選擇性 | `10` | 停止容器時 SIGTERM → SIGKILL 等待秒數 |

### 7.2 config.json 重要欄位

| 欄位 | 說明 |
|------|------|
| `process.user.uid/gid` | 容器內的執行 UID/GID（通常 0） |
| `linux.uidMappings[].hostID` | 對應主機的 UID offset（建議 ≥ 100000） |
| `linux.resources.memory.limit` | 容器最大記憶體（bytes） |
| `linux.resources.cpu.shares` | CPU 相對權重（預設 1024，此設定 512 = 一半權重） |
| `linux.namespaces` | 啟用的 namespace 清單（至少 pid、mount、user） |

---

## 8. 部署架構

### 8.1 目錄結構

```
runc-edge-api/
├── .github/
│   └── SDD.md                 # 本軟體設計文件
├── app/
│   └── sample.sh              # OT 範例程式
├── container_rootfs/          # Guest OS RootFS（需另外準備）
├── systemd/
│   └── restful-runc.service   # systemd 服務設定
├── tests/
│   ├── __init__.py
│   └── test_api.py            # 單元測試
├── api.py                     # 容器管理 REST API
├── config.json                # runc OCI 容器設定
├── requirements.txt           # 執行期相依套件
├── requirements-dev.txt       # 開發/測試相依套件
└── README.md
```

### 8.2 systemd 服務

服務設定檔：`systemd/restful-runc.service`

關鍵設定：
- `Restart=on-failure`：失敗時自動重啟（滿足 OT 高可用要求）
- `EnvironmentFile=/etc/runc-edge-api/env`：敏感設定（`API_KEY`）從外部檔案載入
- `AmbientCapabilities=CAP_SYS_ADMIN`：賦予 runc 所需最小能力
- `NoNewPrivileges=true`：防止服務取得額外特權

---

## 9. 測試策略

### 9.1 測試層級

| 測試類型 | 位置 | 工具 |
|----------|------|------|
| 單元測試 | `tests/test_api.py` | pytest + unittest.mock |
| API 整合測試 | `tests/test_api.py` | FastAPI TestClient |

### 9.2 測試覆蓋範圍

- **認證**：無 Key、錯誤 Key、正確 Key
- **輸入驗證**：合法/非法 container_id（路徑穿越、空白、超長等）
- **啟動**：成功、已存在（409）、runc 錯誤（500）、bundle path 傳遞驗證
- **停止**：成功（grace_period=0）、不存在（404）、預設 grace_period
- **資源更新**：CPU、Memory、空 body（400）、容器不存在（404）、`runc update` 命令驗證
- **列出容器**：有容器、空清單、runc 錯誤
- **取得容器**：成功、不存在（404）

---

## 10. 已知限制

1. **單一 Bundle**：目前設計假設所有容器使用同一個 `config.json`（同一 Bundle）。多容器多設定需擴充 Bundle 管理機制。

2. **啟動半成功狀態**：若 `runc create` 成功但 `runc start` 失敗，容器會卡在 `created` 狀態，需手動執行 `runc delete <id>` 清理。

3. **狀態持久化**：API 重啟後不保存已啟動容器的清單，需透過 `GET /api/containers` 重新查詢。

4. **rootless runc**：目前仍需 `CAP_SYS_ADMIN`，尚未實作完全 rootless 模式。

---

## 11. 未來改善方向

| 優先級 | 項目 |
|--------|------|
| 🟠 P1 | Network namespace 設定（含 veth pair / bridge 配置） |
| 🟠 P1 | 多 Bundle 支援（不同 OT 程式獨立設定） |
| 🟡 P2 | JWT 或 mTLS 認證替換 API Key |
| 🟡 P2 | Rootless runc（完全無特權） |
| 🟡 P2 | 容器狀態持久化（SQLite / 設定檔） |
| 🟡 P2 | Prometheus metrics 端點（`/metrics`） |
| 🟢 P3 | WebSocket 容器 log streaming |
| 🟢 P3 | OCI Image Pull 自動化（skopeo / umoci 整合） |
