# Castle token 结构

日期：2026-10-21。对象是 `accounts.x.ai` 注册页自己生成的 `castleRequestToken`。

本文只记录外形、密文布局、解密顺序，以及解压后明文的编码和已经对过的字段。不收录 token 原文、代理口令或邮箱凭据。生成链路和长度差的实验记录在 `castle-token-research-20261021.md`。

发码请求体只有 `email` 和 `castleRequestToken`。Turnstile 在建号请求里，不在这条 token 的明文里。`_wait_send_preamble` 等的 Turnstile `api.js`、`set-cookie` 和 `/sign-in` 也不是 token 里的字段。

## 线上字符串

手动成功的发码样本：整串 6045 字符。base64 解开后 4525 字节。解压前的压缩明文 4463 字节。建号用的是另一条 token，样本长 6369，比发码多 324 字符。布局相同，明文更长。

```text
8 字符前缀 | base64(包装字节)
```

8 字符前缀不参与 base64。base64 解开之后才是下表。

## 解开后的字节布局

| 偏移 | 长度 | 这一段 | 怎么读 |
|---|---|---|---|
| 0 | 2 | 时间戳包装的字节长度，小端 uint16 | 成功样本都是 36 |
| 2 | 36 | `tf(Date.now().toString())` 再 `btoa` | 先做 base64，再对 `tf` 做逆运算，得到毫秒时间戳字符串 |
| 38 | 24 | 加密头 | 前 8 字节是两个大端 uint32，是明文长度的两半，加起来等于后面的密文长度。后 16 字节的字段含义没有拆开 |
| 62 | 与压缩明文等长 | 流加密后的 deflate-raw 正文 | 见下一节 |

`tf` 对时间戳字符串的每个字符做三次变换：16 位旋转、加上常量 `50305`、再旋转，然后每个字符打成两个字节。这段可以逆回去。外层时间戳是签发那一刻的 `Date.now()`。

## 密文和解密顺序

密文是按下标生成的 XOR 流。密钥流不依赖明文，种子是时间戳字符串。

1. 去掉 8 字符前缀，对剩余部分做 base64 解码。
2. 用偏移 0 的 uint16 取出时间戳包装，逆 `tf`，得到时间戳字符串。
3. 用页面里的同一个加密函数、同一时间戳，加密一块全 0。加密输出从偏移 24 起是密钥流。测量时把这个函数挂到 `window.__enc`。
4. 从偏移 62 起的密文逐字节 XOR 这条密钥流，得到压缩明文。
5. 压缩算法是 `CompressionStream("deflate-raw")`。Python 侧用 `zlib.decompress(data, -15)`。
6. 按下一节的 tag 解码解压后的字节。

测量脚本还可以在 `new Blob([u])` 之前把压缩前的 `u` 做成 base64，写到 `document.documentElement` 的 `data-raw`。Patchright 隔离了 `page.evaluate` 的 JS 世界，这个 DOM 属性仍然读得到。正式注册路径不写 `data-raw`。

## 明文编码

解压后是自定义编码的 765 元数组。开头 varint 是 `fd 05`，也就是 765。每个元素先读 tag：

| tag | 明文 |
|---|---|
| 0 | 空 |
| 1 | UTF-16 字符串 |
| 2 | 原始字节 |
| 3 | 4 字节小端整数 |
| 4 | 8 字节大端 float64 |
| 5 | false |
| 6 | true |
| 7 | 数组 |
| 8 | `JSON.stringify` 之后再按 UTF-16 |
| 9 | 单字节字符串 |
| 10 | zigzag 整数 |

整数落在 int32 里用 tag 10。落到 `0x80000000..0xffffffff` 用 tag 3。同一个探针两边数值不同时，这一项会差 1 到 3 字节。

## 已经对过的明文下标

长度是该字段的 payload 长度。下标是 765 元数组的下标。

