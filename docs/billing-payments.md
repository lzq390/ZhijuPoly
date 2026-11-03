# 月费会员支付部署

开发原生 launcher 使用独立的会员 schema opt-in：设置 `NEXPOLY_DEV_MEMBERSHIP_ENABLED=true` 保持 `membership-0025`，再按需设置 `NEXPOLY_DEV_BILLING_SIMULATION_ENABLED=true` 开启模拟支付。模拟支付需要明确设置 `NEXPOLY_DEV_BILLING_MONTHLY_PRICE_FEN`（例如 `9900`，表示测试展示价 ¥99）。两个开关默认关闭；兼容原有仅设置模拟支付为 `true` 的配置，此时未指定的会员开关自动为 `true`。显式关闭会员开关却启用模拟支付会拒绝启动。它们要求已开启开发用户隔离和显式 `nexpoly_dev_` 前缀会话 cookie；不支持 frozen cohort。不要将这些选择项写入只允许研究服务字段的私有 research dotenv。

会员 opt-in 将 `docker-compose.billing-simulation-dev.yml` 同时加入 CPU/GPU 配置及构建配置哈希，固定真实支付关闭、真实渠道为空、注册开启，健康检查严格要求 `membership-0025`；模拟支付由自己的开关控制。必须先按停机、备份、迁移及校验流程应用 0025，再启动相应镜像。launcher 不会自动迁移或探测数据库版本。GPU operator 和 controller 恢复时冻结会员、模拟支付及价格选择，后续 dotenv 更改不会覆盖恢复配置；变更选择需按既有维护流程重新启动 operator/session。

已迁移环境应在人工启动使用的持久开发配置中明确保留 `NEXPOLY_DEV_MEMBERSHIP_ENABLED=true`。暂停模拟支付时只将 `NEXPOLY_DEV_BILLING_SIMULATION_ENABLED=false`，保留会员开关；价格可以清空或保留有效值，注册仍开启，真实支付仍关闭。此时 CPU 启动、GPU 恢复和健康检查继续使用 0025 overlay。不得同时删除会员 opt-in 或退回 0024 发布配置；从未 opt-in 的环境保留原有 schema 边界，不会被自动升级。overlay 内容变更仍需按原生构建配置哈希校验重新构建，不得重标记旧镜像。

支付适配器接入微信 Native APIv3 和支付宝当面付 `alipay.trade.precreate`。购买一次月费会员产生一个商户订单，由用户扫码支付；这不是自动扣款签约，不会在下月自动从用户账户扣费。月费价格必须由部署者明确配置为人民币分，不提供默认商业价格。

## 权限与会员有效期

- 游客和新注册普通用户拥有相同的业务功能限制；普通用户另可登录、查看自己的订阅与订单、购买会员。
- 支付经后端验签及订单核对后，普通用户获得原会员可用的业务功能。实验室、运维等原有专门授权仍按既有规则判断。
- 迁移时已有会员（包括原先默认会员的账号）标记为存量授权，保留原权限且不会自动到期；原先明确为 `free` 的用户仍为普通用户。存量授权和管理员指定权限的账号不显示购买入口，也不能被下单收费。
- 权限判定顺序为管理员指定权限、存量授权、有效订阅、普通用户。保留原管理员 tier CLI，并将其变更同步到明确的管理员授权字段。
- 一次付款增加一个上海时区的自然月，短月份按月末截断；未到期续费从原到期时间延长，已到期则从本次入账时间开始。到期后恢复普通用户业务权限，登录、历史数据和已付订单保留。已获准启动的任务继续执行，排队任务启动前重新校验权限。
- 本期不调整或开启使用额度，不修改历史 usage 账本及预算。订阅有效期只控制功能访问；后续引入会员额度时，需要另行设计订阅与额度 tier 的映射。

用户可在 `/account/subscription` 查看状态、选择微信或支付宝、扫码付款及手动续费。成功入账后刷新原会话权限，不要求重新登录；前端保留当前工作区草稿并支持返回原功能。

