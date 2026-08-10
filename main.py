"""
astrbot_plugin_truth_or_dare - 真心话大冒险

群聊派对游戏插件。玩家加入游戏后通过 Roll 点决定命运，
机器人随机指定玩家完成真心话或大冒险事件，完成后再进入下一轮。
"""

import time
import random
import asyncio
from typing import Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import At, Plain


# ─── 数据模型 ───────────────────────────────────────────────

class Player:
    """玩家"""

    def __init__(self, user_id: str, user_name: str):
        self.user_id = user_id
        self.user_name = user_name
        self.last_roll: Optional[int] = None  # 最近一次 Roll 的点数


class GameSession:
    """游戏会话，每个群独立"""

    def __init__(self, group_id: str):
        self.group_id = group_id
        self.players: Dict[str, Player] = {}          # user_id -> Player
        self.player_order: List[str] = []             # 按加入顺序的 user_id 列表
        self.is_started = False                       # 游戏是否已开始
        self.current_round: int = 0                   # 当前轮次
        self.round_in_progress = False                # 当前轮是否正在进行中
        self.last_cooldown_end_time: float = 0.0     # 上一轮结束时间戳（用于冷却）
        # 当前轮数据（必须初始化，避免 reset_round 引用未定义属性）
        self.current_event_type: Optional[str] = None   # "truth" 或 "dare"
        self.current_event_text: Optional[str] = None    # 事件内容
        self.current_target_ids: List[str] = []          # 本轮被选中的玩家 user_id
        self.current_target_count: int = 0               # 本轮实际指定人数

    def add_player(self, user_id: str, user_name: str) -> bool:
        """添加玩家，返回是否成功"""
        if user_id in self.players:
            return False
        self.players[user_id] = Player(user_id, user_name)
        self.player_order.append(user_id)
        return True

    def remove_player(self, user_id: str) -> bool:
        """移除玩家，返回是否成功"""
        if user_id not in self.players:
            return False
        del self.players[user_id]
        if user_id in self.player_order:
            self.player_order.remove(user_id)
        return True

    def get_player_count(self) -> int:
        return len(self.players)

    def get_player_list_text(self) -> str:
        """获取玩家列表文本"""
        if not self.players:
            return "暂无玩家"
        lines = []
        for i, uid in enumerate(self.player_order, 1):
            p = self.players[uid]
            roll_info = f" [Roll: {p.last_roll}]" if p.last_roll is not None else ""
            lines.append(f"{i}. {p.user_name}{roll_info}")
        return "\n".join(lines)

    def reset_round(self):
        """重置轮次状态"""
        self.round_in_progress = False
        self.current_event_type = None
        self.current_event_text = None
        self.current_target_ids = []
        self.current_target_count = 0
        # 清除所有玩家的 Roll 记录
        for p in self.players.values():
            p.last_roll = None


# ─── 插件主类 ───────────────────────────────────────────────