| 下标 | 内容 | 手动样本 |
|---|---|---|
| 3 | `document.referrer`，经 `Y_` / `eL` | 34。直接打开注册页时是空的；从 `https://grok.com/` 进来才有值 |
| 63 | SharedWorker 返回数组下标 2，`Yc` | 202 |
| 98 | `n(3)+n(t长)+n(u长)+n(f长)+t+u+f`。`u` 是 SDK 脚本 URL，103 字符。`f` 是 `ub(uD())` 事件日志 | 事件日志 92 |
| 116 | `getComputedStyle(document.body)` 的样式属性列表，经 `ei` / `VK` | 960。有一次实测是 808，那次整条 token 会短一截 |
| 215 | 同一类 Worker 结果 | 188 |
| 242 | SharedWorker 返回数组下标 10，`G6`，高熵完整版本 | `145.0.7632.159` |
| 324 | 专用 Worker `u6` / `PY()`，读 `userAgentData` | `Linuxx86`。`Gr` 读的 model 在手动样本里是空串 |
| 645 | SharedWorker 返回数组下标 3，`zi` | 154 |

SharedWorker 脚本在 Blob 里。`onconnect` 之后等页面发 `"start"`，再读 `navigator.userAgent`、`userAgentData.brands`，并调用 `getHighEntropyValues(["architecture","bitness","model","platformVersion","uaFullVersion","fullVersionList"])`。SDK 原来只等 `mJ = 1500` 毫秒。回包晚于这个时间时，63、215、645 落到同一段 20 字节失败占位：`13cf1397159f159f13b715bf1567155f159f1537`。

平台和架构不在这个 SharedWorker 里。`PY()` 再开一个专用 Worker，同样 1.5 秒超时。超时后返回编码占位，而不是 `Linux` 和 `x86`。

字段 98 的长度前缀可以用字符码直接读。小于 32768 时，`n(x)` 就是一个码位为 `x` 的字符。样本开头是常量 3、空的 `VG()`、103 字符的脚本 URL、事件日志长度。事件日志长度就是这段 payload 的第 4 个字节（下标 3），条件是前三个长度都小于 32768。手动样本这个字节是 92。

事件日志最多 511 条。同一输入框上连续的按键会被合并，逐字输入不会按字符数把日志拉长。两次手势间隔超过 `ON = 1000` 毫秒时，插入一条间隔记录。单纯 `mouse.move` 主要进 `pointerrawupdate` 的采样缓冲，不会按比例进入 `ub(uD())`。补齐后的实测日志大约 78–82 字节。再加一轮真实按下和松开，日志会超过 170。这 10 字节的差是不同手势之间的间隔记录，数量要刚好，不是「已经等满若干秒」的单独标记。

## 页面上的生成

表单提交走到 `mintCastleStepToken`，再调用 `sdk.createRequestToken()`，不传参数。`flow`、`email`、`posture` 只决定超时和日志。`signUp.sendCode` 默认模式是 `"on"`：初始化 10 秒，mint 10 秒。这个模式下 SDK 抛错会原样抛出，空 token 不会发请求。

`CastleProvider` 加载 SDK 后调用 `configure({pk})`，再 `createRequestToken()`。SDK 导出就是这两个函数。发码前没有发往 castle.io 的请求，token 在浏览器里生成。

SDK 文件是 `https://accounts.x.ai/_next/static/chunks/02bh2mv7efkj1.js`（webpack 模块 `598821`）。`CastleProvider` 默认 stable 包是 `29w_xw4f2d2ae.js`。成功样本实际加载的是 next 包。两份只差一个 debug id，事件记录和加密是同一套。

## 2026-10-24 复核

注册页外壳换过一批 chunk，初始 HTML 不再直接列出 Castle 脚本。脚本仍由模块 `577842` 懒加载 `static/chunks/02bh2mv7efkj1.js`，再进入 webpack `598821`。stable 包仍是 `29w_xw4f2d2ae.js`（模块 `587543` / `504922`）。