## 数据库迁移与发布顺序

新增迁移为 `0025_membership_subscriptions`，包含订单、付款事件、订阅期限和存量授权。它只接受完整且校验和匹配的 `0024` 前缀，通用 expand 命令不会自动应用这一步。按项目既有备份及发布流程，先将数据库准备到 `0024`，再使用本版本代码执行专用命令。`AUTH_ADMIN_POSTGRES_DSN` 需由部署环境注入，不能将含凭据的 DSN 写进命令或提交文件。

在项目根目录执行（使用已安装本版本后端依赖的 Python）：

```bash
# 只读检查，不写数据库
PYTHONPATH=backend:. python -m app.billing_migrations

# 执行一次迁移；重复执行会检查已应用版本，不重复授予权限
PYTHONPATH=backend:. python -m app.billing_migrations --apply
```

预览成功返回 `written: false` 和 `would_apply: true`；已应用时返回 `already_applied: true`。应用在同一事务中锁定存量账号及旧 tier 快照、创建订阅 schema 并记录校验和。新账号由数据库触发器原子建立普通用户记录，注册失败会一起回滚；不存在“注册后补写 free”的会员权限空窗。

API 与 worker 应一起发布到支持 `0025` 的版本，沿用已有维护／排空流程；旧版本的严格迁移台账可能拒绝新增 schema。先迁移、部署并检查 readiness，再配置价格、渠道和密钥，最后打开注册／购买开关。启动注册、购买或任何支付渠道时都会强制检查订阅 schema；不能跳过迁移直接启用。

## 配置与密钥

无密钥模板在 [`config/examples/billing.env.example`](../config/examples/billing.env.example)。只将实际启用的渠道填入 `NEXPOLY_BILLING_PROVIDERS`，取值为 `wechat`、`alipay`，多个值用逗号分隔。配置列出的每个渠道都必须完整；缺少商户身份、私钥文件、可信支付平台公钥或 HTTPS 回调地址时拒绝配置。

| 环境变量 | 含义 |
| --- | --- |
| `NEXPOLY_BILLING_ENABLED` | 默认 `false`；是否接受新购买。 |
| `NEXPOLY_BILLING_SIMULATION_ENABLED` | 默认 `false`；仅隔离开发环境可用的明确模拟支付，不能与真实购买／支付渠道同时启用。 |
| `NEXPOLY_BILLING_REGISTRATION_ENABLED` | 默认 `false`；独立控制新账号注册。 |
| `NEXPOLY_BILLING_MONTHLY_PRICE_FEN` | 明确的月费金额，正整数人民币分；不配置价格时不能启用购买。 |
| `NEXPOLY_BILLING_PROVIDERS` | 明确列出的已配置渠道，无默认渠道。 |
| `NEXPOLY_WECHAT_APP_ID` | 与商户号绑定的微信应用 ID。 |
| `NEXPOLY_WECHAT_MERCHANT_ID` | 微信支付普通商户号。 |
| `NEXPOLY_WECHAT_MERCHANT_SERIAL` | 商户 API 证书序列号，用于请求签名。 |
| `NEXPOLY_WECHAT_PRIVATE_KEY_FILE` | 商户 RSA 私钥 PEM 绝对路径，至少 2048 位。 |
| `NEXPOLY_WECHAT_API_V3_KEY_FILE` | 32 字节 ASCII APIv3 密钥文件绝对路径，允许末尾换行。 |
| `NEXPOLY_WECHAT_PLATFORM_KEY_ID` | 商户平台取得的 `PUB_KEY_ID_…`；或可信平台证书序列号。 |
| `NEXPOLY_WECHAT_PLATFORM_PUBLIC_KEY_FILE` | 对应微信支付公钥 PEM 或可信平台证书 PEM 绝对路径。 |
| `NEXPOLY_WECHAT_NOTIFY_URL` | `https://实际域名/api/v1/billing/webhooks/wechat`。 |
| `NEXPOLY_ALIPAY_APP_ID` | 已开通当面付权限的支付宝应用 ID。 |
| `NEXPOLY_ALIPAY_SELLER_ID` | 本商户收款账号的支付宝用户 ID，通常为 `2088…`。 |
| `NEXPOLY_ALIPAY_PRIVATE_KEY_FILE` | 应用 RSA 私钥 PEM 绝对路径，至少 2048 位。 |
| `NEXPOLY_ALIPAY_PUBLIC_KEY_FILE` | 本应用对应的支付宝公钥 PEM 绝对路径；不是应用公钥。 |
| `NEXPOLY_ALIPAY_NOTIFY_URL` | `https://实际域名/api/v1/billing/webhooks/alipay`。 |

