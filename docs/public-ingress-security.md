# 公网入口 IP 白名单

## 风险与边界

`81`、`10000`、`10001`（前端入口），以及 `9000`（生产 NexPoly）、`9001`
（开发 NexPoly/Agent）和 `9011`（OpenScience）当前都可能由 Docker 发布到宿主机
公网地址。对这类端口只配置 FastAPI CORS 或普通 UFW `INPUT` 规则并不充分：
CORS 不是认证机制，而 Docker 会先做端口 DNAT，转发流量通常不会经过宿主机的
普通 `INPUT` 链。

仓库提供的 `public_ingress_firewall.py` 在 nftables `prerouting` 阶段、Docker
DNAT 之前检查原始目标端口。来自非 loopback 网络接口、目标为本机且目标端口为
`81/9000/9001/9011/10000/10001` 的 TCP 流量，只有源地址命中白名单才放行，
其余 IPv4 和 IPv6 流量全部丢弃。本机通过 `127.0.0.1` 执行的健康检查不受影响。

该措施是网络层缓解，不替代身份认证、最小权限和凭据轮换。尤其是 `9001` 的
Agent 具备文件和命令工具时，应把它视为高权限管理入口。

## 准备白名单

复制模板到一个临时的 root 可读文件，并填写真实来源 CIDR：

```bash
cp ops/config/public-ingress-allowlist.conf.example /tmp/nexpoly-ingress.conf
chmod 0600 /tmp/nexpoly-ingress.conf
editor /tmp/nexpoly-ingress.conf
```

配置格式如下：

```dotenv
NEXPOLY_INGRESS_ALLOW_IPV4=203.0.113.25/32,198.51.100.0/24
NEXPOLY_INGRESS_ALLOW_IPV6=2001:db8:1234::25/128
```

- 优先填写管理员或 VPN 的固定公网出口地址；单个 IPv4/IPv6 地址分别使用
  `/32`、`/128`。
- 浏览器位于 NAT 后时，白名单应填写服务器实际看到的公网出口 IP，而不是
  客户端的 `192.168.x.x` 地址。
- 动态家庭宽带不适合直接加入长期白名单，建议先接入固定出口 VPN，或保持端口
  仅监听 loopback 并使用 SSH 隧道。
- `0.0.0.0/0` 和 `::/0` 会被工具拒绝；空白名单也会被拒绝，防止配置失误。
- 示例中的 `192.0.2.0/24`、`198.51.100.0/24` 和 `2001:db8::/32` 是文档
  保留地址，安装前必须替换。

先以普通用户校验，不会更改防火墙：

```bash
python3 scripts/public_ingress_firewall.py validate \
  --config /tmp/nexpoly-ingress.conf
```

## 安装与更新

安装需要 root 权限；安装器会把配置以 `root:root 0600` 保存到
`/etc/nexpoly/public-ingress-allowlist.conf`，安装开机服务并立即应用：

```bash
sudo ./scripts/install_public_ingress_firewall.sh \
  /tmp/nexpoly-ingress.conf
```

systemd 单元在网络和 Docker 启动前加载规则。nftables 更新使用单个原子事务；
新策略校验或提交失败时，旧策略保持生效。更新白名单时，修改一个新的临时文件，
重新执行同一安装命令即可。

查看服务和规则状态：

```bash
sudo systemctl status nexpoly-public-ingress-firewall.service
sudo /usr/local/libexec/nexpoly-public-ingress-firewall status
sudo nft list table inet nexpoly_public_ingress
```

验证必须至少使用两个外部网络：白名单内来源应能访问全部受保护端口，白名单外
来源应超时；不能只在服务器本机用 `curl 127.0.0.1` 作为公网拦截证据。本机检查
可用：

```bash
curl --fail http://127.0.0.1:9000/health
curl --fail http://127.0.0.1:9011/healthz
```

## 最小暴露方案与回滚

若没有任何远程用户必须直连，优先把端口绑定为 `127.0.0.1`：开发环境保持
`NEXPOLY_DEV_FRONTEND_BIND_ADDRESS=127.0.0.1`，OpenScience 设置
`OPENSCIENCE_UI_BIND=127.0.0.1`，再通过 SSH 隧道访问。生产入口也可在上游反向
代理具备认证和白名单时改为 loopback。

不要先删除防火墙规则再调整监听地址。安全回滚顺序是：先确认相关服务均已改为
loopback 或由另一个受控入口保护，再执行：

```bash
sudo systemctl disable --now nexpoly-public-ingress-firewall.service
sudo /usr/local/libexec/nexpoly-public-ingress-firewall remove --confirm-remove
```

停止 systemd 单元本身不会自动删除规则，这是为了避免一次误操作立即重新开放
公网端口。

## 已发生暴露后的处置

IP 白名单只能阻止后续未授权连接。若端口已在公网暴露并被复现，应同步完成：

1. 轮换 Agent、模型供应商、数据库及外部服务的已暴露凭据，撤销旧令牌；
2. 审查 Agent 对话、命令执行、下载、反向连接和异常出站流量日志；
3. 核查宿主机、容器、SSH key、systemd/cron 持久化及关键文件完整性；
4. 清理不应向普通使用者开放的历史对话，并为 Agent 增加独立认证与授权。
