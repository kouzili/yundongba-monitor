# 🎾 韵动吧 · 网球场自动监控捡漏

自动轮询「[韵动吧](https://www.sports8.com.cn)」网球场空余时段，发现空场后微信通知 + 可选自动下单预留。

签名本地生成，直接调用 API 查询排期，无需反复抓包。

## 功能

- **多场地监控**：支持同时监控多个网球场的排期，支持按关键词搜索添加场地
- **场地优先级**：拖拽排序，同一日期时间下优先通知/下单优先级高的场地
- **日期范围**：配置开始 / 结束日期，任意未来日期都能查（不再受签名限制）
- **自定义时段**：可配置目标时间段（如 18:00–21:00 晚场）
- **微信实时提醒**：发现空场立即推送到微信（Server酱）
- **自动下单**：可选开关，开启后自动发起预订（不实际付款，仅锁定场地）
- **Web 管理后台**：可视化配置、一键启停、实时日志

## 架构

```
┌─────────────────────────────────────────────────┐
│  sign.py（本地签名引擎）                          │
└──────────────────┬──────────────────────────────┘
                   │ 本地生成签名，直接调 API
┌──────────────────▼──────────────────────────────┐
│  app.py（Flask Web 后台 + 监控引擎）              │
│  · 场地搜索 / 查排期 / 下单                       │
│  · 定时轮询 + 微信通知                           │
└──────────────────┬──────────────────────────────┘
                   │
          ┌────────┼────────┐
      查排期 API   微信通知   自动下单
```

抓包（mitmproxy）仅在 **appsessionid 失效时** 用于刷新会话，日常监控完全不需要。

## 快速开始

### 环境要求

- macOS（Apple Silicon / Intel）
- Python 3.10+
- [mitmproxy](https://mitmproxy.org/)：`brew install mitmproxy`（仅刷新会话时需要）

### 1. 安装依赖

```bash
cd 网球场监控
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 配置 config.json

```bash
cp config.example.json config.json
```

编辑 `config.json`，填入以下必填项：

| 字段 | 说明 |
|------|------|
| `secret_api` / `secret_ydb` | 签名密钥（获取方式见下文「获取签名密钥」） |
| `appsessionid` | 登录会话令牌 |
| `userid` | 你的用户 ID |
| `serverchan_key` | Server酱 SendKey（微信通知用） |

### 3. 启动 Web 后台

```bash
bash start.sh                                    # 启动
open http://localhost:5100                       # 打开管理页面
```

在页面上：

1. **搜索添加场地**：输入关键词（如「古北」）→ 点「+ 添加」
2. **拖拽排序**：排前面的优先通知/下单
3. **日期范围**：选择开始日期 + 结束日期（默认下一个自然日）
4. **时段 + 间隔**：如 18:00–21:00，轮询间隔 30s
5. **自动下单开关**：默认关（只微信通知），勾选后自动抢场
6. 点「**开始监控**」，右侧日志实时显示轮询结果

## 获取签名密钥与会话

`config.json` 需要三个关键信息：签名密钥（`secret_api` / `secret_ydb`）、会话令牌（`appsessionid`）、用户 ID（`userid`）。

**签名密钥**：项目提供了辅助脚本，可自动定位并解出密钥，填入 `config.json` 即可：

```bash
.venv/bin/python3 reverse_wxapkg.py --search sign
```

**会话令牌（appsessionid 失效时，约数天一次）**：

1. 页面点「**启动抓包**」→ 记下代理 IP 和端口
2. iPhone WiFi 设置 → HTTP 代理 → 手动 → 填入 IP:8080
3. 打开韵动吧 App（或微信小程序）逛一下订场页面
4. 回页面点「**停止抓包**」→「**提取会话**」
5. **手机代理记得关掉**

## 微信通知配置

[Server酱](https://sct.ftqq.com/) 微信扫码注册，拿到 SendKey，填入 `config.json` 的 `serverchan_key` 字段。

## 项目文件

| 文件 | 说明 |
|------|------|
| `app.py` | Flask Web 后台 + 监控引擎 + API |
| `sign.py` | 签名 + API 客户端（查排期 / 下单 / 搜索） |
| `reverse_wxapkg.py` | 辅助脚本：获取签名密钥 |
| `templates/index.html` | 管理界面 |
| `start.sh` | 一键启动脚本 |
| `config.json` | 运行时配置（含密钥，已 gitignore） |
| `config.example.json` | 配置模板 |
| `monitor.py` | 命令行版监控脚本（可独立运行） |

## 命令行用法

```bash
# 快速测试签名与 API（读 config.json）
.venv/bin/python3 sign.py

# 命令行版监控（旧版，独立运行）
.venv/bin/python3 monitor.py --once
```

## 注意事项

- **密钥保密**：`config.json`（含签名密钥、session、SendKey）已加入 `.gitignore`，切勿提交公开
- **会话有效期**：`appsessionid` 约数天失效，失效后按上文刷新会话
- **轮询频率**：建议 ≥30s，避免触发风控
- **自动下单有风险**：会真实占场，默认关闭，请谨慎开启
- **合规提示**：本项目仅供个人学习与技术研究，请勿用于商业用途或干扰他人正常订场

## License

MIT