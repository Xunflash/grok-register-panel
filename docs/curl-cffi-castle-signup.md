# curl_cffi 注册与 Castle token

日期：2026-10-26。对象是面板方案 `curl_cffi`（界面名称「协议注册（curl_cffi）」）。本文按代码实际执行顺序记录注册流程和 Castle token 的生成过程。不收录 token 原文、`pk`、邮箱、密码、SSO 或代理口令。

相关旧文档：

- `docs/castle-token-structure.md`：密文布局和解压后的 tag。
- `docs/castle-token-research-20261021.md`：手动 HAR 与有头 Patchright 对过的字段长度。

## 1. 这条方案在面板里的位置

方案常量是 `signup_scheme.py` 的 `SCHEME_CURL_CFFI = "curl_cffi"`。别名还有 `curl-cffi`、`cffi`、`协议注册（curl_cffi）`。

入口有三处，保存的都是同一个字符串：

- 网页控制台下拉：`webui/monitor.py` 里 `<option value="curl_cffi">`。
- 桌面版方案菜单：`grok_register_ttk.py` 的 `registration_scheme_combo`。
- 控制接口枚举：`webui/api_projection.py` 的 `registration_scheme`。

选中后 `webui/monitor.py` 的 `_registration_env()` 做三件事：

1. `GROK_REGISTRATION_SCHEME=curl_cffi`。
2. `GROK_FP_ENHANCE=0`，避免再走 Camoufox 的指纹启动项。
3. 若本机有 `/usr/bin/chromium` 或 `/usr/bin/chromium-browser`，写入 `PATCHRIGHT_CHROME`。若存在 `/tmp/.X11-unix/X121`，默认 `DISPLAY=:121`，供本轮开头那一次浏览器采样式名。

`signup_until_sso()` 调用 `maybe_run_protocol_signup()`。方案是 `curl_cffi` 时不会返回 `None`，因此不会进入 Camoufox 表单点击。`_patchright_skips_camoufox()` 对 `protocol_patchright` 和 `curl_cffi` 都为真，worker 不调用 `start_browser()`。

每个账号之后的日志、停止、失败统计、SSO 入库，仍走 `signup_until_sso()` 原来的返回值：`email`、`profile`（密码和姓名）、`sso`。流量不另做一套。worker 已经用 `pick_proxy_for_worker()` 绑好出口，`_run_curl_cffi_signup()` 再经 `batch_traffic.meter_proxy_url()` 包一层，curl 的 CONNECT 会计入这一批的流量。

## 2. 一轮开始：只开一次浏览器

`run_registration_cli()` 打出方案和代理池之后，若当前方案是 `curl_cffi`，调用 `nobrowser_protocol/castle_env.py` 的 `prepare_castle_round(config.proxy)`。

这一步做的事：

1. `patchright_fingerprint(proxy)` 取一份 Windows 画像，用来决定窗口大小、UA 和时区。样式名本身不跟账号变，这里只是为了把注册页打开。
2. Patchright 启动 `/usr/bin/chromium`。有 `DISPLAY` 或 `PATCHRIGHT_HEADED` 时用有头，否则加 `--headless=new`。启动参数含 `--window-size` 和 SwiftShader：`--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist`。
3. 打开与有头注册相同的长注册 URL：`https://accounts.x.ai/sign-up?redirect=grok-com&return_to=%2F&mwbv=true&htid=<uuid>&analytics_consent=true&advertising_consent=true`，Referer 是 `https://grok.com/`。
4. 等 1.5 秒后执行脚本：比较 `documentElement` 和 `body` 的 `getComputedStyle().length`，取更长的那份，按下标把属性名收成数组。
5. 名字少于 100 个则抛错。否则写入 `scripts/castle-style-props.json`，或环境变量 `CASTLE_STYLE_FILE` 指向的文件。

2026-10-26 在 claw 的有头 Chromium 145 上，注册页采到 687 个名字。采集失败时批次日志写「沿用已有名单」，后面的 mint 继续读磁盘上的那份。

单独重采可以跑 `scripts/castle_prepare_round.py`，用 `CASTLE_CONFIG` 读代理、`CASTLE_STYLE_FILE` 指定输出。

## 3. 每个账号：面板怎么把一次注册交出去

`_run_curl_cffi_signup()` 和 Patchright 方案共用同一套重试：