class TruthOrDarePlugin(Star):
    """真心话大冒险插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.games: Dict[str, GameSession] = {}  # group_id -> GameSession

    # ── 工具方法 ──────────────────────────────────────────

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        """获取群号"""
        gid = event.get_group_id()
        return gid if gid else None

    def _get_session(self, group_id: str) -> GameSession:
        """获取或创建群会话"""
        if group_id not in self.games:
            self.games[group_id] = GameSession(group_id)
        return self.games[group_id]

    def _parse_truth_questions(self) -> List[str]:
        """解析真心话题库"""
        raw = self.config.get("truth_questions", "")
        if not raw:
            return []
        return [q.strip() for q in raw.strip().split("\n") if q.strip()]

    def _parse_dare_tasks(self) -> List[str]:
        """解析大冒险题库"""
        raw = self.config.get("dare_tasks", "")
        if not raw:
            return []
        return [t.strip() for t in raw.strip().split("\n") if t.strip()]

    def _pick_event(self) -> tuple:
        """
        随机决定事件类型和内容。
        返回 (event_type, event_text)
        """
        truth_weight = self.config.get("truth_weight", 50)
        is_truth = random.randint(1, 100) <= truth_weight

        if is_truth:
            questions = self._parse_truth_questions()
            if questions:
                return ("truth", random.choice(questions))
            else:
                return ("truth", "请说出一件关于你的真心话！")
        else:
            tasks = self._parse_dare_tasks()
            if tasks:
                return ("dare", random.choice(tasks))
            else:
                return ("dare", "请表演一个才艺！")

    def _check_cooldown(self, session: GameSession) -> Optional[float]:
        """检查冷却时间，返回剩余秒数或 None（已冷却完毕）"""
        cooldown = self.config.get("round_cooldown", 0)
        if cooldown <= 0:
            return None
        elapsed = time.time() - session.last_cooldown_end_time
        if elapsed < cooldown:
            return cooldown - elapsed
        return None

    def _calc_target_count(self, player_count: int) -> int:
        """
        根据玩家总数动态计算事件目标人数。

        规则：4人起，每增加2人，目标人数+1。
        - 4人 → 2人
        - 6人 → 3人
        - 8人 → 4人
        - 10人 → 5人
        以此类推，最少2人，不超过总人数。
        """
        if player_count < 4:
            return min(2, player_count)
        return min(max(2, (player_count - 4) // 2 + 2), player_count)

    # ── 指令处理 ──────────────────────────────────────────

    @filter.command("td_join", alias={"td加入", "tdjoin"})
    async def cmd_join(self, event: AstrMessageEvent):
        """加入真心话大冒险游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("⚠️ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        max_players = self.config.get("max_players", 50)

        if session.is_started:
            yield event.plain_result("⚠️ 游戏已经开始，请等待下一局再加入！")
            return

        if session.get_player_count() >= max_players:
            yield event.plain_result(f"⚠️ 游戏人数已达上限（{max_players}人），无法加入！")
            return

        if session.add_player(user_id, user_name):
            logger.info(f"[真心话大冒险] 玩家 {user_name}({user_id}) 加入群 {group_id} 的游戏")
            yield event.plain_result(
                f"✅ {user_name} 加入了真心话大冒险！\n"
                f"当前玩家数：{session.get_player_count()}"
            )
        else:
            yield event.plain_result(f"⚠️ {user_name} 已经在游戏中啦！")

    @filter.command("td_leave", alias={"td退出", "tdleave"})
    async def cmd_quit(self, event: AstrMessageEvent):
        """退出真心话大冒险游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if user_id not in session.players:
            yield event.plain_result(f"⚠️ {user_name} 不在游戏中！")
            return

        if session.is_started:
            yield event.plain_result("⚠️ 游戏正在进行中，请等待当前轮次结束后再退出！")
            return

        session.remove_player(user_id)
        yield event.plain_result(
            f"👋 {user_name} 退出了游戏。\n"
            f"当前玩家数：{session.get_player_count()}"
        )

    @filter.command("td_list", alias={"td列表", "tdlist"})
    async def cmd_player_list(self, event: AstrMessageEvent):
        """查看当前玩家列表"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        player_text = session.get_player_list_text()
        status = "🟢 游戏中" if session.is_started else "🟡 等待开始"
        yield event.plain_result(
            f"📋 真心话大冒险 - 玩家列表 [{status}]\n"
            f"人数：{session.get_player_count()}\n"
            f"────────────────\n"
            f"{player_text}"
        )

    @filter.command("td_start", alias={"td开始", "tdstart"})
    async def cmd_start(self, event: AstrMessageEvent):
        """开始真心话大冒险游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        min_players = self.config.get("min_players", 4)

        if session.is_started:
            yield event.plain_result("⚠️ 游戏已经开始啦！")
            return

        if session.get_player_count() < min_players:
            yield event.plain_result(
                f"⚠️ 至少需要 {min_players} 名玩家才能开始游戏！\n"
                f"当前玩家数：{session.get_player_count()}"
            )
            return

        session.is_started = True
        session.current_round = 0

        player_text = session.get_player_list_text()
        # 计算当前人数对应的目标人数
        target_count = self._calc_target_count(session.get_player_count())
        yield event.plain_result(
            f"🎉 真心话大冒险 正式开始！\n\n"
            f"👥 参与玩家（{session.get_player_count()}人）：\n"
            f"{player_text}\n\n"
            f"📌 请所有玩家发送 /td_roll 来 Roll 点！\n"
            f"📌 本轮将随机抽取 {target_count} 人完成事件！\n"
            f"📌 Roll 点最低的玩家优先被选中！"
        )

    @filter.command("td_roll", alias={"tdr", "tdroll"})
    async def cmd_roll(self, event: AstrMessageEvent):
        """玩家 Roll 点（仅群聊）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if not session.is_started:
            yield event.plain_result("⚠️ 游戏还没开始，请先发送 /td_start 开始游戏！")
            return

        if user_id not in session.players:
            yield event.plain_result("⚠️ 你不在游戏中，请先发送 /td_join 加入！")
            return

        # 执行 Roll 点
        roll_min = self.config.get("roll_min", 1)
        roll_max = self.config.get("roll_max", 100)
        roll_result = random.randint(roll_min, roll_max)
        session.players[user_id].last_roll = roll_result

        logger.info(
            f"[真心话大冒险] {user_name}({user_id}) Roll 出了 {roll_result}"
        )

        yield event.plain_result(
            f"🎲 {user_name} Roll 出了 {roll_result} 点！"
        )

    @filter.command("td_result", alias={"td结果", "tdresult"})
    async def cmd_roll_result(self, event: AstrMessageEvent):
        """查看所有玩家的 Roll 结果"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("⚠️ 游戏还没开始！")
            return

        # 检查是否所有人都 Roll 了
        rolled_count = sum(1 for p in session.players.values() if p.last_roll is not None)
        total = session.get_player_count()

        lines = []
        for uid in session.player_order:
            p = session.players[uid]
            roll_text = str(p.last_roll) if p.last_roll is not None else "未Roll"
            lines.append(f"  {p.user_name}：{roll_text}")

        yield event.plain_result(
            f"📊 Roll 点结果（{rolled_count}/{total} 已Roll）\n\n"
            + "\n".join(lines)
            + f"\n\n💡 发送 /td_go 让机器人处理事件！"
        )

    @filter.command("td_go", alias={"tdgo", "td下一轮"})
    async def cmd_go(self, event: AstrMessageEvent):
        """
        处理真心话大冒险事件。

        机器人根据 Roll 点结果随机指定玩家完成真心话或大冒险。
        目标人数根据玩家总数动态计算：4人=2人，每多2人多1人。
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("⚠️ 游戏还没开始，请先发送 /td_start 开始游戏！")
            return

        # 检查冷却
        remaining = self._check_cooldown(session)
        if remaining is not None:
            yield event.plain_result(f"⏳ 冷却中，请等待 {remaining:.0f} 秒后再开始下一轮！")
            return

        # 检查是否所有人都 Roll 了
        unrolled = [p for p in session.players.values() if p.last_roll is None]
        if unrolled:
            names = "、".join(p.user_name for p in unrolled)
            yield event.plain_result(
                f"⚠️ 以下玩家还没 Roll 点：\n{names}\n\n"
                f"请发送 /td_roll 完成 Roll 点后再继续！"
            )
            return

        # 找出 Roll 点最低的玩家
        all_players = list(session.players.values())
        min_roll = min(p.last_roll for p in all_players)
        lowest_players = [p for p in all_players if p.last_roll == min_roll]

        # 决定事件类型和内容
        event_type, event_text = self._pick_event()
        type_name = "💬 真心话" if event_type == "truth" else "🎯 大冒险"

        # 动态计算目标人数：4人→2人，每+2人→+1人
        target_count = self._calc_target_count(len(all_players))
        # 上限保护：实际目标人数不超过可选玩家总数
        actual_count = min(target_count, len(all_players))

        # 从最低 Roll 点玩家中选目标
        if len(lowest_players) >= actual_count:
            targets = random.sample(lowest_players, actual_count)
        else:
            # 最低 Roll 点玩家不够，从所有玩家中随机补足
            targets = lowest_players.copy()
            remaining_pool = [p for p in all_players if p not in lowest_players]
            extra_needed = actual_count - len(targets)
            if extra_needed > 0 and remaining_pool:
                extra = random.sample(remaining_pool, min(extra_needed, len(remaining_pool)))
                targets.extend(extra)

        # 记录本轮数据
        session.current_round += 1
        session.round_in_progress = True
        session.current_event_type = event_type
        session.current_event_text = event_text
        session.current_target_ids = [t.user_id for t in targets]
        session.current_target_count = len(targets)  # 使用实际选中人数

        # 构建回复（使用 At 组件 @ 被选中的玩家）
        target_names = "、".join(t.user_name for t in targets)
        at_chain = [At(qq=t.user_id) for t in targets]

        result = (
            f"🎪 第 {session.current_round} 轮\n\n"
            f"📊 Roll 点结果：\n"
        )
        for p in sorted(all_players, key=lambda x: x.last_roll):
            result += f"  {p.user_name}：{p.last_roll}\n"

        result += (
            f"\n🎯 被选中的玩家（{len(targets)}人）：{target_names}\n"
            f"{type_name}：{event_text}\n\n"
            f"💡 请 {target_names} 完成事件后，发送 /td_done 确认完成！\n"
            f"💡 发送 /td_skip 可以跳过本轮（需要被选中的玩家本人确认）"
        )

        logger.info(
            f"[真心话大冒险] 群 {group_id} 第 {session.current_round} 轮："
            f"类型={event_type}，目标={target_names}，事件={event_text}"
        )

        # 先发 @ 提醒，再发事件详情
        yield event.chain_result(at_chain + [Plain(result)])

    @filter.command("td_done", alias={"td完成", "tddone"})
    async def cmd_done(self, event: AstrMessageEvent):
        """完成当前事件，进入下一轮"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("⚠️ 游戏还没开始！")
            return

        if not session.round_in_progress:
            yield event.plain_result("⚠️ 当前没有进行中的事件，请先发送 /td_go 开始新一轮！")
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        # 只有被选中的玩家才能确认完成
        if user_id not in session.current_target_ids:
            yield event.plain_result(f"⚠️ {user_name} 不是本轮被选中的玩家，无法确认完成！")
            return

        # 重置轮次并进入下一轮
        session.reset_round()
        session.last_cooldown_end_time = time.time()

        yield event.plain_result(
            f"✅ {user_name} 完成了事件！\n\n"
            f"🎉 本轮结束！请发送 /td_go 开始下一轮！"
        )

    @filter.command("td_skip", alias={"td跳过", "tdskip"})
    async def cmd_skip(self, event: AstrMessageEvent):
        """跳过当前事件（需要被选中的玩家确认）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("⚠️ 游戏还没开始！")
            return

        if not session.round_in_progress:
            yield event.plain_result("⚠️ 当前没有进行中的事件！")
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if user_id not in session.current_target_ids:
            yield event.plain_result(f"⚠️ {user_name} 不是本轮被选中的玩家，无法跳过！")
            return

        event_type = session.current_event_type
        event_text = session.current_event_text
        type_label = "真心话" if event_type == "truth" else "大冒险"

        # 重置轮次
        session.reset_round()
        session.last_cooldown_end_time = time.time()

        yield event.plain_result(
            f"⏭️ {user_name} 跳过了本轮事件！\n"
            f"跳过的{type_label}：{event_text}\n\n"
            f"🎉 本轮结束！请发送 /td_go 开始下一轮！"
        )

    @filter.command("td_kick", alias={"td踢人", "tdkick"})
    async def cmd_kick(self, event: AstrMessageEvent):
        """管理员踢出玩家（解决玩家 AFK 导致游戏卡死的问题）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("⚠️ 游戏还没开始！")
            return

        # 检查发送者是否为群管理员（通过消息事件获取角色）
        sender_id = event.get_sender_id()
        sender_role = event.get_sender_role() if hasattr(event, 'get_sender_role') else None

        # 如果获取不到角色信息，允许任何玩家踢人（但只能踢自己）
        # 如果有角色信息，只允许管理员踢人
        is_admin = sender_role in ("owner", "admin") if sender_role else True

        # 从消息中提取被踢玩家
        message = event.get_message_str()
        # 格式: /td_kick @某人 或 /td_kick 玩家名
        # 尝试从 At 消息中提取
        at_targets = [comp for comp in event.message_obj.message if isinstance(comp, At)]
        if at_targets:
            target_id = str(at_targets[0].qq)
            if target_id not in session.players:
                yield event.plain_result("⚠️ 该玩家不在游戏中！")
                return
            target_name = session.players[target_id].user_name
        else:
            # 尝试从纯文本中提取玩家名
            parts = message.strip().split(None, 1)
            if len(parts) < 2:
                yield event.plain_result("⚠️ 请指定要踢出的玩家：/td_kick @玩家 或 /td_kick 玩家名")
                return
            target_name = parts[1].strip()
            # 按名字查找
            target_id = None
            for uid, p in session.players.items():
                if p.user_name == target_name:
                    target_id = uid
                    break
            if not target_id:
                yield event.plain_result(f"⚠️ 未找到玩家：{target_name}")
                return

        # 踢出玩家
        session.remove_player(target_id)
        # 如果被踢玩家是当前轮的目标，重置轮次
        if target_id in session.current_target_ids:
            session.reset_round()
            session.last_cooldown_end_time = time.time()
            yield event.plain_result(
                f"👢 {target_name} 被踢出游戏！\n"
                f"当前轮次已重置，请发送 /td_go 重新开始。\n"
                f"当前玩家数：{session.get_player_count()}"
            )
        else:
            yield event.plain_result(
                f"👢 {target_name} 被踢出游戏！\n"
                f"当前玩家数：{session.get_player_count()}"
            )

        # 如果玩家不够了，自动结束游戏
        min_players = self.config.get("min_players", 4)
        if session.get_player_count() < min_players:
            total_rounds = session.current_round
            session.is_started = False
            session.reset_round()
            session.players.clear()
            session.player_order.clear()
            yield event.plain_result(
                f"⚠️ 玩家数量不足 {min_players} 人，游戏自动结束！\n"
                f"总共进行了 {total_rounds} 轮。感谢大家的参与！🎉"
            )

    @filter.command("td_stop", alias={"td结束", "tdstop"})
    async def cmd_stop(self, event: AstrMessageEvent):
        """结束游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("⚠️ 游戏还没开始！")
            return

        total_rounds = session.current_round
        session.is_started = False
        session.reset_round()
        session.players.clear()
        session.player_order.clear()

        yield event.plain_result(
            f"🛑 游戏结束！\n"
            f"总共进行了 {total_rounds} 轮。\n"
            f"感谢大家的参与！🎉"
        )

    @filter.command("td_help", alias={"td帮助", "tdhelp"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助信息"""
        yield event.plain_result(
            "🎲 真心话大冒险 - 游戏帮助\n\n"
            "📌 游戏流程：\n"
            "1️⃣ /td_join  - 加入游戏\n"
            "2️⃣ /td_start - 开始游戏（至少4人）\n"
            "3️⃣ /td_roll  - 所有玩家 Roll 点\n"
            "4️⃣ /td_go    - 机器人处理事件（随机指定玩家）\n"
            "5️⃣ /td_done  - 完成事件，进入下一轮\n"
            "6️⃣ /td_skip  - 跳过当前事件\n\n"
            "📋 其他指令：\n"
            "/td_list   - 查看玩家列表\n"
            "/td_result - 查看 Roll 点结果\n"
            "/td_leave  - 退出游戏\n"
            "/td_kick   - 踢出 AFK 玩家（管理员）\n"
            "/td_stop   - 结束游戏\n"
            "/td_help   - 显示此帮助\n\n"
            "📐 动态人数规则：\n"
            "4人→抽2人 | 6人→抽3人 | 8人→抽4人\n"
            "每增加2人，事件目标人数+1\n\n"
            "💡 所有指令均以 td_ 开头，避免与其他插件冲突"
        )

    # ── 生命周期 ──────────────────────────────────────────

    async def initialize(self):
        """插件初始化"""
        logger.info("[真心话大冒险] 插件已加载！")

    async def terminate(self):
        """插件卸载清理"""
        self.games.clear()
        logger.info("[真心话大冒险] 插件已卸载！")