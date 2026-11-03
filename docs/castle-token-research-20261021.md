# Castle token 研究记录

日期：2026-10-21。对象是 `accounts.x.ai` 注册页自己生成的 `castleRequestToken`，对照手动成功 HAR 和 Patchright 实发请求。

本文只记录生成链路、长度差的来源，以及已经留在代码里的补齐。不收录 token 原文、代理口令、邮箱 refresh token。

## 材料

- 手动成功 HAR：`/home/xunflash/projects/grok/local/manual-signup-20261020.har`
- 发码请求是 HAR 条目 2128，时间 `2026-10-20T08:27:35`，`POST /api/auth/send-verification-code`，HTTP 200
- 建号请求是 HAR 条目 2181，时间约 77 秒后，用的是一条新 token，不是发码那条
- 页面 SDK：`https://accounts.x.ai/_next/static/chunks/02bh2mv7efkj1.js`（webpack 模块 `598821`，约 707222 字节）。`CastleProvider` 默认 stable 包是 `29w_xw4f2d2ae.js`，成功 HAR 实际加载的是 next 包 `02bh2mv7efkj1.js`。两份只差一个 debug id，事件记录和加密是同一套
- 面板实现：`nobrowser_protocol/patchright_engine.py`
- 远程运行副本：`/data/claw-relocated/releases/panel-2061dec`，浏览器是 `/usr/bin/chromium` 145.0.7632.159，有头，`DISPLAY=:121` 的 Xvfb
- HAR 浏览器：Chrome 145，Linux，UA 为 `Chrome/145.0.0.0`。高熵版本是 `145.0.7632.159`

发码体只有两个键：`email`、`castleRequestToken`。Turnstile 不在发码体里，在建号体里。面板日志 `castle how=page_send_code_request` 表示长度来自页面自己的 XHR，不是 Python 截断或另造的字符串。

## Token 外形

线上字符串是：

```text
8字符前缀 | base64(明文包装)
```

解码后的布局：

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 2 | 时间戳 UTF-8 长度，小端。成功样本都是 36 |
| 2 | 36 | `tf(Date.now().toString())` 再 `btoa`。可逆，发码样本解出 `1792484855207` |
| 38 | 24 | 加密头。前 8 字节是两个大端 uint32，分别是明文长度的两半，加起来等于密文长度 |
| 62 | 明文等长 | 流加密后的压缩正文 |

`tf` 对每个字符做 16 位旋转、加常量 `50305`、再旋转，然后每个字符打成两个字节。加密是按索引生成的 XOR 流，不依赖明文本身，种子来自时间戳字符串。同一时间戳下，用全 0 明文再加密一次就能取出密钥流，从而解开 HAR。压缩是 `CompressionStream("deflate-raw")`，Python 侧用 `zlib.decompress(data, -15)`。

解压后的明文是自定义编码的 765 元数组，开头 varint 是 `fd 05`（765）。标签：

| tag | 含义 |
|---|---|
| 0 | 空 |
| 1 | UTF-16 字符串 |
| 2 | 原始字节 |
| 3 | 4 字节小端整数 |
| 4 | 8 字节大端 float64 |
| 5 / 6 | false / true |
| 7 | 数组 |
| 8 | `JSON.stringify` 后再按 UTF-16 |
| 9 | 单字节字符串 |
| 10 | zigzag 整数 |

整数落在 int32 里用 tag 10，落到 `0x80000000..0xffffffff` 用 tag 3。同一个探针两边数值不同时，编码宽度会差 1 到 3 字节。这解释了总长度里那些零碎的 ±1。

## 页面上的生成链路

发码不是面板拼出来的。表单提交走到页面里的：

`mintCastleStepToken` → `sdk.createRequestToken()`，不传参数。

`flow`、`email`、`posture` 只决定超时和日志。`signUp.sendCode` 默认模式是 `"on"`，对应 closed：初始化 10 秒、mint 10 秒。模式 `"on"` 时 SDK 抛错会原样抛出，空 token 不会发请求。

`CastleProvider` 加载 SDK 后调用 `configure({pk})`，再 `createRequestToken()`。SDK 导出就是这两个函数。