敏感材料使用只读 secret 文件挂载，限定后端运行身份读取，不能放入 Git、前端环境变量、镜像层或日志。文件路径必须是后端进程实际可读的绝对路径，宿主机存在路径不代表容器可读。示例不创建密钥、不签约商户、不修改运行环境，也不发起真实支付。

现有默认 compose 不会因为填写上述变量而自动挂载密钥。开发环境或经审核的独立部署可以显式添加支付 overlay；下列片段仅展示只读挂载方式，不是生产发布文件，也不代表生产发布契约已允许该挂载：

```yaml
services:
  backend:
    volumes:
      - type: bind
        source: /absolute/private/nexpoly-payment-keys
        target: /run/nexpoly-payment-keys
        read_only: true
    environment:
      NEXPOLY_BILLING_ENABLED: "false"
      NEXPOLY_BILLING_REGISTRATION_ENABLED: "false"
      NEXPOLY_WECHAT_PRIVATE_KEY_FILE: /run/nexpoly-payment-keys/wechat-merchant.pem
      NEXPOLY_WECHAT_API_V3_KEY_FILE: /run/nexpoly-payment-keys/wechat-api-v3-key
      NEXPOLY_WECHAT_PLATFORM_PUBLIC_KEY_FILE: /run/nexpoly-payment-keys/wechat-platform.pem
      NEXPOLY_ALIPAY_PRIVATE_KEY_FILE: /run/nexpoly-payment-keys/alipay-application.pem
      NEXPOLY_ALIPAY_PUBLIC_KEY_FILE: /run/nexpoly-payment-keys/alipay-platform.pem
```

该片段还需显式转发商户身份、回调地址、渠道和价格配置。生产部署有严格的发布／挂载清单校验，必须通过其审核与验证流程接纳支付配置，不能直接把开发 overlay 叠到生产文件来绕过契约。代码提交与示例不会自动开启注册或购买，也不会迁移运行数据库；数据库变更和部署启用需分别按项目发布流程执行。

当前支持微信普通商户直连与支付宝普通公钥 RSA2 模式；支付宝证书模式和服务商子商户模式不在本适配范围。支付宝需要使用对应公钥模式配置，不能将支付宝根证书替代支付宝公钥。

微信验签仅信任运维明确配置的公钥 ID／证书，不从未验签的请求自动获取或信任新密钥。平台公钥更换或平台证书轮换时，应提前更新配置并重启相关进程；未知序列号会拒绝验签，由平台重试和主动查单恢复。平台证书在加载时检查序列号和有效期。优先使用微信支付提供的公钥模式减少平台证书轮换操作。

生产接口固定为 `https://api.mch.weixin.qq.com` 和 `https://openapi.alipay.com/gateway.do`，不支持通过环境变量替换网关、关闭 TLS／验签或开启 mock 付款。代理环境变量不参与支付 HTTP 客户端；部署网络需允许后端直连这两个 HTTPS 域名。代码测试的 `httpx.MockTransport` 仅在测试构造器中注入。

## 回调、查单与关单

反向代理应将两个回调路径转发给后端，允许平台通过公开 HTTPS POST，不重定向，也不要求用户登录 Cookie 或 CSRF Token。这些路径仍必须经过支付验签；不能以来源 IP、前端“支付完成”按钮或浏览器跳转作为付款证据。不要解析再序列化微信请求体，验签必须使用收到的原始字节。

