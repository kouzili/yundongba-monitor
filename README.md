# 🎾 韵动吧 · 网球场自动监控捡漏

自动轮询「[韵动吧](https://www.sports8.com.cn)」网球场空余时段，发现空场后微信通知 + 可自动下单预留。

## 功能

- **多场地监控**：支持同时监控多个网球场的排期
- **按日期筛选**：每个场地可独立选择关注的日期
- **自定义时段**：可配置目标时间段（如 18:00–21:00 晚场）
- **微信实时提醒**：发现空场立即推送到微信（Server酱）
- **自动下单**：预置签名后可自动发起预订（不实际付款，仅锁定场地）
- **Web 管理后台**：可视化配置、一键启停、实时日志

## 架构

```
iPhone (韵动吧APP) ──代理──→ Mac (mitmproxy 抓包)
                                   │
                          API sign 提取
                                   │
                          Flask Web 后台 ← 用户浏览器
                                   │
                          ┌────────┼────────┐
                    定时轮询 API   微信通知   自动下单
```

## 快速开始

### 环境要求

- macOS（Apple Silicon / Intel）
- Python 3.10+
- [mitmproxy](https://mitmproxy.org/)：`brew install mitmproxy`
- iPhone 与 Mac 连接同一 Wi-Fi

### 1. 安装依赖

```bash
cd 网球场监控
python3 -m venv .venv
.venv/bin/pip install flask requests -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. iPhone 安装 mitmproxy CA 证书（仅首次）

1. Safari 访问 `http://mitm.it` → 下载配置描述文件
2. 设置 → 通用 → VPN 与设备管理 → 安装 mitmproxy
3. 设置 → 通用 → 关于本机 → 证书信任设置 → 开启 mitmproxy

### 3. 抓取 API 签名

```bash
bash start.sh                                    # 启动 Web 后台
open http://localhost:5100                       # 打开管理页面
```

1. 页面点「**启动抓包**」→ 记下显示的代理 IP 和端口
2. iPhone WiFi 设置 → HTTP 代理 → 手动 → 填入 IP:8080
3. 打开韵动吧 App → 逐个进入你想监控的场地 → 切换到你关心的日期
4. 回到页面点「**停止抓包**」→「**提取签名 & 同步**」
5. **手机代理记得关掉**（WiFi 设置 → HTTP 代理 → 关闭）

### 4. 配置 & 启动监控

- 页面左侧勾选要监控的 **场地** 和 **日期**
- 设置目标 **时间段** 和轮询 **间隔** → 保存配置
- 点「**开始监控**」→ 右侧日志面板实时显示轮询结果

### 5. 微信通知配置

[Server酱](https://sct.ftqq.com/) 微信扫码注册，拿到 SendKey。已在 `app.py` 中配置默认 key，也可在页面中修改 `config.json` 的 `serverchan_key` 字段。

## 项目文件

| 文件 | 说明 |
|------|------|
| `app.py` | Flask Web 后台 + 监控引擎 + API |
| `templates/index.html` | 管理界面 |
| `monitor.py` | 命令行版监控脚本（可独立运行） |
| `extract_signs.py` | 从 mitmproxy 抓包提取签名 |
| `start.sh` | 一键启动脚本 |
| `config.json` | 运行时配置（自动生成） |

## 命令行用法

```bash
.venv/bin/python3 monitor.py --once          # 单次查询
.venv/bin/python3 monitor.py                  # 持续轮询 (30s)
.venv/bin/python3 monitor.py --interval 15    # 每15秒
```

## 注意事项

- **签名有效期**：appsessionid 约数小时~一天失效，重新抓包即可刷新
- **证书只需装一次**：后续抓包不用重装 iPhone 证书
- **代理只开抓包时**：抓完立刻关，否则手机正常上网受影响
- **轮询频率**：建议 ≥30s，避免触发风控
- **自动下单**：需预置对应场次的签名，否则仅通知不抢

## License

MIT
