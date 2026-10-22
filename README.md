# B站刷视频（astrbot_plugin_bili_watcher）

一个 AstrBot v4 插件：让机器人按关键词搜索 B 站视频并“观看”，支持每日定时执行、结果推送到 QQ；支持扫码登录 B 站后自动评论并 @ 你的账号。

> 观看为安全浏览版：只拉取视频信息、记录观看，**不做刷播放量**。
> 自动评论有风控/封号风险，请自行评估后谨慎开启。

## 功能

- 按关键词搜索 B 站视频并“观看”（拉取视频详情，记录观看）。
- 每日定时执行一次（时间、时区可配置），每天最多刷 N 条。
- 结果汇总后推送回指定 QQ 会话（可选）。
- 评论：对刷到的视频自动评论，并 @ 指定 B 站账号。
- 扫码登录：`/bili_login` 生成二维码，扫码后自动保存 Cookie，无需手动抓取。

## 安装

三种方式任选其一，推荐方式一。

### 方式一：从链接安装（推荐，最简单）

1. 打开 AstrBot 面板 →「插件」页 → 点「安装插件」。
2. 切到「从链接」标签页，粘贴仓库地址：

   ```
   https://github.com/tea1-bot/astrbot_plugin_bili_watcher
   ```

3. 点安装，等待完成。
4. 在「已安装插件」里找到「B站刷视频」，点加载/启用。

### 方式二：下载 ZIP 上传安装

1. 打开仓库页面，点绿色 **Code** 按钮 → **Download ZIP**。
2. AstrBot 面板 →「插件」页 →「安装插件」→「从文件上传」→ 选择刚下载的 `.zip`。
3. 安装完成后，在插件列表里启用「B站刷视频」。

### 方式三：手动复制

1. 下载 ZIP（或 `git clone` 仓库）并解压。
2. 把包含 `main.py`、`metadata.yaml` 的文件夹重命名为 `astrbot_plugin_bili_watcher`，复制到 AstrBot 插件目录 `data/plugins/` 下。
3. 重启 AstrBot（或重载插件），在插件列表里启用。

> 依赖说明：`aiohttp`、`qrcode`、`Pillow` 均为 AstrBot 自带依赖，无需额外安装。

## 首次使用（推荐顺序）

1. 在 QQ 里发 `/bili_login`（管理员）→ 收到二维码 → 用 **B站 App** 扫码并在手机上确认。
2. 收到「登录成功」后，去插件设置里：
   - 填「刷视频的关键词」
   - 填「评论@的B站账号（昵称）」
   - 打开「是否留下评论」（可选）
   - 设置「每天刷几条视频」「执行时间」「结果推送到的会话」
3. 发 `/bili` 测试一轮。

## 配置项

| 键 | 说明 |
| --- | --- |
| `daily_count` | 每天刷几条视频（0 = 不自动刷） |
| `keywords` | 刷视频的关键词，逗号分隔 |
| `run_time` | 每天自动执行时间 HH:MM |
| `timezone` | 时区，如 Asia/Shanghai |
| `notify_session` | 每日报告推送的会话，留空不推送 |
| `leave_comment` | 是否留下评论 |
| `comment_at` | 评论 @ 的 B 站昵称（非 UID） |
| `comment_text` | 评论正文模板，支持 {title}/{author}/{account}，留空只 @账号 |
| `comment_interval` | 两条评论之间的间隔秒数 |
| `sessdata` | B站 SESSDATA（扫码登录自动填入） |
| `bili_jct` | B站 bili_jct（扫码登录自动填入） |

## 指令（管理员）

- `/bili_login` —— 扫码登录 B 站（自动保存 Cookie）
- `/bili_logout` —— 清除已保存的登录 Cookie
- `/bili` —— 按配置立即刷一轮
- `/bili_keyword 关键词` —— 用指定关键词刷一轮
- `/bili_status` —— 查看配置与今日进度
- `/bili_reset` —— 重置今日计数

## 获取会话 ID

在要接收报告的 QQ 会话里发送 `/sid`，复制其中的“统一消息来源”
（形如 `aiocqhttp:group_message:123456`）填到 `notify_session`。

## 说明与注意

- “观看”为安全版：仅调用公开接口搜索 + 拉取视频详情并记录，不会人为抬高播放量。
- 评论需要登录态（`SESSDATA` + `bili_jct`），`/bili_login` 扫码即可自动获取；Cookie 属敏感信息，请勿泄露。
- 自动评论可能触发 B 站风控（常见错误码 -352）甚至封号，建议把 `comment_interval` 调大、评论内容避免千篇一律。
- @ 账号需要填写准确的 B 站昵称才能成功 @；若评论成功但 @ 未生效，多半是昵称不对。
