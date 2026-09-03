<h1 align="center">OutlookRegister</h1>

<p align="center">
  Outlook / Hotmail 自動註冊，並取得 Microsoft Graph OAuth2 <code>refresh_token</code>（以 DrissionPage 驅動本機 Chrome 進行瀏覽器自動化）。
</p>

<p align="center">
  <a href="./README.md">English</a> ·
  <a href="./README.zh-CN.md">简体中文</a> ·
  <a href="./README.zh-TW.md"><b>繁體中文</b></a>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
  <img alt="DrissionPage" src="https://img.shields.io/badge/Browser-DrissionPage-4B5563">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-blue">
</p>

> 本專案基於 **[LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister)** 二開。  
> 強化重點：可選輔助信箱綁定、更完整的 OAuth 中間頁、cookie 優先授權、分批排程，以及 Ctrl+C 中斷後的彙總與清理。

---

## 使用教學

### 1. 環境需求

- 建議 Python 3.10+  
- 可用的 HTTP/SOCKS 代理（強烈建議）  
- 若啟用輔助信箱綁定：相容的臨時信箱 / CF Temp Mail 類 API  

### 2. 安裝

```bash
git clone <本倉庫網址>
cd OutlookRegister
pip install -r requirements.txt
# 無需另外下載瀏覽器核心；確保本機已安裝 Chrome 即可
# 若 Chrome 不在預設路徑，可於 config.json 的 browser.path 指定 chrome.exe
```

### 3. 設定

```bash
# Windows
copy config.example.json config.json

# Linux / macOS
cp config.example.json config.json
```

編輯 `config.json`：至少填好 **proxy**。對外分享時請勿包含真實金鑰。

### 4. 執行

```bash
python main.py
```

成功帳號會**追加**寫入 `Results/oauth2.txt`：

```text
信箱----密碼----client_id----refresh_token
```

日誌位於 `log/`。按 **Ctrl+C** 可中斷：先寫彙總，再關閉瀏覽器並清理 profile。

---

## `config.json` 欄位說明

範本見 `config.example.json`（與空白 `config.json` 結構相同）。兩個檔案都支援**整行 `//` 註解**（程式讀取時會先剝掉再解析；隨附 `.vscode/settings.json` 將其按 JSONC 處理，編輯器不再報錯）。

### 頂層欄位

| 欄位 | 類型 | 說明 |
| --- | --- | --- |
| `email_suffix` | string | 註冊信箱後綴，如 `@outlook.com` 或 `@hotmail.com`。 |
| `headless` | bool | `false` 顯示瀏覽器視窗；`true` 無頭。 |
| `bot_protection_wait` | number | 註冊頁同意按鈕前的等待基準（秒）；表單欄位採用隨機人類化節奏。 |
| `max_captcha_retries` | number | 驗證碼按壓總嘗試次數（如 `2` = 按壓兩次都不過就棄用當前視窗；single 模式則判定此IP不可用並結束執行）。 |
| `captcha_strategy` | number | 驗證碼／交接策略，見下表。 |
| `px_solve_mode` | string | PX「按住」驗證碼解法：`"hold"`（預設，成功率穩定）或 `"a11y"`（無障礙備用：點無障礙小人圖示→等進度條走完→點長條確認）。 |
| `concurrent_flows` | number | 並發執行緒數（同時開啟的瀏覽器任務數）。 |
| `tasks` | number | 全域提交任務上限；與 `success_tasks` **任一達標**即結束。 |
| `success_tasks` | number \| null | 全域成功上限。`null` = 不依成功數截斷（仍受 `tasks` 限制）。 |
| `batch_success_limit` | number | 單批成功上限；達標後重置程式內代理權重／統計並開下一批。累計成功／耗時保留。**不會**更換固定代理的真實出口 IP。 |
| `proxy` | object | 代理設定（正式使用必填）。 |
| `oauth2` | object | Graph OAuth2 設定。 |
| `temp_mail` | object | 可選：保護帳戶頁自動綁定輔助信箱。 |

### `captcha_strategy`

| 值 | 行為 |
| --- | --- |
| `0` | 全自動（驗證碼 + 進信箱 + OAuth）。 |
| `1` | 半自動：你手動過驗證碼，其餘自動。 |
| `2` | 驗證碼介面出現後交由人工；該任務程式不跑 OAuth。 |

### `proxy`

