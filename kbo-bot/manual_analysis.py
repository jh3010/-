import asyncio
import datetime
import json
import os
import tempfile
from typing import Optional

import discord
from discord.ui import Button, Modal, Select, TextInput, View

MANUAL_ANALYSIS_FILE = "manual_analysis.json"
MAX_TEXT = 4000


def _load() -> dict:
    try:
        with open(MANUAL_ANALYSIS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    directory = os.path.dirname(os.path.abspath(MANUAL_ANALYSIS_FILE)) or "."
    fd, tmp = tempfile.mkstemp(prefix="manual_analysis_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, MANUAL_ANALYSIS_FILE)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def analysis_key(game_id: str) -> str:
    return str(game_id).strip()


def get_manual_analysis(game_id: str) -> Optional[dict]:
    item = _load().get(analysis_key(game_id))
    return item if isinstance(item, dict) else None


def is_published(game_id: str) -> bool:
    item = get_manual_analysis(game_id)
    return bool(item and item.get("status") == "published")


def save_draft(game_id: str, payload: dict, author_id: int):
    data = _load()
    old = data.get(analysis_key(game_id), {}) or {}
    data[analysis_key(game_id)] = {
        **old,
        **payload,
        "gameId": analysis_key(game_id),
        "status": "draft",
        "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "updatedBy": str(author_id),
    }
    _save(data)


def publish_analysis(game_id: str, author_id: int):
    data = _load()
    item = data.get(analysis_key(game_id))
    if not isinstance(item, dict):
        return False
    item["status"] = "published"
    item["publishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    item["publishedBy"] = str(author_id)
    _save(data)
    return True


def unpublish_analysis(game_id: str, author_id: int):
    data = _load()
    item = data.get(analysis_key(game_id))
    if not isinstance(item, dict):
        return False
    item["status"] = "draft"
    item["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    item["updatedBy"] = str(author_id)
    _save(data)
    return True


def _clip(v: str) -> str:
    return str(v or "")[:MAX_TEXT]


class ManualAnalysisModal(Modal, title="KBO 수동 경기 분석 작성"):
    def __init__(self, game_id: str, game_label: str, existing: Optional[dict] = None):
        super().__init__(timeout=None)
        existing = existing or {}
        self.game_id = game_id
        self.game_label = game_label
        self.summary = TextInput(
            label="종합 의견",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_TEXT,
            default=_clip(existing.get("summary", "")),
            placeholder="경기 전체 흐름과 핵심 판단을 입력하세요.",
        )
        self.key_points = TextInput(
            label="핵심 변수 TOP 3",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_TEXT,
            default=_clip(existing.get("keyPoints", "")),
            placeholder="1. ...\n2. ...\n3. ...",
        )
        self.hitter = TextInput(
            label="타자 분석",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_TEXT,
            default=_clip(existing.get("hitter", "")),
            placeholder="상위타선, 중심타선, 최근 타격 등을 입력하세요.",
        )
        self.pitcher = TextInput(
            label="투수 분석",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_TEXT,
            default=_clip(existing.get("pitcher", "")),
            placeholder="선발/중계투수 운용과 비교를 입력하세요.",
        )
        self.notes = TextInput(
            label="결장·구장·날씨 및 기타",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_TEXT,
            default=_clip(existing.get("notes", "")),
            placeholder="결장, 구장, 날씨, 기타 확인사항을 입력하세요.",
        )
        for item in (self.summary, self.key_points, self.hitter, self.pitcher, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        payload = {
            "gameLabel": self.game_label,
            "summary": str(self.summary.value or "").strip(),
            "keyPoints": str(self.key_points.value or "").strip(),
            "hitter": str(self.hitter.value or "").strip(),
            "pitcher": str(self.pitcher.value or "").strip(),
            "notes": str(self.notes.value or "").strip(),
        }
        save_draft(self.game_id, payload, interaction.user.id)
        await interaction.response.send_message(
            "수동 분석을 임시저장했습니다. 아래에서 검수 후 공개할 수 있습니다.",
            ephemeral=True,
            view=ManualReviewView(self.game_id),
        )


class ManualReviewView(View):
    def __init__(self, game_id: str):
        super().__init__(timeout=None)
        self.game_id = game_id

    @discord.ui.button(label="내용 다시 작성", style=discord.ButtonStyle.secondary, custom_id="kbo_manual_edit")
    async def edit_button(self, interaction: discord.Interaction, button: Button):
        item = get_manual_analysis(self.game_id) or {}
        label = item.get("gameLabel", self.game_id)
        await interaction.response.send_modal(
            ManualAnalysisModal(self.game_id, label, item)
        )

    @discord.ui.button(label="완료 후 공개", style=discord.ButtonStyle.success, custom_id="kbo_manual_publish")
    async def publish_button(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("관리자만 공개할 수 있습니다.", ephemeral=True)
            return
        item = get_manual_analysis(self.game_id)
        if not item:
            await interaction.response.send_message("저장된 분석이 없습니다.", ephemeral=True)
            return
        if not any(str(item.get(k, "")).strip() for k in ("summary", "keyPoints", "hitter", "pitcher", "notes")):
            await interaction.response.send_message("내용을 하나 이상 입력한 뒤 공개하세요.", ephemeral=True)
            return
        publish_analysis(self.game_id, interaction.user.id)
        await interaction.response.edit_message(
            content=f"✅ 공개 완료: {item.get('gameLabel', self.game_id)}\n이제 사용자 화면에서 수동 분석이 표시될 수 있습니다.",
            view=self,
        )

    @discord.ui.button(label="공개 취소", style=discord.ButtonStyle.danger, custom_id="kbo_manual_unpublish")
    async def unpublish_button(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        if unpublish_analysis(self.game_id, interaction.user.id):
            await interaction.response.send_message("공개 상태를 해제했습니다.", ephemeral=True)
        else:
            await interaction.response.send_message("해당 분석을 찾을 수 없습니다.", ephemeral=True)


class ManualGameSelect(Select):
    def __init__(self, games: list[dict]):
        self.games = games
        options = []
        for i, game in enumerate(games[:25]):
            away = game.get("team_a") or game.get("away") or "원정팀"
            home = game.get("team_b") or game.get("home") or "홈팀"
            gdate = game.get("gdate") or game.get("date") or ""
            gtime = game.get("gtime") or game.get("time") or ""
            status = str(game.get("gameStatusNormalized") or game.get("statusCode") or "BEFORE").upper()
            suffix = " · 취소" if game.get("cancel") or status == "CANCEL" else ""
            options.append(discord.SelectOption(
                label=f"{away} @ {home}"[:100],
                description=f"{gdate} {gtime}{suffix}"[:100],
                value=str(i),
            ))
        super().__init__(placeholder="수동 분석할 경기를 선택하세요.", min_values=1, max_values=1, options=options, custom_id="kbo_manual_game_select")

    async def callback(self, interaction: discord.Interaction):
        try:
            game = self.games[int(self.values[0])]
        except (ValueError, IndexError):
            await interaction.response.send_message("경기 선택이 잘못되었습니다.", ephemeral=True)
            return
        if game.get("cancel") is True or str(game.get("gameStatusNormalized") or game.get("statusCode") or "").upper() in {"CANCEL", "CANCELED", "CANCELLED"}:
            await interaction.response.send_message("취소된 경기는 수동 분석 대상으로 선택할 수 없습니다.", ephemeral=True)
            return
        game_id = str(game.get("gameId") or "").strip()
        if not game_id:
            await interaction.response.send_message("경기 ID를 찾지 못했습니다.", ephemeral=True)
            return
        away = game.get("team_a") or game.get("away") or "원정팀"
        home = game.get("team_b") or game.get("home") or "홈팀"
        label = f"{away} @ {home} · {game.get('gdate') or game.get('date') or ''} {game.get('gtime') or game.get('time') or ''}"
        existing = get_manual_analysis(game_id)
        if existing:
            state = "공개" if existing.get("status") == "published" else "임시저장"
            await interaction.response.send_message(
                f"기존 분석이 있습니다. 현재 상태: **{state}**\n아래 버튼으로 수정하거나 공개 상태를 변경하세요.",
                ephemeral=True,
                view=ManualReviewView(game_id),
            )
            return
        await interaction.response.send_modal(
            ManualAnalysisModal(game_id, label)
        )


class ManualGameSelectView(View):
    def __init__(self, games: list[dict]):
        super().__init__(timeout=None)
        self.add_item(ManualGameSelect(games))


def build_published_manual_embed(game_id: str) -> Optional[discord.Embed]:
    item = get_manual_analysis(game_id)
    if not item or item.get("status") != "published":
        return None
    embed = discord.Embed(
        title=f"수동 분석 · {item.get('gameLabel', game_id)}",
        description=item.get("summary") or "종합 의견이 입력되지 않았습니다.",
        color=discord.Color.gold(),
    )
    if item.get("keyPoints"):
        embed.add_field(name="핵심 변수 TOP 3", value=item["keyPoints"][:1024], inline=False)
    if item.get("hitter"):
        embed.add_field(name="타자 분석", value=item["hitter"][:1024], inline=False)
    if item.get("pitcher"):
        embed.add_field(name="투수 분석", value=item["pitcher"][:1024], inline=False)
    if item.get("notes"):
        embed.add_field(name="결장 · 구장 · 기타", value=item["notes"][:1024], inline=False)
    published_at = item.get("publishedAt") or ""
    embed.set_footer(text=f"관리자 검수 완료 · {published_at}")
    return embed


def build_manual_public_button(game_id: str):
    if not is_published(game_id):
        return None
    view = View(timeout=None)

    class PublicButton(Button):
        def __init__(self):
            super().__init__(label="관리자 검수 분석 보기", style=discord.ButtonStyle.success, custom_id=f"kbo_manual_public_{analysis_key(game_id)}"[:100])

        async def callback(self, interaction: discord.Interaction):
            embed = build_published_manual_embed(game_id)
            if embed is None:
                await interaction.response.send_message("현재 공개된 수동 분석이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message(embed=embed, ephemeral=True)

    view.add_item(PublicButton())
    return view