微信响应和回调均验证 RSA-SHA256 签名、公钥 ID、时间戳（允许 5 分钟差异），回调随后进行 AES-256-GCM 解密。部署机器需正确同步时间。支付宝请求使用 RSA2 签名，同步响应验证原始业务 JSON 片段，异步回调验证除 `sign`／`sign_type` 外的排序参数；重复字段拒绝接受。

验签通过只证明平台消息来源。业务入账事务仍必须核对本地订单的渠道、订单号、订单冻结金额、CNY 币种、商户身份与交易号，并确保一个支付交易／订单最多增加一次会员时长。适配器返回的 `amount_fen` 是订单总额；微信 `payer_total` 是优惠后实际付款金额，不能用它替代订单总额比对。

订单支付窗口固定为创建后 15 分钟。订单 ID 在数据库可以使用 UUID，发送至平台使用同一个 UUID 的 32 字符 `hex`，不截断或重新生成。平台回调保留这个商户订单号，业务层按 UUID 规范化后匹配本地订单。价格变化只影响之后的新订单；历史未完成订单仍按其创建时冻结价格核对。

微信成功回调返回 HTTP 200 或 204；支付宝返回 HTTP 200 和纯文本 `success`。必须在可靠记录支付及幂等处理结果之后才确认成功，失败时返回可重试错误；如使用异步处理，需先完成可靠事件落库。

回调可能重复、延迟或最终遗漏，应持续主动查单。HTTP 超时、查单暂不可用、验签失败都不能视作付款失败或订单已关闭。复用原商户订单号查明状态，避免以新订单号重试造成重复付款。`paid` 结果必须含有效交易号及正整数金额；尚未在平台创建的订单可返回 `pending` 且金额为空，绝不能据此开通会员。

微信 `time_expire` 限制付款截止时间，但不会自动将订单状态变为 `CLOSED`。订单到期应先查单，再对仍未支付的订单调用关闭订单接口；关闭失败要保留待核对状态并再次查询，防止关闭与付款竞争。不要只根据本地时间把未知订单认定为关闭。退款状态不作为新的付款接受；退款与会员权益回收需要独立业务流程。

暂停购买时只关闭 `NEXPOLY_BILLING_ENABLED`，保留已售渠道、密钥、回调路由与查单任务，使已发出的支付二维码及迟到回调可以完成核对。`available_providers` 表示完整配置的渠道，是否展示购买入口还应同时检查 `enabled`。

订单冻结创建时的应用 ID 和商户身份。更换收款商户前应完成旧订单核对；同一渠道的新商户配置不能接管旧商户订单。当前配置与订单商户不符时，主动查单会保留待处理状态，不能当作已关闭订单。后台每 30 秒处理一批待核对订单，浏览器也可触发查单；数据库限制同一订单至少间隔 10 秒查询。发布维护期间，支付写入及查询触发的补偿处理服从现有排空机制，平台回调可在恢复后重试。

## 测试与上线验证

开发环境可显式开启 `NEXPOLY_BILLING_SIMULATION_ENABLED=true` 验证注册、下单、开通与续费。启动时同时要求 `GPU_BROKER_ENVIRONMENT=dev`、显式 `AUTH_COOKIE_NAME` 以 `nexpoly_dev_` 开头、真实购买开关关闭、`NEXPOLY_BILLING_PROVIDERS` 为空，且已配置正整数月费。价格仅用于模拟订单展示，不会收款。

模拟 plans 和订单都返回 `simulation: true`，电脑和手机页面均明确标注“开发模拟支付”和“不会扣款”。电脑下单后生成指向本站手机确认页的二维码：使用微信或支付宝扫一扫打开页面，核对渠道和展示金额，点击确认后由后端记录模拟付款并开通会员；电脑通过订单轮询自动刷新原会话的会员权限。该二维码是 NexPoly 的开发确认链接，不是官方收款码，不调用支付平台，也不产生真实交易。用户可用页面提供的链接在另一个浏览器中验收同一流程。