1. `run_patchright_pre_inbox()`：收件箱之前的失败，若还没用掉邮箱和云打码，记 `ReusableResourceError`，换出口再试，不计入失败次数。
2. `retry_patchright_mailboxes()`：最多 3 次。`get_email_and_token()` 领邮箱。`existingEmail` 标记已用并换邮箱。发码被拒且属于换出口的错误码时，HTTP 400 不标记已用，调用 `recycle_patchright_exit()` 再领下一个邮箱。验证码没到则标记 `code_miss` 并换邮箱。
3. 停止按钮走 `cancel_callback`。为真时抛 `RegistrationCancelled`。

领到邮箱后进入 `signup_with_curl_cffi()`。临时目录前缀是 `curl-cffi-signup-`，里面有 `fp.json`、`page.html`、`token.txt`，结束时删除。

### 3.1 写这个账号的指纹

`write_castle_fingerprint(fp.json, 该 worker 的代理)` 调用 `patchright_fingerprint()`，再经 `castle_node_profile()` 去掉出口 IP 和代理，只留下 Node 要读的字段：

| 字段 | 来源 |
|---|---|
| `user_agent` | 本机 Chromium `--product-version`，拼成 Windows Chrome UA。读不到版本时用 `145.0.0.0` |
| `ua_metadata` | 与有头 CDP `Emulation.setUserAgentOverride` 相同：brands、`fullVersion`、`fullVersionList`、平台 `Windows`、平台版本 `15.0.0`、架构 `x86`、64 位、`model` 空、`wow64` 假 |
| `platform` | `Win32` |
| `locale` / `languages` | `en-US`，语言列表 `en-US`、`en` |
| `screen` | 1366×768、1536×864、1440×900、1920×1080 轮换 |
| `hardware_concurrency` / `device_memory` | 与屏幕同一序号轮换：8/8、4/8、8/16、12/8 |
| `timezone_id` | 用该代理探测出口 IP，再取地理时区 |
| `webgl_vendor` / `webgl_renderer` | Camoufox 的 Windows WebGL 池抽样，避开名称里含 GTX 980、SwiftShader、Basic Render Driver 的项 |

文件权限尽量设为 `0600`。日志只打屏幕、时区、核数和内存。

### 3.2 打开注册页

`make_protocol_session(proxy, impersonate="chrome136")` 建立 curl_cffi 会话。TLS 指纹是 chrome136，请求头里的 UA 换成上面这份 Windows UA。

GET 第 2 节那条长注册 URL。请求头：

- `Accept: text/html,...`
- `Accept-Language: en-US,en;q=0.9`
- `Referer: https://grok.com/`
- `User-Agent`: 本账号画像里的 UA

HTML 写入 `page.html`。状态码大于等于 400，或正文里没有 `pk_`，直接失败，此时还没消耗邮箱和打码。日志只记录状态码、HTML 长度、pk 是否存在。

### 3.3 铸 Castle token

`_mint_castle()` 起子进程：

```text
node scripts/castle_node_mint.mjs <临时目录>/page.sdk.js
```

环境变量：

| 变量 | 作用 |
|---|---|
| `CASTLE_FP_FILE` | 本账号 `fp.json` |
| `CASTLE_STYLE_FILE` | 本轮样式名，默认 `scripts/castle-style-props.json` |
| `CASTLE_PAGE_FILE` | 本账号刚保存的 HTML，供 `pk` 和 `dpl` |
| `CASTLE_TOKEN_FILE` | 只把 token 写进这个文件，标准输出不打 token |

超时 90 秒。标准输出只转发以 `fp `、`script_url`、`token_len`、`mint_error`、`event_count`、`slot 98`、`style_names`、`bytes`、`pk ` 开头的行。失败或文件空则抛 `castle mint failed`。铸 token 的内部步骤见第 4 节。

### 3.4 发验证码

`build_send_verification_code(email, castle_request_token)` 的 JSON 只有 `email` 和 `castleRequestToken`。POST 到 `https://accounts.x.ai/api/auth/send-verification-code`。请求头带 `Origin: https://accounts.x.ai`、`Content-Type: application/json`、当前页 URL 作为 Referer、同一份 UA。

- HTTP 400 且错误码属于换出口集合（含 `account:email-signup-unavailable` 和限流）：`raise_send_code_rejection()`。面板不把邮箱标已用，换出口和邮箱。
- 其他 4xx 或状态 0：抛错。若尚未标记 `usage.email_used`，释放邮箱预留。
- 2xx：`usage.email_used = True`，这个邮箱算已消耗。然后 `get_oai_code()` 用面板原有的 Outlook 读信。日志只打验证码里的数字个数。