| 欄位 | 類型 | 說明 |
| --- | --- | --- |
| `mode` | string | `single` 使用 `single_port`；`multiple` 使用 `port_start`～`port_end` 連接埠池；`pool` 從 `pool_file` 順序讀代理（支援帳密 socks5，一視窗一代理）。 |
| `type` | string | 代理協定，如 `http`、`socks5`。 |
| `host` | string | 代理主機，如 `127.0.0.1`。**執行前請填寫。** |
| `single_port` | number | `mode=single` 時的連接埠。 |
| `port_start` | number | `mode=multiple` 時起始連接埠。 |
| `port_end` | number | `mode=multiple` 時結束連接埠（含）。 |
| `max_per_proxy` | number | 單一連接埠在行程內最多被選中次數；用滿後暫不選，全滿或批次重置後計數清零。 |
| `pool_type` | string | `mode=pool` 且 `pool_file` 行內未寫協定時使用的預設協定，如 `http`、`socks5`。 |
| `pool_file` | string | `mode=pool` 時的代理清單檔案；相對路徑按 `config.json` 同級目錄解析。 |
| `front_proxy` | string | `mode=pool` 時的前置出牆代理；牆內可填本地 `http://127.0.0.1:7897`，牆外直連可留空。 |

`pool_file` 每行一條代理，空行和 `#` 開頭的註解會跳過。支援以下格式：

```text
host:port
host:port:user:pass
user:pass@host:port
http://host:port
http://user:pass@host:port
socks5://host:port
socks5://user:pass@host:port
```

如果行內已經帶 `http://` 或 `socks5://`，會直接使用行內協定，不會再額外拼接 `pool_type`，避免出現重複協定前綴。行內未寫協定時，才使用 `pool_type` 作為預設協定。

### `oauth2`

| 欄位 | 類型 | 說明 |
| --- | --- | --- |
| `enable_oauth2` | bool | `false` 時註冊成功即可計成功，不拉 token。 |
| `client_id` | string | 授權與換 token 使用的用戶端 ID。 |
| `redirect_url` | string | 重新導向 URI（預設 `http://localhost`）。 |
| `Scopes` | string[] | 授權範圍，一般為 `offline_access` + Graph 預設範圍。 |

### `temp_mail`

僅在 Microsoft 出現 **「讓我們來保護你的帳戶」** 且啟用綁定時使用。

| 欄位 | 類型 | 說明 |
| --- | --- | --- |
| `enabled` | bool | `false` = 不自動綁定（出現保護頁時走跳過等邏輯）。 |
| `base_url` | string | 臨時信箱 API 根位址。不用則留空。 |
| `admin_password` | string | 建立地址用的管理員密碼。**勿公開。** |
| `domain` | string | 新建信箱網域。 |
| `name_prefix` | string | 本地部分前綴（可選）。 |
| `enable_prefix` | bool | 是否啟用 `name_prefix`。 |
| `code_timeout` | number | 等待驗證碼郵件逾時（秒）。 |
| `poll_interval` | number | 輪詢收件匣間隔（秒）。 |

---

## 專案用途

```text
產生信箱／密碼
  -> 開啟註冊頁並填表
  -> 按壓驗證碼
  ->（可選）綁定輔助信箱
  -> 進入 Outlook 信箱
  -> OAuth2（優先 cookie，失敗再新瀏覽器並可注入 cookie）
  -> 將 refresh_token 追加寫入 Results/oauth2.txt
```

OAuth 可處理：個人／工作帳戶選擇、保護帳戶、驗證電子郵件、保持登入「否」、同意授權與 code 擷取等。

---

## 主要功能

- 多代理並發註冊  
- 依批成功上限重置程式內權重  
- Cookie 優先 OAuth + 冷啟動／新環境後援  
- 可選 temp_mail 輔助信箱綁定  
- Ctrl+C：先彙總再關瀏覽器、清 profile  
- `log/` 進度與失敗分類  

---

## 上游與致謝

- [LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister) — 本專案二開來源  
- [Microsoft identity platform / Graph](https://learn.microsoft.com/en-us/graph/auth-v2-user) — OAuth2 與 Graph  
- [DrissionPage](https://github.com/g1879/DrissionPage) — 瀏覽器自動化（驅動本機 Chrome）  

---

## 授權條款

本專案採用 [MIT License](./LICENSE)。  
保留對上游 OutlookRegister 及所依賴 MIT 元件之署名說明。

---

## 友情連結

- [linux.do](https://linux.do)：**學AI，上L站！！！**