当天拉下来的 next 包是 707222 字节，`sha256 d01a0320866b05cb5cb133e412a7bf2ade481c8f8cbcda8fcc22436f4065283e`。stable 包是 707229 字节，比 next 多 7 字节。两处 1500 毫秒超时、`mJ=1500`、`ON=1e3` 都还在原位置。`createRequestToken()` 仍然不传参数。

页面开关里 `castle_sdk_next` 和 `account_improved_castle_flow` 为开，`account_castle_steps` 是空对象。这次随页下来的 49 个 chunk 里，没有第二套加密或新的明文下标。空的 `account_castle_steps` 不会多铸一条 step token。

所以 token 外形没有新版本可补。和手动样本比仍然短在运行时字段，不是 SDK 换代：

| 字段 | 手动样本 | 现在页面打出来的 |
|---|---|---|
| 98 事件日志 | 92 字节 | 约 78–82 字节。间隔条数没对齐，不能把“已经等过”写进加载时的脚本 |
| 116 样式列表 | 960 | 有时是 808，整条 token 会短一截 |
| 63 / 215 / 645 | Worker 真值 | 1.5 秒内没回包时仍是那 20 字节占位。现有补丁把超时改成 20 秒 |

字段 98 的脚本 URL 仍是 103 字符的 `02bh2mv7efkj1.js`。发码体还是 `email` 加 `castleRequestToken`。

## 765 个槽位从哪来

`createRequestToken` 把结果放进数组 `Y3`，再按 tag 编成明文。765 个槽里，一次页面实测的分布是：zigzag 整数 410 个、4 字节整数 152 个、字节串 161 个、单字节字符串 19 个、false 20 个、true 1 个、数组 2 个。其中 119 个 zigzag 仍是预设值 165，40 个是 161，表示这个采集器没有改写该槽。

会随环境变化、并且已经对上来源的槽：

| 下标 | SDK 赋值 | 来源 | 这次实测 |
|---|---|---|---|
| 3 | `Y3[3] = _(wX, Y_())` | `document.referrer` | 34 字节。`goto` 带了 `https://grok.com/` |
| 63 | `Y3[63] = _(ww, Yc())` | SharedWorker 回包下标 2 | 202 字节。20 秒超时内回来了，不是 20 字节占位 |
| 98 | `Y3[98] = _(wz, ub(uD()))` | 脚本 URL 加事件日志 | 129 字符。这次没有 1.2 秒手势间隔，日志比注册流程短 |
| 116 | `Y3[116] = Z(4, VK())` | `ei()` 读 `getComputedStyle` 的属性名 | 只读 `document.body` 时是 808；改成 body 与 `documentElement` 里更长的那份后是 960 |
| 215 | 同一 SharedWorker 回包 | Worker 结果 | 188 字节 |
| 242 | `Y3[242] = _(al, G6())` | SharedWorker 的 `uaFullVersion` | `145.0.7632.159` |
| 324 | `Y3[324]` 的专用 Worker | `userAgentData` 平台和架构 | `Linuxx86` |
| 645 | `Y3[645] = Z(w3, zi())` | SharedWorker 回包下标 3 | 154 字节 |

其余大量 4 字节和 zigzag 是各探针的哈希或计数，同一次浏览器里也会变。它们只让整条 token 在大约 5740–6060 之间晃动，不改变上面这几个槽的来源。

差异就这三类：

1. 字段 116。`ei()` 只在被调用的那一刻读样式。只读 `body` 时属性名较少，编码后是 808。`documentElement` 当时更全，编码后是 960。正式路径已经改成取两者里更长的一份。
2. 字段 63、215、242、645。Worker 在 `mJ=1500` 内没回包时，这三个长字段变成同一段 20 字节占位，版本字段也不再是 `145.0.7632.159`。把 `mJ` 和 `mN` 改成 20 秒并等 `ok` 之后，这次明文里没有那段占位。
3. 字段 98。事件日志只记录真实手势，同一输入框的连续按键会合并，间隔要超过 1000 毫秒才多一条。所以同样的 SDK，快速回车和带停顿的注册，这段长度不一样。