### 3.5 验证邮箱、打码、建号

1. `build_verify_email()` 去掉验证码里的非数字，POST `/api/auth/verify-email`。4xx 抛错。
2. 在调用云打码之前把 `usage.remote_captcha_used = True`。sitekey 使用 `0x4AAAAAAAhr9JGVDZbrZOo0`，页面 URL 传给 `solve_turnstile()`。打码实现仍是面板当前选的 CapSolver / YesCaptcha / 本地点击。
3. 姓名从 Patchright 同一组英文名里随机。密码用 `_rand_password()`。
4. `build_create_account()` POST `/api/auth/create-account`。体里有邮箱、密码、姓名、`emailValidationCode`、`turnstileToken`，以及发码时那条 `castleRequestToken`。
5. 返回 `existingEmail`，或只有 `signInMethods` 且没有 SSO：抛 `ExistingEmailError`，邮箱标记已用并换下一个。
6. SSO 先从响应 cookie 的 `sso` / `sso-rw` 取；没有则 GET `redirectUrl` 再读 cookie；还没有则看 JSON 的 `session` 或 `sso` 字段。都没有则失败。日志只打 `sso len`。

返回给面板的字典含 `email`、`sso`、`password`、`given_name`、`family_name`、`engine=curl_cffi`。`_run_curl_cffi_signup()` 再包成面板通用的 `profile` + `sso`。没有 SSO 时 `maybe_run_protocol_signup()` 抛 `curl_cffi signup returned no sso`。

## 4. Castle token 在 Node 里怎么铸

脚本是 `scripts/castle_node_mint.mjs`。它不打开浏览器。每次执行都重新下载 SDK，在补过的浏览器环境里跑 `configure({pk})` 和 `createRequestToken()`。

### 4.1 读指纹并下载 SDK

1. 读 `CASTLE_FP_FILE`。有 `timezone_id` 时立刻设置 `process.env.TZ`，后面的 `Date.getTimezoneOffset()` 用这个时区。
2. 没有画像时用内置默认：Windows Chrome `145.0.7632.159`、屏幕 1920×1080、核数 8、内存 8、架构 `x86`、WebGL 为 NVIDIA GTX 1660。面板路径总会传入画像，正常注册不会走到这组默认值。
3. `CASTLE_FP_ONLY=1` 时只打印一行 `fp` 就退出，供检查画像，不下载 SDK。
4. GET `https://accounts.x.ai/_next/static/chunks/02bh2mv7efkj1.js`，UA 用画像里的 Windows UA。字节数打印为 `bytes`。2026-10 这份脚本约 707222 字节。保存路径是 argv[2]，面板传入的是临时目录里的 `page.sdk.js`。

### 4.2 让 SDK 能创建 Worker

SDK 把两段 Worker 源码放进 `Blob`，再用 `URL.createObjectURL`。Node 没有浏览器 Blob URL，脚本做了这些替换：

1. `CastleBlob` 把构造参数收成字符串。`URL.createObjectURL` 记成 `blob:castle/N`，源码放进 Map。长度超过 1500 的 Blob 只打印长度和头两个字符码，那是压缩前明文的入口，不是 token。
2. `MessageChannel` 的两个 `MessagePort` 用 `peer` 互指。`postMessage` 在微任务里交给对端的 `onmessage`。
3. `Worker` / `SharedWorker` 用 `vm.createContext` 跑 Blob 里的脚本。`debugger` 语句去掉。Worker 全局补上 `navigator`、`OffscreenCanvas`、`location`、`performance`、`Promise`、`crypto`、`TextEncoder`、`atob`/`btoa`。
4. 专用 Worker 的 `postMessage` 必须就是往父页面回包的函数。若在 `vm.createContext` 之后再包一层，会无限递归。SharedWorker 在构造时派发 `connect`，页面随后向 `port` 发送 `"start"`。

下载到的 SDK 里实际有三段 Blob：

| 脚本 | 大约长度 | 行为 |
|---|---|---|
| SharedWorker | 920 字节 | `onconnect` 后等 `"start"`，读 `navigator.userAgent`、`userAgentData.brands`，再 `getHighEntropyValues`。回一条类型 2、长度 13 的数组 |
| 专用 Worker | 1030 字节 | 读 UA、平台、内存、`OffscreenCanvas` 的 WebGL 厂商和渲染器，先回类型 1、长度 10；再回类型 3、长度 3 的高熵结果 |
| 计时 Worker | 314 字节 | `performance.now()` 做一次间隔，回类型 4、长度 3 |

