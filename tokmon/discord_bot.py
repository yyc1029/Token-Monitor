"""Optional Discord bot for querying usage and receiving limit alerts."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlparse


STATE_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "TokenMonitor",
    "discord-state.json",
)
WINDOWS = (("five_hour", "5 小時"), ("seven_day", "每週"))
SOURCES = (("codex", "Codex"), ("claude", "Claude"))


def _number(value):
    return f"{int(value or 0):,}"


def _percent(value):
    if value is None:
        return "無資料"
    return f"{float(value):.0f}%"


def _reset_text(timestamp, now=None):
    if not timestamp:
        return "重置時間未知"
    now = now or time.time()
    seconds = max(0, int(timestamp - now))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小時")
    parts.append(f"{minutes} 分")
    local = datetime.fromtimestamp(timestamp).strftime("%m/%d %H:%M")
    return f"{' '.join(parts)}後（{local}）"


def _selected_sources(source):
    source = (source or "all").lower()
    if source == "codex":
        return (SOURCES[0],)
    if source == "claude":
        return (SOURCES[1],)
    return SOURCES


def format_usage(snapshot, source="all"):
    lines = ["📊 **Token Monitor**"]
    for key, label in _selected_sources(source):
        data = snapshot.get(key) or {}
        limits = data.get("limits") or {}
        today = (data.get("windows") or {}).get("today") or {}
        lines.extend(("", f"**{label}**"))
        for window_key, window_label in WINDOWS:
            window = limits.get(window_key) or {}
            if not window:
                continue
            if window.get("expired"):
                lines.append(f"{window_label}：已重置，等待新視窗的值")
                continue
            lines.append(
                f"{window_label}：{_percent(window.get('used_percent'))}"
                + (f" · {_reset_text(window.get('resets_at'), snapshot.get('now'))}" if window else "")
            )
        if limits.get("stale") and limits.get("age") is not None:
            lines.append(f"⚠️ 額度資料為 {int(limits['age'] // 60)} 分鐘前，可能過時")
        suffix = "（本機處理量，非額度單位）" if key == "codex" else ""
        lines.append(f"今日：{_number(today.get('total'))} tokens · {_number(today.get('requests'))} 次請求{suffix}")
    return "\n".join(lines)


def format_tokens(snapshot, source="all"):
    lines = ["🧮 **Token 用量**"]
    for key, label in _selected_sources(source):
        windows = (snapshot.get(key) or {}).get("windows") or {}
        lines.extend(("", f"**{label}**"))
        for window_key, window_label in (("today", "今日"), ("5h", "近 5 小時"), ("7d", "近 7 天")):
            agg = windows.get(window_key) or {}
            lines.append(f"{window_label}：{_number(agg.get('total'))} tokens · {_number(agg.get('requests'))} 次請求")
    return "\n".join(lines)


def format_status(snapshot):
    updated = snapshot.get("last_poll") or 0
    age = max(0, int(time.time() - updated)) if updated else None
    lines = ["🟢 **Token Monitor 狀態**", f"本機資料：{age} 秒前更新" if age is not None else "本機資料：尚未更新"]
    if snapshot.get("last_error"):
        lines.append(f"收集錯誤：{snapshot['last_error']}")
    now = snapshot.get("now") or time.time()
    for source, label in SOURCES:
        live = (snapshot.get("live") or {}).get(source) or {}
        limits = (snapshot.get(source) or {}).get("limits") or {}
        age = limits.get("age")
        origin = "即時查詢" if limits.get("source") == "live" else "CLI 快取"
        parts = [f"{origin} {int(age // 60)} 分鐘前" if age is not None else "尚無資料"]
        if limits.get("stale"):
            parts.append("可能過時")
        if live.get("error"):
            parts.append(f"查詢失敗：{live['error']}")
        elif live.get("enabled") and live.get("next_attempt"):
            parts.append(f"{max(0, int((live['next_attempt'] - now) // 60))} 分後再查")
        lines.append(f"{label} 額度：" + " · ".join(parts))
    return "\n".join(lines)


def valid_dashboard_url(value):
    """Only allow ordinary web links in Discord link buttons."""
    try:
        parsed = urlparse(value or "")
        return value if parsed.scheme in ("http", "https") and parsed.netloc else None
    except ValueError:
        return None


class DiscordService:
    """Runs discord.py in its own asyncio loop beside the stdlib HTTP server."""

    def __init__(self, collector, config):
        self.collector = collector
        self.config = config or {}
        self.token = os.environ.get(self.config.get("token_env", "TOKMON_DISCORD_TOKEN"), "").strip()
        self.allowed_users = {int(x) for x in self.config.get("allowed_user_ids", []) if str(x).isdigit()}
        self.allow_all_users = bool(self.config.get("allow_all_users", False))
        self.channel_id = int(self.config.get("notification_channel_id") or 0)
        self.dashboard_url = valid_dashboard_url(self.config.get("dashboard_url"))
        self.thresholds = sorted({float(x) for x in self.config.get("thresholds", [50, 80, 95])})
        self.check_interval = max(10.0, float(self.config.get("check_interval", 30)))
        self._thread = None
        self._loop = None
        self._client = None
        self._alert_task = None
        self._state = self._load_state()

    @property
    def enabled(self):
        return bool(self.config.get("enabled") and self.token)

    def start(self):
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run, name="discord-bot", daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._client:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh)
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    def _authorized(self, interaction):
        # Fail closed: forgetting the allow-list must never expose account usage
        # and the private dashboard URL to every member of a Discord server.
        return self.allow_all_users or interaction.user.id in self.allowed_users

    async def _respond(self, interaction, text=None, *, embed=None, view=None):
        if not self._authorized(interaction):
            await interaction.response.send_message("這個 Token Monitor 不接受你的查詢。", ephemeral=True)
            return
        await interaction.response.send_message(
            content=text,
            embed=embed,
            view=view,
            ephemeral=bool(self.config.get("ephemeral", True)),
        )

    def _dashboard_view(self, discord):
        if not self.dashboard_url:
            return None
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="開啟完整儀表板",
            emoji="📈",
            style=discord.ButtonStyle.link,
            url=self.dashboard_url,
        ))
        return view

    def _usage_embed(self, discord, snapshot, source="all"):
        embed = discord.Embed(
            title="📊 Token Monitor",
            description="目前額度與本機 Token 用量",
            color=0x57F287,
            timestamp=datetime.fromtimestamp(snapshot.get("now") or time.time()).astimezone(),
        )
        for key, label in _selected_sources(source):
            data = snapshot.get(key) or {}
            limits = data.get("limits") or {}
            today = (data.get("windows") or {}).get("today") or {}
            rows = []
            for window_key, window_label in WINDOWS:
                window = limits.get(window_key) or {}
                if not window:
                    continue
                reset = _reset_text(window.get("resets_at"), snapshot.get("now")) if window else "重置時間未知"
                rows.append(f"**{window_label}**　{_percent(window.get('used_percent'))}\n↳ {reset}")
            suffix = "\n↳ 本機處理量，不等同訂閱額度" if key == "codex" else ""
            rows.append(f"**今日**　{_number(today.get('total'))} tokens · {_number(today.get('requests'))} 次請求{suffix}")
            embed.add_field(name=label, value="\n".join(rows), inline=False)
        embed.set_footer(text="資料來自這台筆電的 Token Monitor")
        return embed

    def _tokens_embed(self, discord, snapshot, source="all"):
        embed = discord.Embed(
            title="🧮 Token 用量",
            color=0x5865F2,
            timestamp=datetime.fromtimestamp(snapshot.get("now") or time.time()).astimezone(),
        )
        for key, label in _selected_sources(source):
            windows = (snapshot.get(key) or {}).get("windows") or {}
            rows = []
            for window_key, window_label in (("today", "今日"), ("5h", "近 5 小時"), ("7d", "近 7 天")):
                agg = windows.get(window_key) or {}
                rows.append(f"**{window_label}**　{_number(agg.get('total'))} tokens · {_number(agg.get('requests'))} 次")
            embed.add_field(name=label, value="\n".join(rows), inline=False)
        return embed

    def _run(self):
        try:
            import discord
            from discord import app_commands
        except ImportError:
            return

        service = self

        class Client(discord.Client):
            def __init__(self):
                super().__init__(intents=discord.Intents.none())
                self.tree = app_commands.CommandTree(self)

            async def setup_hook(self):
                await self.tree.sync()

            async def on_ready(self):
                if service._alert_task is None or service._alert_task.done():
                    service._alert_task = asyncio.create_task(service._alert_loop())

        client = Client()

        @client.tree.command(name="usage", description="查看目前額度與今日 Token 用量")
        @app_commands.describe(source="all、codex 或 claude")
        @app_commands.choices(source=[
            app_commands.Choice(name="全部", value="all"),
            app_commands.Choice(name="Codex", value="codex"),
            app_commands.Choice(name="Claude", value="claude"),
        ])
        async def usage(interaction: discord.Interaction, source: str = "all"):
            snap = service.collector.snapshot()
            await service._respond(
                interaction,
                embed=service._usage_embed(discord, snap, source),
                view=service._dashboard_view(discord),
            )

        @client.tree.command(name="tokens", description="查看各時間範圍的 Token 用量")
        @app_commands.describe(source="all、codex 或 claude")
        @app_commands.choices(source=[
            app_commands.Choice(name="全部", value="all"),
            app_commands.Choice(name="Codex", value="codex"),
            app_commands.Choice(name="Claude", value="claude"),
        ])
        async def tokens(interaction: discord.Interaction, source: str = "all"):
            snap = service.collector.snapshot()
            await service._respond(interaction, embed=service._tokens_embed(discord, snap, source))

        @client.tree.command(name="status", description="查看 Token Monitor 的資料更新狀態")
        async def status(interaction: discord.Interaction):
            await service._respond(interaction, format_status(service.collector.snapshot()))

        @client.tree.command(name="dashboard", description="取得完整 Token Monitor 儀表板連結")
        async def dashboard(interaction: discord.Interaction):
            view = service._dashboard_view(discord)
            if view:
                await service._respond(interaction, "📈 點下方按鈕開啟完整儀表板。", view=view)
            else:
                await service._respond(
                    interaction,
                    "尚未設定手機可連線的儀表板網址。本機網址 127.0.0.1 無法從手機開啟。",
                )

        self._client = client
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(client.start(self.token))
        finally:
            self._loop.close()

    async def _notification_channel(self):
        channel = self._client.get_channel(self.channel_id)
        if channel is None and self.channel_id:
            try:
                channel = await self._client.fetch_channel(self.channel_id)
            except Exception:
                return None
        return channel

    async def _alert_loop(self):
        await self._client.wait_until_ready()
        while not self._client.is_closed():
            try:
                messages = self._detect_alerts(self.collector.snapshot())
                if messages:
                    channel = await self._notification_channel()
                    if channel:
                        for message in messages:
                            await channel.send(message)
            except Exception:
                pass
            await asyncio.sleep(self.check_interval)

    def _detect_alerts(self, snapshot):
        messages = []
        initialized = bool(self._state)
        for source, label in SOURCES:
            limits = (snapshot.get(source) or {}).get("limits") or {}
            for window_key, window_label in WINDOWS:
                window = limits.get(window_key) or {}
                percent = window.get("used_percent")
                reset_at = window.get("resets_at")
                if percent is None:
                    continue
                state_key = f"{source}.{window_key}"
                previous = self._state.get(state_key) or {}
                old_percent = previous.get("percent")
                old_reset = previous.get("reset_at")
                if initialized and old_percent is not None:
                    # The collector temporarily reports 0% after a reset time has
                    # passed, before the provider publishes the next window. In
                    # that case old_percent may already be zero when reset_at moves
                    # forward, so the elapsed old deadline also proves a reset.
                    reset_changed = bool(old_reset and reset_at and reset_at > old_reset + 60)
                    reset_confirmed = reset_changed and (
                        percent < old_percent or old_reset <= (snapshot.get("now") or time.time())
                    )
                    if reset_confirmed:
                        messages.append(f"🔄 **{label} {window_label}額度已重置**\n目前使用量：{_percent(percent)}")
                    else:
                        crossed = [x for x in self.thresholds if old_percent < x <= percent]
                        if crossed:
                            messages.append(
                                f"⚠️ **{label} {window_label}已達 {_percent(max(crossed))}**\n"
                                f"目前：{_percent(percent)} · {_reset_text(reset_at, snapshot.get('now'))}重置"
                            )
                self._state[state_key] = {"percent": float(percent), "reset_at": reset_at}
        self._save_state()
        return messages