发码前 HAR 里没有发往 castle.io 的请求。token 在浏览器里生成。

## 为什么 Patchright 一开始更短

面板早期实发长度大约 5661–5925。HAR 发码是 6045（解码 4525，压缩明文 4463）。建号那条是 6369，比发码长 324 字符，所以真实浏览器自己的长度也不是常数。

面板没有把字符串切片。长度差在压缩前的明文，主要是少采到的指纹，不是加密格式不同。

事件日志有 511 条上限。同一输入框上连续的按键会被合并，所以把邮箱逐字打完不会按字符数把日志拉长。两次事件间隔超过 `ON = 1000` 毫秒时，会插入一条间隔记录。单纯 `mouse.move` 主要进 `pointerrawupdate` 那条采样缓冲，不会按比例进入 `ub(uD())` 这段事件日志。

## 明文里对过的字段

下面的编号是解压后 765 元数组的下标。长度是该字段的 payload 长度。

| 字段 | 来源 | HAR | 早期 Patchright | 补齐后 |
|---|---|---|---|---|
| 63 | SharedWorker 返回数组下标 2，`Yc` | 202 | 20 字节失败占位 | 202 |
| 215 | 同一类 Worker 结果 | 188 | 20 | 188 |
| 645 | SharedWorker 返回数组下标 3，`zi` | 154 | 20 | 154 |
| 242 | SharedWorker 返回数组下标 10，`G6`，高熵完整版本 | `145.0.7632.159` | `92ss6wlksf` | `145.0.7632.159` |
| 324 | 另一个专用 Worker `u6` / `PY()`，读 `userAgentData` | `Linuxx86` | 30 字符占位 | `Linuxx86` |
| 116 | `getComputedStyle(document.body)` 的样式属性列表，经 `ei` / `VK` | 960 | 曾短到 808 | 960 |
| 3 | `document.referrer`，经 `Y_` / `eL` | 34 | 0 | 34 |
| 98 | `n(3)+n(t长)+n(u长)+n(f长)+t+u+f`。`u` 是 SDK 脚本 URL，103 字符，两边相同。`f` 是 `ub(uD())` 事件日志 | 事件日志 92 | 约 48 | 78–82 |

SharedWorker 脚本在 Blob 里，`onconnect` 后等页面发 `"start"`，再读 `navigator.userAgent`、`userAgentData.brands`，并调用 `getHighEntropyValues(["architecture","bitness","model","platformVersion","uaFullVersion","fullVersionList"])`。SDK 原来只等 `mJ = 1500` 毫秒，回包经常晚于这个时间，三个长字段就落到同一段 20 字节占位：`13cf1397159f159f13b715bf1567155f159f1537`。

平台和架构不在这个 SharedWorker 里。`PY()` 再开一个专用 Worker（`u6`），同样 1.5 秒超时。超时后 `Gt` / `Gn` 返回编码占位，而不是 `Linux` 和 `x86`。`Gr` 读的是 model，HAR 里是空串。

`document.referrer` 在直接 `page.goto(注册页)` 时是空的。HAR 那场是从 `https://grok.com/` 走过来的。

字段 98 的长度前缀可以用字符码直接读：小于 32768 时，`n(x)` 就是一个码位为 `x` 的字符。样本开头是 `\\x03 \\x00 g <日志长度>`，分别是常量 3、空的 `VG()`、103 字符的脚本 URL、事件日志长度。

## 已经留在代码里的改动

文件：`nobrowser_protocol/patchright_engine.py`。远程副本同步到了 `panel-2061dec`。

1. 拦截 `02bh2mv7efkj1.js`，把两处 1.5 秒超时改成 20 秒。一处是 SharedWorker 的 `r[9]=r[8]?mN:mJ`，一处是平台 Worker 的 `f[20]=setTimeout(..., f[21])`。函数名是 `extend_castle_worker_timeout`，由 `_keep_castle_worker` 在 `signup_with_patchright` 里挂上。
2. `_goto_accounts_signup` 打开注册页时带 `referer=https://grok.com/`。
3. `_play_signup_events` 在填表前做一轮指针按下/松开，再按一次 Tab，每次动作后停 1200 毫秒。环境变量 `CASTLE_EVENT_ROUNDS` 可把轮数调到 1–24，默认 1。轮数加到 2 并附带额外按键时，事件日志会到 170 以上，token 明显长过 6045。
4. `_fill_signup_email` 把邮箱分成两段打，中间停 1200 毫秒。同一输入框上的连续按键仍然会被 SDK 合并，停顿是为了留下间隔记录。