父页面用回包下标区分结果。类型 5 是 Worker 内部错误。原始 SDK 等待只有 `mJ=1500` 和 `mN=3000` 毫秒。脚本在 `eval` 前把这两处改成 `20000`，避免回包被当成超时占位。占位串是 20 字节的 `13cf1397159f159f13b715bf1567155f159f1537`。补齐后的运行里，这三段都在超时前回来。

Worker 里的 `navigator` 和 WebGL 用的就是第 3.1 节那份画像，不是 Node 自己的 Linux UA。`getParameter(37445)` 返回 `webgl_vendor`，`37446` 返回 `webgl_renderer`。其它参数返回 0。`WEBGL_debug_renderer_info` 的常量号仍是 37445 / 37446。

### 4.3 页面对象和 SDK 装载

SDK 是 Turbopack 包：`(globalThis.TURBOPACK||[]).push([[...], factory])`。脚本换成自己的队列，`eval` 之后调用 `factory`。`createRequestToken` 和 `configure` 在第三个参数上，不在第一个参数上。

装载前还要有这些对象，否则采集函数会在中途抛掉：

- `document.currentScript`：在 `eval` 之前设好。`src` 来自 HTML 里的 `dpl=`（40 字符）拼上 `https://accounts.x.ai/_next/static/chunks/02bh2mv7efkj1.js?dpl=`。完整长度是 103。`async` 为真，`defer` 为假。SDK 初始化时的 `nff()` 只读这一次 `currentScript.src`，晚了就变成 6 字符占位。
- `location.port` 为空字符串。`VG()` 读 `location.port`，空字符串对应字段 98 的第二段长度 0。缺这个属性时第二段是 6 字符占位。
- `document.referrer` 固定为 `https://grok.com/`。字段 3 的明文长度是 34，和从 grok.com 进入的 HAR 相同。
- `document.body` / `documentElement` / `createElement` 是 Proxy。未知方法返回函数，避免 `appendChild` 一类调用抛掉。iframe 的 `contentWindow.Date` 用真实 `Date`，这样 `getTimezoneOffset()` 能跑。
- `getComputedStyle` 返回本轮样式名文件。`length` 和下标 0..n-1 是属性名。`getPropertyValue` 返回空串。SDK 的 `ei()` 用下标收集名字，再做成哈希；它不读取每个属性的计算值。
- `localStorage` / `sessionStorage` 是内存 Map，避免 Node 实验性 `localStorage` 的警告。
- `requestAnimationFrame`、`matchMedia` 未单独实现。已补上的是 Worker、DOM、事件和样式名。`Error.prepareStackTrace` 返回空串，避免堆栈把整份 SDK 打进日志；同时也让依赖真实堆栈文本的探针拿到空栈。
- 页面 `navigator` 的 UA、平台、语言、核数、内存、`userAgentData`、`webdriver=false` 用 `defineProperty` 指到 Worker 同一份画像。`plugins` / `mimeTypes` 是一个 PDF 插件占位。

`eval` 之前还会打两处日志补丁：初始化完成打印 `castle_ready`，采集器开始打印 `collector`。`Y3` 数组在创建时挂到 `globalThis.__castleSlots`，便于事后看槽位类型和长度，不打印槽位原文。

### 4.4 手势，然后 configure

SDK 在 `eval` 时就把 `click`、`select`、`keydown` 等监听器挂到 `document`。`addEventListener` 必须是真的登记，不能是空函数。

监听器挂好之后、`configure` 之前，脚本做 5 轮动作。每一轮：

1. 对一个 `input` 元素派发 `click`。
2. 派发 `select`。
3. 派发 `Tab` 的 `keydown` 和 `keyup`。
4. 除最后一轮外，等待 1200 毫秒。

间隔超过 SDK 的 `ON = 1000` 毫秒时，采集器自己插入一条间隔记录。实测会留下的是 `click`（事件号 0）、`select`（事件号 9）和间隔（事件号 22）。同一输入框上的连续按键会被合并，所以逐字敲邮箱不会按字符数把日志拉长。`pointerdown` 在这份 SDK 里不直接进入 `ub(uD())`。

然后读 `pk`：优先用本次 HTML；Node `fetch` 注册页经常 403，就读 `CASTLE_PAGE_FILE`。先试 `configure(pk字符串)`，SDK 会拒绝并打印 `Castle: missing or wrong pk`。接着 `configure({ pk })`，这一步才成功。`createRequestToken()` 与 20 秒超时赛跑。成功时只打印 `token_len`。设置了 `CASTLE_TOKEN_FILE` 时把 token 写入该文件。