验收时电脑应通过手机可访问的开发地址打开站点，二维码使用当前页面的 origin，不接受浏览器传入任意回调地址。通过 `localhost` 或 `127.0.0.1` 打开的页面会提示手机不可访问；手机也需能访问开发站点的网络和端口。

手机确认页为 `/billing/simulate-pay#token=…`，不要求登录，也不加载科研工作区。二维码凭证由订单 ID 和 256 位随机值组成，有效期与订单的 15 分钟支付窗口一致；凭证放在 URL fragment 中，不随页面请求、访问日志或 Referrer 发送。页面仅通过 JSON POST 将凭证发送给 `POST /api/v1/billing/simulation/checkout`（只读预览）和 `POST /api/v1/billing/simulation/confirm`（明确确认），请求不携带登录 Cookie。接口校验允许的 Origin 和 JSON 内容类型，仅返回渠道、金额、订单尾号、状态及有效期等验收信息，不暴露账号身份。持有二维码链接的人可以确认该笔开发订单，因此不要将完整链接或二维码写入日志或公开分享。

扫码、链接预览和页面刷新都不会开通会员。明确确认后，后端核对凭证、模拟开关、订单渠道和金额、订单有效期及账号状态，再通过原有原子入账流程更新开发数据库；同一订单重复／并发确认最多延长一个自然月。原登录用户的 `POST /api/v1/billing/orders/{order_id}/simulate-payment` 接口保留兼容，仍使用 Origin、会话上下文和 CSRF 校验。模拟渠道始终拒绝支付平台回调，真实订单与模拟订单不能互相开通。关闭模拟开关后，手机模拟接口不可用，历史订单仍保留模拟标识，已开通的开发会员按原到期时间有效。此次扫码流程使用现有订单数据，不新增数据库迁移，也不改变额度、存量授权或已有订阅。

[`backend/tests/test_billing_providers.py`](../backend/tests/test_billing_providers.py) 使用新生成的合成 RSA 密钥、真实签名／AES-GCM 密文和内存 HTTP transport，覆盖请求签名、响应原文验签、回调篡改、错商户、错金额、错币种、重复字段、过期签名、未知公钥和网络超时。测试不会读取生产密钥、访问支付平台或创建真实订单。

仅运行支付适配单元测试时，可使用已安装 `httpx`、`cryptography`、`pytest` 的 Python 环境：

```bash
PYTHONPATH=backend python -m pytest --noconftest -q backend/tests/test_billing_providers.py
```

首次真实支付仍需在商户资质、正式价格、HTTPS 域名、密钥和数据库迁移准备完毕后，由部署者完成小额人工验收：扫码支付成功、重复回调、页面关闭后查询恢复、跨价格变更旧订单、付款与关单竞争、关闭购买后的迟到回调，以及最终会员有效期与商户账单对应。单元测试通过不代表商户签约或公网回调已完成。

## 官方协议依据

适配依据核对于 2026-11-02：

- [微信 Native 下单](https://pay.wechatpay.cn/doc/v3/merchant/4012791877)、[商户订单号查询](https://pay.wechatpay.cn/doc/v3/merchant/4012791880)、[关闭订单](https://pay.wechatpay.cn/doc/v3/merchant/4012791881)。
- [微信支付成功回调通知](https://pay.wechatpay.cn/doc/v3/merchant/4012791882)、[APIv3 签名与验签](https://pay.wechatpay.cn/doc/v3/merchant/4012365342)、[Native 接入易错点](https://pay.wechatpay.cn/doc/v3/merchant/4054257695)。
- [支付宝官方 Python SDK](https://github.com/alipay/alipay-sdk-python-all)，其中 `DefaultAlipayClient` 定义 RSA2 请求签名和原始业务响应验签，`AlipayTradePrecreateModel`／`AlipayTradeQueryResponse` 对应预创建与查询字段。
- [支付宝异步返回结果验签说明](https://developer.alibaba.com/docs/doc.htm?articleId=103296&docType=1&treeId=194)。