`_keep_castle_worker` 只在 `signup_with_patchright` 这条正式路径上。下面三次测量脚本自己又做了一次同样的脚本改写，并额外把压缩前明文写进 `data-raw`，用来读字段长度。那段测量代码没有进仓库。

## 代理和邮箱

前几轮长度实验用的是配置里原有的那一个 Resin 入口，邮箱是 `example.com` 探测地址。那些请求返回 `account:email-signup-unavailable`，不能当成真实邮箱的结果。

xfpack 在 Resin 里是订阅，不是平台名。平台只有 `Default`、`TPSTestHK`、`TPSTestJP`、`TPSTestUS`、`grok2api`、`outlook`。`Default` 和 `grok2api` 的正则是 `(xfpack)/*`。直接拿订阅正文里的 `http://user:password@proxy.example:3129` 去开浏览器，出口 IP 能探通，但 `accounts.x.ai` 超时，因为节点前面还有 Resin 这一层。

正确接法是 Resin 混合端口上的新 sticky：用户名 `Default.<新sticky>`，节点标签形如 `xfpack/http-<出口>:3129`。

2026-10-21 用库存里三个未使用的 Outlook，经三个新 sticky 各发了一次码。没有把邮箱标成已用，也没有继续验证码和建号。发码响应都是 HTTP 200。

| 次数 | 邮箱 | 出口 | token | 明文 | 事件日志 |
|---|---|---|---|---|---|
| 1 | account1@example.test | 65.111.28.186 | 5925 | 6802 | 76 |
| 2 | account3@example.test | 45.3.54.61 | 6049 | 6966 | 78 |
| 3 | account2@example.test | 104.207.37.132 | 6037 | 6966 | 82 |

第 1 次字段 116 是 808，另外两次是 960。第 2、3 次的 token 已经贴着 HAR 的 6045。三次的 referrer、版本、平台、SharedWorker 三段都和 HAR 同长。事件日志仍比 92 短 10 到 16 字节。

再补一次 Tab 之后的单次测量：token 6081，明文 7003，事件日志 82。日志更接近 92，但整段明文比 HAR 多 28 字节。事件日志差 10 字节，其余是数值探针编码宽度的波动。

## 测量时怎么读明文

测量脚本在页面里做这三件事，正式注册路径只做第 1 件：

1. `extend_castle_worker_timeout` 改两个超时。
2. 在 `new Blob([u])` 前把压缩前的 `u` 做成 base64，写到 `document.documentElement` 的 `data-raw`。Patchright 隔离了 `page.evaluate` 的 JS 世界，DOM 属性仍然读得到。
3. 把加密函数挂到 `window.__enc`。用 HAR 的时间戳 `1792484855207` 加密一块全 0，从偏移 24 起就是密钥流。HAR 密文 XOR 密钥流，再 `zlib.decompress(..., -15)`，得到 HAR 明文。

字段 98 的事件日志长度就是 payload 第 4 个字节（下标 3），条件是前三个长度都小于 32768。HAR 这个字节是 92。

## 还没对齐的部分

事件日志 82 对 HAR 的 92。再加一轮按下/松开会让日志跳到 170 以上。同一输入框上的重复按键补不了这 10 字节，差的是不同手势之间的间隔记录，数量要刚好。

数值探针的 ±1 到 ±3 字节会让总长度在相邻两次运行里晃大约几十字节。单次 token 落在 5925–6081，HAR 是 6045。样式列表偶尔仍是 808 而不是 960，那一次 token 会短一截。

这三次真实邮箱只测到发码 200，没有跑完验证码和建号。建号用的是另一条更长的 token，不在这次测量里。
