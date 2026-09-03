<h1 align="center">OutlookRegister</h1>

<p align="center">
  Outlook / Hotmail 自动注册，并获取 Microsoft Graph OAuth2 <code>refresh_token</code>（基于 DrissionPage 驱动本机 Chrome 自动化）。
</p>

<p align="center">
  <a href="./README.md">English</a> ·
  <a href="./README.zh-CN.md"><b>简体中文</b></a> ·
  <a href="./README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
  <img alt="DrissionPage" src="https://img.shields.io/badge/Browser-DrissionPage-4B5563">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-blue">
</p>

> 本项目基于 **[LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister)** 二开。  
> 增强点包括：可选辅助邮箱绑定、更完整的 OAuth 中间页处理、cookie 优先授权、分批调度，以及 Ctrl+C 中断后的汇总与清理。

---

## 使用教程

### 1. 环境要求

- 建议 Python 3.10+  
- **本机已安装 Google Chrome**（DrissionPage 驱动系统 Chrome，非 bundled Chromium）  
- 可用的 HTTP/SOCKS 代理（强烈建议）  
- 若开启辅助邮箱绑定：兼容的临时邮箱 / CF Temp Mail 类 API  

### 2. 安装

```bash
git clone <本仓库地址>
cd OutlookRegister
pip install -r requirements.txt
# 无需再单独安装浏览器内核；确保本机已装 Chrome 即可
# 如 Chrome 不在默认路径，可在 config.json 的 browser.path 指定 chrome.exe
```

### 3. 配置

```bash
# Windows
copy config.example.json config.json

# Linux / macOS
cp config.example.json config.json
```

编辑 `config.json`：至少填好 **proxy**。对外分享时不要带上真实密钥。

### 4. 运行

```bash
python main.py
```

成功账号会**追加**写入 `Results/oauth2.txt`：

```text
邮箱----密码----client_id----refresh_token
```

日志在 `log/`。按 **Ctrl+C** 可中断：先写汇总，再关闭浏览器并清理 profile。

---

## `config.json` 字段说明

模板见 `config.example.json`（与空的 `config.json` 结构一致）。两个文件都支持**整行 `//` 注释**（程序读取时会先剥掉再解析；随附 `.vscode/settings.json` 把它们按 JSONC 处理，编辑器不再报错）。

### 顶层字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `email_suffix` | string | 注册邮箱后缀，如 `@outlook.com` 或 `@hotmail.com`。 |
| `headless` | bool | `false` 显示浏览器窗口；`true` 无头。 |
| `bot_protection_wait` | number | 注册页同意按钮前的等待基准（秒）；表单字段采用随机人类化节奏。 |
| `max_captcha_retries` | number | 验证码按压总尝试次数（如 `2` = 按压两次都不过就弃用当前窗口；single 模式则判定此IP不可用并结束运行）。 |
| `captcha_strategy` | number | 验证码/交接策略，见下表。 |
| `px_solve_mode` | string | PX「按住」验证码解法：`"hold"`（默认，成功率稳定）或 `"a11y"`（无障碍备用：点无障碍小人图标→等进度条走完→点长条确认）。 |
| `concurrent_flows` | number | 并发线程数（同时打开的浏览器任务数）。 |
| `tasks` | number | 全局提交任务上限；与 `success_tasks` **任一达标**即结束。 |
| `success_tasks` | number \| null | 全局成功上限。`null` = 不按成功数截断（仍受 `tasks` 限制）。 |
| `batch_success_limit` | number | 单批成功数上限；达到后重置程序内代理权重/统计并开下一批。累计成功/耗时保留。**不会**更换固定代理的真实出口 IP。 |
| `browser` | object | 可选：浏览器路径 / 窗口尺寸。见下表。 |
| `proxy` | object | 代理配置（正式使用必填）。 |
| `oauth2` | object | Graph OAuth2 配置。 |
| `temp_mail` | object | 可选：保护帐户页自动绑定辅助邮箱。 |

### `browser`（可选）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `path` | string | Chrome/Edge 可执行文件路径；留空则 DrissionPage 自动探测系统 Chrome。 |
| `window_size` | [w, h] \| null | 固定窗口尺寸（内外一致，避免 viewport 随机化指纹）；`null` 则在若干真实分辨率间随机。 |
| `user_data_root` | string | 兜底清理用的 profile 根目录（DrissionPage 实际用临时目录，退出随浏览器删除）。 |

### `captcha_strategy`