### 4.5 明文里和 HAR 对过的槽

解压后是 765 元数组。下面的长度是 tag 解码后的 payload 长度，数字来自 2026-10-26 这条 Node 路径的实测，对照 `castle-token-research-20261021.md` 里的手动 HAR。

| 下标 | 内容 | HAR | 本路径 |
|---|---|---|---|
| 3 | `document.referrer` | 34 | 34 |
| 63 | SharedWorker 回包下标 2 | 202 | 232，不是 20 字节超时占位 |
| 98 | 常量 3、空的 `VG()`、103 字符脚本 URL、事件日志 | 事件日志 92；整段约 4+0+103+92 | 整段 189；前缀字符码 3、0、103、82 |
| 116 | 样式名经过 `ei()` / `VK()` | 960（687 个属性名编进去） | 160。687 个名字会被读到，但 `W(..., join)` 的结果是 8 位数字哈希，名字表本身没有按 HAR 那种 960 字节放进槽里 |
| 215 | 同一类 Worker 结果 | 188 | 204 |
| 242 | `uaFullVersion` | `145.0.7632.159`（14 字符） | 同样 14 字符，内容是画像里的完整版本 |
| 324 | 平台和架构拼接 | HAR 手动样本是 `Linuxx86` | 画像是 Windows，明文为 `Windowsx86`，编码长度 14 |
| 645 | SharedWorker 回包下标 3 | 154 | 238 |

tag 个数也和那次浏览器实测不完全相同。浏览器那次大约是 zigzag 410、4 字节整数 152、字节串 161、单字节字符串 19、false 20、true 1、数组 2。Node 这条路径字节串仍是 161、单字节字符串仍是 19，zigzag 约 398，4 字节整数约 155，false 18，true 3，数组 11。多出来的数组和 true/false 表示有些探针走了和真实浏览器不同的分支。

因此相对 HAR，已经对齐的是来源页、脚本 URL、版本号字符串、Worker 没有超时占位、事件日志到了有头注册用的 78–82 字节这一档。还没对齐的是：样式槽只有哈希而不是 960 字节的名字表；Worker 三槽比 HAR 更长；平台是 Windows 而不是 HAR 那场的 Linux；事件日志比手动样本的 92 短 10 字节；大量数值探针的 tag 分布仍有差。

## 5. 和「发码 400」有关的一次对照

同一条配置代理、不换出口，连续 5 个尾部未用 Outlook 发码都是 HTTP 400，错误码 `account:email-signup-unavailable`。那 5 次的 token 已经铸完（长度 6153 或 6161，脚本 URL 103，事件日志 82）。

换成 5 个不同出口之后，发码全部 HTTP 200。其中 3 个建号返回 `redirectUrl` 和 `session`，1 个建号返回 `existingEmail`，1 个发码成功后 Outlook 刷新令牌 `invalid_grant`，没有读到验证码。这次用的出口是 65.111.1.82、104.207.34.75、104.207.32.73、216.26.250.100、104.207.58.106，时区分别是纽约、纽约、纽约、多伦多、曼谷。

面板规则仍是：这个 400 同时换出口和邮箱，并且 HTTP 400 不把邮箱标成已用。token 明文和 HAR 的差距列在第 4.5 节，不因为换出口成功就消失。

## 6. 文件一览

| 文件 | 作用 |
|---|---|
| `signup_scheme.py` | 方案常量、`_run_curl_cffi_signup()`、接入 `maybe_run_protocol_signup()` |
| `nobrowser_protocol/curl_cffi_signup.py` | 单个账号的 curl 会话、铸 token、发码、验证、打码、建号、取 SSO |
| `nobrowser_protocol/castle_env.py` | 写账号指纹；一轮开始时开浏览器采样式名 |
| `scripts/castle_node_mint.mjs` | 下载 SDK，补环境，执行 `createRequestToken` |
| `scripts/castle-style-props.json` | 最近一次采到的样式名 |
| `scripts/castle_prepare_round.py` | 手动重采样式名 |
| `browser_session.py` | `patchright_fingerprint()`、`castle_node_profile()` |
| `grok_register_ttk.py` | 本轮开头采样式名；该方案不启动 Camoufox |
| `webui/monitor.py` | 下拉项，以及子进程环境变量 |
| `webui/api_projection.py` | 控制接口允许 `curl_cffi` |
