"""B站刷视频插件

按关键词搜索并“观看”B站视频，支持每日定时任务与手动指令。
- 观看：安全浏览版，只拉取视频信息并记录，不做刷量。
- 评论：可对刷到的视频发表评论并 @ 指定 B 站账号。
- 登录：支持 /bili_login 扫码登录，自动获取并保存 SESSDATA / bili_jct Cookie。
"""
import asyncio
import html
import json
import os
import random
import re
import tempfile
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
COMMENT_URL = "https://api.bilibili.com/x/v2/reply/add"
QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
VIDEO_URL = "https://www.bilibili.com/video/{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

_HTML_TAG = re.compile(r"<[^>]+>")

# 评论失败常见错误码 -> 说明
_COMMENT_ERR = {
    -101: "未登录/登录态失效",
    -352: "风控校验失败",
    -403: "权限不足",
    -404: "视频不存在",
    12002: "评论区已关闭",
    12009: "评论含敏感词",
    12015: "评论过快（被限频）",
    12016: "评论内容太短",
}


def _fmt_num(n) -> str:
    """把数字格式化为 1.2万 / 3.4亿 这样的可读形式。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._polling_keys: set[str] = set()
        # 当日刷视频计数（用于“一天刷几条”上限）
        self._today: str = ""
        self._today_count: int = 0

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        if self._should_schedule():
            self._task = asyncio.create_task(self._scheduler_loop())
            logger.info(
                "B站刷视频：每日任务已启动（%s %s）。",
                self._get_str("run_time", "09:00"),
                self._get_str("timezone", "Asia/Shanghai"),
            )

    async def terminate(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("B站刷视频插件已停止。")

    # ------------------------------------------------------------------ 指令
    @filter.command("bili", alias={"刷视频", "刷b站", "看b站"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_watch(self, event: AstrMessageEvent):
        """按插件设置立即刷一轮 B 站视频"""
        keywords = self._get_keywords()
        if not keywords:
            yield event.plain_result("尚未配置关键词，请在插件设置里填写“刷视频的关键词”。")
            return
        report = await self._do_watch_round(keywords, self._get_int("daily_count", 5))
        yield event.plain_result(report)

    @filter.command("bili_keyword", alias={"刷关键词"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_keyword(self, event: AstrMessageEvent, keyword: GreedyStr):
        """用指定关键词立即刷一轮 B 站视频"""
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("用法：/bili_keyword 关键词")
            return
        report = await self._do_watch_round([keyword], self._get_int("daily_count", 5))
        yield event.plain_result(report)

    @filter.command("bili_status", alias={"刷视频状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_status(self, event: AstrMessageEvent):
        """查看当前配置与今日进度"""
        yield event.plain_result(self._status_text())

    @filter.command("bili_reset", alias={"刷视频重置"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_reset(self, event: AstrMessageEvent):
        """重置今日刷视频计数"""
        self._today = ""
        self._today_count = 0
        yield event.plain_result("已重置今日刷视频计数。")

    @filter.command("bili_login", alias={"登录b站", "b站登录"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_login(self, event: AstrMessageEvent):
        """扫码登录 B 站，自动保存评论所需的 Cookie"""
        data = await self._get_json(QR_GENERATE_URL, {})
        if not data or data.get("code") != 0:
            yield event.plain_result(
                f"获取登录二维码失败：{(data or {}).get('message', '未知错误')}"
            )
            return
        qr = data.get("data") or {}
        url = qr.get("url")
        qrcode_key = qr.get("qrcode_key")
        if not url or not qrcode_key:
            yield event.plain_result("获取登录二维码失败：缺少 url 或 qrcode_key。")
            return

        img_path = self._render_qrcode(url)
        if not img_path:
            yield event.plain_result("生成二维码图片失败。")
            return

        if qrcode_key in self._polling_keys:
            yield event.plain_result("已有一次登录流程正在进行，请先完成或等待其超时。")
            return
        self._polling_keys.add(qrcode_key)

        yield event.image_result(img_path)
        yield event.plain_result(
            "请用【B站 App】扫上面的二维码登录，约 3 分钟内有效，登录成功后我会自动保存 Cookie。"
        )

        asyncio.create_task(self._poll_login(event.unified_msg_origin, qrcode_key))

    @filter.command("bili_logout", alias={"退出登录", "b站退出"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_logout(self, event: AstrMessageEvent):
        """清除已保存的 B 站登录 Cookie"""
        await self._save_cookie("", "")
        yield event.plain_result("已清除保存的 B 站登录 Cookie。")

    # ------------------------------------------------------------------ 定时
    def _should_schedule(self) -> bool:
        return self._get_int("daily_count", 5) > 0 and bool(self._get_keywords())

    def _parse_run_time(self) -> tuple[int, int, str]:
        t = self._get_str("run_time", "09:00")
        tz_name = self._get_str("timezone", "Asia/Shanghai") or "Asia/Shanghai"
        try:
            hh, mm = t.split(":")
            hh = int(hh) % 24
            mm = int(mm) % 60
        except Exception:
            hh, mm = 9, 0
        return hh, mm, tz_name

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            hh, mm, tz_name = self._parse_run_time()
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("Asia/Shanghai")
            now = datetime.now(tz)
            nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            delay = (nxt - now).total_seconds()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self._run_daily_watch()
            except Exception as exc:  # noqa: BLE001
                logger.exception("B站刷视频每日任务执行失败: %s", exc)

    async def _run_daily_watch(self) -> None:
        count = self._get_int("daily_count", 5)
        if count <= 0:
            return
        report = await self._do_watch_round(self._get_keywords(), count)
        target = self._get_str("notify_session", "")
        if target:
            await self._send_text(target, report)
        else:
            logger.info("B站刷视频每日任务完成：\n%s", report)

    # ------------------------------------------------------------------ 观看逻辑
    async def _do_watch_round(self, keywords: list[str], count: int) -> str:
        count = max(0, int(count))
        if count <= 0:
            return "每日刷视频条数为 0，未执行。"

        tz_name = self._get_str("timezone", "Asia/Shanghai") or "Asia/Shanghai"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        today = datetime.now(tz).strftime("%Y-%m-%d")

        leave_comment = self._get_bool("leave_comment", False)
        comment_interval = max(0, self._get_int("comment_interval", 8))

        async with self._lock:
            if self._today != today:
                self._today = today
                self._today_count = 0
            remaining = count - self._today_count
            if remaining <= 0:
                return f"今日已刷满 {count} 条，明天再来吧～"

            candidates = await self._collect_videos(keywords, remaining * 3)
            watched: list[str] = []
            comment_log: list[tuple[str, bool, str]] = []
            for video in candidates:
                if len(watched) >= remaining:
                    break
                info = await self._get_video_info(video["bvid"])
                watched.append(self._summarize(video, info))
                if leave_comment:
                    ok, msg = await self._post_comment(video, info)
                    title = video.get("title") or info.get("title") or "(无标题)"
                    comment_log.append((title, ok, msg))
                    if comment_interval > 0:
                        await asyncio.sleep(comment_interval + random.uniform(0, 3))
                else:
                    # 安全版：仅记录观看，不调用播放/心跳接口刷量
                    await asyncio.sleep(0.3)

            if not watched:
                return "没有找到符合条件的视频，请检查关键词或稍后再试。"

            self._today_count += len(watched)

        lines = [f"【B站刷视频】今日进度 {self._today_count}/{count}", ""]
        for i, s in enumerate(watched, 1):
            lines.append(f"{i}. {s}")
            lines.append("")
        if comment_log:
            lines.append("【评论结果】")
            for i, (t, ok, msg) in enumerate(comment_log, 1):
                short = t[:20] + ("…" if len(t) > 20 else "")
                mark = "✅" if ok else "❌"
                lines.append(f"{i}. {mark} {short} — {msg}")
        return "\n".join(lines).rstrip()

    async def _collect_videos(self, keywords: list[str], wanted: int) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        per_kw = max(1, wanted // max(1, len(keywords)) + 1)
        for kw in keywords:
            if len(out) >= wanted:
                break
            items = await self._search_videos(kw, per_kw * 2)
            for it in items:
                bvid = it.get("bvid")
                if not bvid or bvid in seen:
                    continue
                seen.add(bvid)
                out.append(it)
                if len(out) >= wanted:
                    break
        return out[:wanted]

    async def _search_videos(self, keyword: str, limit: int = 10) -> list[dict]:
        data = await self._get_json(
            SEARCH_URL,
            {"search_type": "video", "keyword": keyword, "page": 1, "page_size": limit},
        )
        if not data or data.get("code") != 0:
            logger.warning(
                "B站搜索失败（%s）: %s", keyword, (data or {}).get("message")
            )
            return []
        result = ((data.get("data") or {}).get("result")) or []
        out = []
        for it in result:
            bvid = it.get("bvid")
            if not bvid:
                continue
            title = _HTML_TAG.sub("", html.unescape(str(it.get("title", "")))).strip()
            out.append(
                {
                    "bvid": bvid,
                    "aid": it.get("aid"),
                    "title": title,
                    "author": it.get("author", ""),
                    "play": it.get("play", 0),
                    "duration": it.get("duration", ""),
                }
            )
        return out

    async def _get_video_info(self, bvid: str) -> dict:
        data = await self._get_json(VIEW_URL, {"bvid": bvid})
        if not data or data.get("code") != 0:
            return {}
        return data.get("data") or {}

    def _summarize(self, video: dict, info: dict) -> str:
        title = video.get("title") or info.get("title") or "(无标题)"
        author = video.get("author") or (info.get("owner") or {}).get("name") or "未知"
        play = video.get("play")
        if play is None:
            stat = info.get("stat") or {}
            play = stat.get("view", 0)
        duration = video.get("duration") or ""
        if not duration:
            d = int(info.get("duration") or 0)
            if d > 0:
                duration = f"{d // 60}:{d % 60:02d}"
        bvid = video.get("bvid", "")
        link = VIDEO_URL.format(bvid) if bvid else ""

        line2 = f"UP主：{author}｜播放：{_fmt_num(play)}"
        if duration:
            line2 += f"｜时长：{duration}"
        return f"{title}\n{line2}\n{link}"

    # ------------------------------------------------------------------ 评论
    def _build_comment_message(self, video: dict, info: dict) -> str:
        account = self._get_str("comment_at", "").strip()
        body = self._get_str("comment_text", "").strip()
        title = video.get("title") or info.get("title") or ""
        author = video.get("author") or (info.get("owner") or {}).get("name") or ""
        body = (
            body.replace("{title}", title)
            .replace("{author}", author)
            .replace("{account}", account)
        )
        parts: list[str] = []
        if account:
            parts.append(f"@{account}")
        if body:
            parts.append(body)
        return " ".join(parts).strip()

    async def _post_comment(self, video: dict, info: dict) -> tuple[bool, str]:
        message = self._build_comment_message(video, info)
        if not message:
            return False, "评论内容为空"

        aid = info.get("aid") or video.get("aid")
        if not aid:
            return False, "缺少视频 aid，无法评论"

        sessdata = self._get_str("sessdata", "")
        bili_jct = self._get_str("bili_jct", "")
        if not sessdata or not bili_jct:
            return False, "未配置 Cookie，请先 /bili_login 扫码登录"

        bvid = video.get("bvid", "")
        headers = dict(HEADERS)
        headers["Cookie"] = f"SESSDATA={sessdata}; bili_jct={bili_jct}"
        headers["Referer"] = VIDEO_URL.format(bvid) if bvid else "https://www.bilibili.com/"
        headers["Origin"] = "https://www.bilibili.com"

        payload = {
            "oid": str(aid),
            "type": "1",
            "message": message,
            "plat": "1",
            "csrf": bili_jct,
        }
        session = await self._ensure_session()
        try:
            async with session.post(
                COMMENT_URL,
                data=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text()
                data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("发表评论失败（%s）: %s", bvid, exc)
            return False, f"请求失败：{exc}"

        code = (data or {}).get("code")
        if code == 0:
            return True, "评论成功"
        reason = _COMMENT_ERR.get(code, f"未知错误码 {code}")
        return False, reason

    # ------------------------------------------------------------------ 扫码登录
    def _render_qrcode(self, url: str) -> str | None:
        try:
            import qrcode

            img = qrcode.make(url)
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            img.save(path)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成二维码图片失败: %s", exc)
            return None

    async def _poll_login(self, session: str, qrcode_key: str) -> None:
        deadline = time.time() + 180
        last_status = None
        try:
            while time.time() < deadline:
                data = await self._get_json(QR_POLL_URL, {"qrcode_key": qrcode_key})
                if not data:
                    await asyncio.sleep(2)
                    continue
                code = data.get("code")
                if code == 0:
                    d = data.get("data") or {}
                    cookies = self._extract_cookies(d.get("url", ""))
                    sessdata = cookies.get("SESSDATA", "")
                    bili_jct = cookies.get("bili_jct", "")
                    if sessdata and bili_jct:
                        await self._save_cookie(sessdata, bili_jct)
                        await self._send_text(
                            session,
                            "B站登录成功，Cookie 已保存。现在可以把“是否留下评论”打开使用了。",
                        )
                    else:
                        await self._send_text(
                            session,
                            "登录接口返回成功，但未能解析到 Cookie，请重试或手动填写配置。",
                        )
                    return
                if code == 86038:
                    await self._send_text(session, "二维码已失效，请重新发送 /bili_login。")
                    return
                if code == 86090 and last_status != 86090:
                    await self._send_text(session, "已扫码，请在手机上确认登录…")
                last_status = code
                await asyncio.sleep(2)
            await self._send_text(session, "登录超时，请重新发送 /bili_login。")
        finally:
            self._polling_keys.discard(qrcode_key)

    def _extract_cookies(self, url: str) -> dict[str, str]:
        if not url:
            return {}
        try:
            q = parse_qs(urlparse(url).query)
        except Exception:
            return {}
        out: dict[str, str] = {}
        for key in ("SESSDATA", "bili_jct"):
            vals = q.get(key)
            if vals:
                out[key] = vals[0]
        return out

    async def _save_cookie(self, sessdata: str, bili_jct: str) -> None:
        self.config["sessdata"] = sessdata
        self.config["bili_jct"] = bili_jct
        saver = getattr(self.config, "save_config_async", None) or getattr(
            self.config, "save_config", None
        )
        if saver is None:
            return
        try:
            if asyncio.iscoroutinefunction(saver):
                await saver()
            else:
                saver()
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存 Cookie 到配置失败: %s", exc)

    # ------------------------------------------------------------------ 网络 / 发送
    async def _get_json(self, url: str, params: dict) -> dict | None:
        session = await self._ensure_session()
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                text = await resp.text()
                return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("请求 B 站接口失败 %s: %s", url, exc)
            return None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS)
        return self._session

    async def _send_text(self, session: str, text: str) -> None:
        try:
            await self.context.send_message(session, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.exception("向会话 %s 发送消息失败: %s", session, exc)

    # ------------------------------------------------------------------ 配置读取
    def _get_int(self, key: str, default: int = 0) -> int:
        val = self.config.get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _get_bool(self, key: str, default: bool = False) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in {"1", "true", "yes", "on", "启用", "开启"}
        return bool(val)

    def _get_str(self, key: str, default: str = "") -> str:
        val = self.config.get(key, default)
        return "" if val is None else str(val).strip()

    def _get_keywords(self) -> list[str]:
        raw = self._get_str("keywords", "")
        if not raw:
            return []
        out: list[str] = []
        for p in re.split(r"[,，;；\n]", raw):
            p = p.strip()
            if p and p not in out:
                out.append(p)
        return out

    def _status_text(self) -> str:
        keywords = self._get_keywords()
        kw_text = "、".join(keywords) if keywords else "（未配置）"
        comment = "开启" if self._get_bool("leave_comment", False) else "关闭"
        logged = "已登录" if self._get_str("sessdata", "") and self._get_str("bili_jct", "") else "未登录"
        lines = [
            "【B站刷视频 · 状态】",
            f"每日条数：{self._get_int('daily_count', 5)}",
            f"关键词：{kw_text}",
            f"执行时间：{self._get_str('run_time', '09:00')}（{self._get_str('timezone', 'Asia/Shanghai')}）",
            f"评论：{comment}",
            f"评论@账号：{self._get_str('comment_at', '') or '（未配置）'}",
            f"登录状态：{logged}",
            f"通知会话：{self._get_str('notify_session', '') or '（未配置）'}",
            f"今日进度：{self._today_count}/{self._get_int('daily_count', 5)}",
        ]
        return "\n".join(lines)