| 值 | 行为 |
| --- | --- |
| `0` | 全自动（验证码 + 进邮箱 + OAuth）。 |
| `1` | 半自动：你手动过验证码，其余自动。 |
| `2` | 验证码界面出现后交给人工；该任务程序不跑 OAuth。 |

### `proxy`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `mode` | string | `single` 使用 `single_port`；`multiple` 使用 `port_start`～`port_end` 端口池；`pool` 从 `pool_file` 顺序读代理（支持账密 socks5，一窗口一代理）。 |
| `type` | string | 代理协议，如 `http`、`socks5`。 |
| `host` | string | 代理主机，如 `127.0.0.1`。**运行前请填写。** |
| `single_port` | number | `mode=single` 时的端口。 |
| `port_start` | number | `mode=multiple` 时起始端口。 |
| `port_end` | number | `mode=multiple` 时结束端口（含）。 |
| `max_per_proxy` | number | 单个端口在进程内最多被选中次数；用满后暂不选，全满或批次重置后计数清零。 |
| `pool_type` | string | `mode=pool` 且 `pool_file` 行内未写协议时使用的默认协议，如 `http`、`socks5`。 |
| `pool_file` | string | `mode=pool` 时的代理清单文件；相对路径按 `config.json` 同级目录解析。 |
| `front_proxy` | string | `mode=pool` 时的前置出墙代理；墙内可填本地 `http://127.0.0.1:7897`，墙外直连可留空。 |

`pool_file` 每行一条代理，空行和 `#` 开头的注释会跳过。支持以下格式：

```text
host:port
host:port:user:pass
user:pass@host:port
http://host:port
http://user:pass@host:port
socks5://host:port
socks5://user:pass@host:port
```

如果行内已经带 `http://` 或 `socks5://`，会直接使用行内协议，不会再额外拼接 `pool_type`，避免出现重复协议前缀。行内未写协议时，才使用 `pool_type` 作为默认协议。

### `oauth2`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `enable_oauth2` | bool | `false` 时注册成功即可计成功，不拉 token。 |
| `client_id` | string | 授权与换 token 使用的客户端 ID。 |
| `redirect_url` | string | 重定向 URI（默认 `http://localhost`）。 |
| `Scopes` | string[] | 授权范围，一般为 `offline_access` + Graph 默认范围。 |

### `temp_mail`

仅在微软弹出 **「让我们来保护你的帐户」** 且开启绑定时使用。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `enabled` | bool | `false` = 不自动绑定（出现保护页时走跳过等逻辑）。 |
| `base_url` | string | 临时邮箱 API 根地址。不用则留空。 |
| `admin_password` | string | 创建地址用的管理员密码。**勿公开。** |
| `domain` | string | 新建邮箱域名。 |
| `name_prefix` | string | 本地部分前缀（可选）。 |
| `enable_prefix` | bool | 是否启用 `name_prefix`。 |
| `code_timeout` | number | 等待验证码邮件的超时（秒）。 |
| `poll_interval` | number | 轮询收件箱间隔（秒）。 |

---

## 项目用途

```text
生成邮箱/密码
  -> 打开注册页并填表
  -> 按压验证码
  ->（可选）绑定辅助邮箱
  -> 进入 Outlook 邮箱
  -> OAuth2（优先 cookie，失败再新浏览器并可注入 cookie）
  -> 将 refresh_token 追加写入 Results/oauth2.txt
```

OAuth 可处理：个人/工作帐户选择、保护帐户、验证电子邮件、保持登录「否」、同意授权与 code 捕获等。

---

## 主要功能

- 多代理并发注册  
- 按批成功上限重置程序内权重  
- Cookie 优先 OAuth + 冷启动/新环境兜底  
- 可选 temp_mail 辅助邮箱绑定  
- Ctrl+C：先汇总再关浏览器、清 profile  
- `log/` 进度与失败分类  

---

## 上游与致谢

- [LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister) — 本项目二开来源  
- [Microsoft identity platform / Graph](https://learn.microsoft.com/en-us/graph/auth-v2-user) — OAuth2 与 Graph  
- [DrissionPage](https://github.com/g1879/DrissionPage) — 浏览器自动化（驱动本机 Chrome）  

---

## 开源协议

本项目采用 [MIT License](./LICENSE)。  
保留对上游 OutlookRegister 及所依赖 MIT 组件的署名说明。

---

## 友情链接

- [linux.do](https://linux.do)：**学AI，上L站！！！**
