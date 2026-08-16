"""
astrbot_plugin_truth_or_dare - 真心话大冒险

群聊派对游戏插件。玩家加入游戏后通过 Roll 点决定命运，
机器人随机指定玩家完成真心话或大冒险事件，完成后再进入下一轮。
"""

import time
import random
import asyncio
import json
import os
import re
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
        self.current_round_start_time: float = 0.0   # 当前轮开始时间戳（用于超时自动跳过）
        # 当前轮数据（必须初始化，避免 reset_round 引用未定义属性）
        self.current_event_type: Optional[str] = None   # "truth" 或 "dare"
        self.current_event_text: Optional[str] = None    # 事件内容
        self.current_target_ids: List[str] = []          # 本轮被选中的玩家 user_id
        self.current_target_count: int = 0               # 本轮实际指定人数
        # 管理员手动指定的本轮目标（优先级高于 Roll 点）
        self.designated_ids: List[str] = []              # 指定的玩家 user_id 列表
        # 本轮已确认完成事件的被选中玩家
        self.completed_target_ids: List[str] = []

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
        self.current_round_start_time = 0.0
        self.current_event_type = None
        self.current_event_text = None
        self.current_target_ids = []
        self.current_target_count = 0
        # 清除手动指定名单（本轮已用完，避免影响下一轮）
        self.designated_ids = []
        # 清除本轮已完成确认记录
        self.completed_target_ids = []
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
        """解析真心话题库（支持换行、逗号、分号、顿号分隔）"""
        raw = self.config.get("truth_questions", "")
        if not raw:
            return []
        parts = re.split(r"[\n,，;；、|]+", raw)
        return [q.strip() for q in parts if q.strip()]

    def _parse_dare_tasks(self) -> List[str]:
        """解析大冒险题库（支持换行、逗号、分号、顿号分隔）"""
        raw = self.config.get("dare_tasks", "")
        if not raw:
            return []
        parts = re.split(r"[\n,，;；、|]+", raw)
        return [t.strip() for t in parts if t.strip()]

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

    def _finish_round(self, session: GameSession):
        """结算当前轮：轮次 +1 并重置本轮状态，记录冷却开始时间"""
        session.current_round += 1
        session.reset_round()
        session.last_cooldown_end_time = time.time()

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

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断发送者是否为群主/管理员（兼容中英文角色字段；可回退到 config 中的 admin_ids）"""
        sender_role = (
            event.get_sender_role() if hasattr(event, "get_sender_role") else None
        )
        if sender_role:
            # 归一化角色字段，兼容不同平台的大小写/中文取值
            role_key = str(sender_role).strip().lower()
            if role_key in ("owner", "admin", "administrator", "creator", "群主", "管理员", "管理"):
                return True
        # 兜底：匹配配置中指定的管理员 user_id
        admin_ids = self.config.get("admin_ids", []) or []
        if admin_ids:
            try:
                sender_id = int(event.get_sender_id())
            except (ValueError, TypeError):
                sender_id = None
            if sender_id is not None and sender_id in admin_ids:
                return True
        return False

    def _to_at_id(self, user_id) -> int:
        """将 user_id 转换为 At 组件所需的 int 类型，失败则原样返回"""
        try:
            return int(user_id)
        except (ValueError, TypeError):
            return user_id

    def _check_round_timeout(self):
        """检测进行中的轮次是否超时，超时则自动跳过（解决 AFK 卡死）"""
        timeout = self.config.get("round_timeout", 0)
        if timeout <= 0:
            return
        now = time.time()
        for gid, session in self.games.items():
            if not session.is_started or not session.round_in_progress:
                continue
            if session.current_round_start_time and (
                now - session.current_round_start_time >= timeout
            ):
                session.reset_round()
                session.last_cooldown_end_time = now
                logger.info(
                    f"[真心话大冒险] 群 {gid} 第 {session.current_round} 轮超时，已自动跳过"
                )

    # ── 数据持久化 ──────────────────────────────────────────

    def _get_data_path(self) -> str:
        """获取数据持久化文件路径"""
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "game_sessions.json")

    def _save_sessions(self):
        """保存所有游戏会话到磁盘"""
        try:
            data = {}
            for gid, session in self.games.items():
                if not session.players:
                    continue
                data[gid] = {
                    "players": [
                        {
                            "user_id": uid,
                            "user_name": session.players[uid].user_name,
                            "last_roll": session.players[uid].last_roll,
                        }
                        for uid in session.player_order
                    ],
                    "player_order": session.player_order,
                    "is_started": session.is_started,
                    "current_round": session.current_round,
                }
            with open(self._get_data_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[真心话大冒险] 保存游戏数据失败: {e}")

    def _load_sessions(self):
        """从磁盘加载游戏会话"""
        try:
            path = self._get_data_path()
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for gid, sdata in data.items():
                session = GameSession(gid)
                for pdata in sdata.get("players", []):
                    p = Player(pdata["user_id"], pdata["user_name"])
                    p.last_roll = pdata.get("last_roll")
                    session.players[p.user_id] = p
                session.player_order = sdata.get("player_order", [])
                session.is_started = sdata.get("is_started", False)
                session.current_round = sdata.get("current_round", 0)
                # 重启后不恢复进行中的轮次状态，需要重新 Roll
                session.round_in_progress = False
                session.current_event_type = None
                session.current_event_text = None
                session.current_target_ids = []
                session.current_target_count = 0
                session.completed_target_ids = []
                self.games[gid] = session
            if self.games:
                logger.info(f"[真心话大冒险] 已恢复 {len(self.games)} 个群的游戏会话")
        except Exception as e:
            logger.error(f"[真心话大冒险] 加载游戏数据失败: {e}")

    async def _periodic_save(self):
        """定时自动保存游戏数据（每 30 秒）并检测轮次超时"""
        while True:
            await asyncio.sleep(30)
            self._check_round_timeout()
            self._save_sessions()

    # ── 指令处理 ──────────────────────────────────────────

    @filter.command("td_join", alias={"td加入", "tdjoin"})
    async def cmd_join(self, event: AstrMessageEvent):
        """加入真心话大冒险游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        max_players = self.config.get("max_players", 50)

        if session.is_started:
            yield event.plain_result("游戏已经开始，请等待下一局再加入！")
            return

        if session.get_player_count() >= max_players:
            yield event.plain_result(f"游戏人数已达上限（{max_players}人），无法加入！")
            return

        if session.add_player(user_id, user_name):
            logger.info(f"[真心话大冒险] 玩家 {user_name}({user_id}) 加入群 {group_id} 的游戏")
            yield event.plain_result(
                f"{user_name} 加入了真心话大冒险！\n"
                f"当前玩家数：{session.get_player_count()}"
            )
        else:
            yield event.plain_result(f"{user_name} 已经在游戏中啦！")

    @filter.command("td_leave", alias={"td退出", "tdleave"})
    async def cmd_quit(self, event: AstrMessageEvent):
        """退出真心话大冒险游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if user_id not in session.players:
            yield event.plain_result(f"{user_name} 不在游戏中！")
            return

        if session.is_started:
            yield event.plain_result("游戏正在进行中，请等待当前轮次结束后再退出！")
            return

        session.remove_player(user_id)
        yield event.plain_result(
            f"{user_name} 退出了游戏。\n"
            f"当前玩家数：{session.get_player_count()}"
        )

    @filter.command("td_list", alias={"td列表", "tdlist"})
    async def cmd_player_list(self, event: AstrMessageEvent):
        """查看当前玩家列表"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        player_text = session.get_player_list_text()
        status = "游戏中" if session.is_started else "等待开始"
        yield event.plain_result(
            f"真心话大冒险 - 玩家列表 [{status}]\n"
            f"人数：{session.get_player_count()}\n"
            f"────────────────\n"
            f"{player_text}"
        )

    @filter.command("td_start", alias={"td开始", "tdstart"})
    async def cmd_start(self, event: AstrMessageEvent):
        """开始真心话大冒险游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        min_players = self.config.get("min_players", 4)

        if session.is_started:
            yield event.plain_result("游戏已经开始啦！")
            return

        if session.get_player_count() < min_players:
            yield event.plain_result(
                f"至少需要 {min_players} 名玩家才能开始游戏！\n"
                f"当前玩家数：{session.get_player_count()}"
            )
            return

        session.is_started = True
        session.current_round = 0

        player_text = session.get_player_list_text()
        # 计算当前人数对应的目标人数
        target_count = self._calc_target_count(session.get_player_count())
        yield event.plain_result(
            f"真心话大冒险 正式开始！\n\n"
            f"参与玩家（{session.get_player_count()}人）：\n"
            f"{player_text}\n\n"
            f"请所有玩家发送 /td_roll 来 Roll 点！\n"
            f"本轮将随机抽取 {target_count} 人完成事件！\n"
            f"Roll 点最低的玩家优先被选中！"
        )

    @filter.command("td_roll", alias={"tdr", "tdroll"})
    async def cmd_roll(self, event: AstrMessageEvent):
        """玩家 Roll 点（仅群聊）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if not session.is_started:
            yield event.plain_result("游戏还没开始，请先发送 /td_start 开始游戏！")
            return

        if session.round_in_progress:
            yield event.plain_result(
                "当前轮次正在进行中，请完成或跳过本轮（/td_done 或 /td_skip）后再开始下一轮！"
            )
            return

        if user_id not in session.players:
            yield event.plain_result("你不在游戏中，请先发送 /td_join 加入！")
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
            f"{user_name} Roll 出了 {roll_result} 点！"
        )

    @filter.command("td_result", alias={"td结果", "tdresult"})
    async def cmd_roll_result(self, event: AstrMessageEvent):
        """查看所有玩家的 Roll 结果"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始！")
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
            f"Roll 点结果（{rolled_count}/{total} 已Roll）\n\n"
            + "\n".join(lines)
            + f"\n\n发送 /td_go 让机器人处理事件！"
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
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始，请先发送 /td_start 开始游戏！")
            return

# 本轮已在进行：必须先由被选中玩家结算（/td_done 或 /td_skip），才能开新一轮
        # 这一拦截防止任何人反复 /td_go 刷取目标或刷高轮次
        if session.round_in_progress:
            in_progress_names = "、".join(
                session.players[uid].user_name
                for uid in session.current_target_ids
                if uid in session.players
            )
            yield event.plain_result(
                f"本轮正在进行中！请 {in_progress_names} 完成事件后发送 /td_done 确认，"
                f"或由被选中的玩家发送 /td_skip 跳过！"
            )
            return

        # 检查冷却
        remaining = self._check_cooldown(session)
        if remaining is not None:
            yield event.plain_result(f"冷却中，请等待 {remaining:.0f} 秒后再开始下一轮！")
            return

        # 检查 Roll 状态：未被管理员手动指定的玩家都必须 Roll（修复原「指定时全员免 Roll」漏洞）
        unrolled = [
            p for p in session.players.values()
            if p.last_roll is None and p.user_id not in session.designated_ids
        ]
        if unrolled:
            names = "、".join(p.user_name for p in unrolled)
            yield event.plain_result(
                f"以下玩家（未被手动指定）还没 Roll 点：\n{names}\n\n"
                f"请发送 /td_roll 完成 Roll 点后再继续！"
            )
            return

        # 找出 Roll 点最低的玩家
        all_players = list(session.players.values())
        min_roll = min(p.last_roll for p in all_players) if all(
            p.last_roll is not None for p in all_players
        ) else None
        lowest_players = (
            [p for p in all_players if p.last_roll == min_roll]
            if min_roll is not None else []
        )

        # 决定事件类型和内容
        event_type, event_text = self._pick_event()
        type_name = "真心话" if event_type == "truth" else "大冒险"

        # 动态计算目标人数：4人→2人，每+2人→+1人
        target_count = self._calc_target_count(len(all_players))
        # 上限保护：实际目标人数不超过可选玩家总数
        actual_count = min(target_count, len(all_players))

        # 优先使用管理员手动指定的目标
        targets: List = []
        is_designated = False
        designated_note = ""
        if session.designated_ids:
            designated_requested = list(session.designated_ids)
            for uid in designated_requested:
                if uid in session.players:
                    targets.append(session.players[uid])
            # 限缩到目标人数
            if len(targets) > actual_count:
                targets = targets[:actual_count]
            is_designated = True
            if not targets:
                # 指定的玩家均已不在游戏中，提示并清空后按 Roll 点逻辑处理
                designated_note = (
                    "\n（管理员手动指定的玩家已不在游戏中，本轮改按 Roll 点结果选择）"
                )
                session.designated_ids = []

        # 若无指定，走原有 Roll 点逻辑
        if not targets:
            if not lowest_players:
                yield event.plain_result("没有可用的 Roll 点结果，请先 /td_roll！")
                return
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

        # 记录本轮数据（轮次编号 = 已结算轮数 + 1，递增移到 _finish_round）
        session.round_in_progress = True
        session.current_round_start_time = time.time()
        session.current_event_type = event_type
        session.current_event_text = event_text
        session.current_target_ids = [t.user_id for t in targets]
        session.current_target_count = len(targets)  # 使用实际选中人数
        # 重置本轮逐人确认状态（让所有被选中的玩家重新走 /td_done 确认）
        session.completed_target_ids = []

        # 构建回复（使用 At 组件 @ 被选中的玩家）
        target_names = "、".join(t.user_name for t in targets)
        at_chain = [At(qq=self._to_at_id(t.user_id)) for t in targets]

        result = (
            f"第 {session.current_round + 1} 轮\n\n"
            f"Roll 点结果：\n"
        )
        # 排序键：为 last_roll 为 None 的玩家提供无穷大兜底，避免 None 与 int 比较崩溃
        for p in sorted(
            all_players,
            key=lambda x: x.last_roll if x.last_roll is not None else float("inf"),
        ):
            roll_text = str(p.last_roll) if p.last_roll is not None else "未Roll"
            result += f"  {p.user_name}：{roll_text}\n"

        result += (
            f"\n被选中的玩家（{len(targets)}人）：{target_names}\n"
            f"{type_name}：{event_text}\n\n"
            f"请 {target_names} 完成事件后，发送 /td_done 确认完成！\n"
            f"发送 /td_skip 可以跳过本轮（需要被选中的玩家本人确认）"
        )
        if is_designated:
            result += "\n（本轮由管理员手动指定）"
        if designated_note:
            result += designated_note

        logger.info(
            f"[真心话大冒险] 群 {group_id} 第 {session.current_round + 1} 轮："
            f"类型={event_type}，目标={target_names}，事件={event_text}"
        )

        # 先发 @ 提醒，再发事件详情
        yield event.chain_result(at_chain + [Plain(result)])

    @filter.command("td_指定", alias={"td指定", "tddesignate"})
    async def cmd_designate(self, event: AstrMessageEvent):
        """
        管理员手动指定本轮事件目标（优先级高于 Roll 点）。

        用法：/td_指定 @玩家1 @玩家2 ...
             /td_指定 玩家名1 玩家名2 ...
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始，请先发送 /td_start 开始游戏！")
            return

# 权限检查：仅群主/管理员可指定（判定由 _is_admin 统一处理，兼容角色字段与 admin_ids）
        sender_name = event.get_sender_name()
        if not self._is_admin(event):
            yield event.plain_result(
                f"{sender_name}，你没有管理员权限，无法手动指定事件目标！"
            )
            return

        # 解析指定目标：优先从 At 组件，再从纯文本名字
        target_ids: List[str] = []
        target_names: List[str] = []

        # 从 At 组件提取
        at_targets = [comp for comp in event.message_obj.message if isinstance(comp, At)]
        for at in at_targets:
            uid = str(at.qq)
            if uid in session.players and uid not in target_ids:
                target_ids.append(uid)
                target_names.append(session.players[uid].user_name)

        # 从纯文本名字提取（去掉指令本身）
        message = event.get_message_str()
        parts = message.strip().split()
        # 第一段是指令名（/td_指定 或别名），跳过
        if parts:
            parts = parts[1:]
        for name in parts:
            name = name.strip()
            if not name or name.startswith("@"):
                continue
            # 按名字查找
            for uid, p in session.players.items():
                if p.user_name == name and uid not in target_ids:
                    target_ids.append(uid)
                    target_names.append(p.user_name)
                    break

        if not target_ids:
            yield event.plain_result(
                "请指定要手动指定的玩家：\n"
                "/td_指定 @玩家1 @玩家2 ...\n"
                "/td_指定 玩家名1 玩家名2 ..."
            )
            return

        # 限制指定人数不超过动态计算的目标人数（避免一次指定过多）
        target_count = self._calc_target_count(session.get_player_count())
        if len(target_ids) > target_count:
            yield event.plain_result(
                f"指定人数过多！当前游戏最多可指定 {target_count} 人（本局 {session.get_player_count()} 人）。"
            )
            return

        # 保存指定名单（用于本轮 /td_go）
        session.designated_ids = target_ids
        names_str = "、".join(target_names)
        yield event.plain_result(
            f"已手动指定本轮目标玩家（{len(target_ids)}人）：{names_str}\n\n"
            f"下次发送 /td_go 时，将优先让这些玩家完成事件。\n"
            f"如需取消指定，请发送 /td_指定清除"
        )

    @filter.command("td_指定清除", alias={"td指定清除", "tddesignate_clear"})
    async def cmd_designate_clear(self, event: AstrMessageEvent):
        """清除本轮手动指定的目标"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始！")
            return

        sender_name = event.get_sender_name()
        if not self._is_admin(event):
            yield event.plain_result(
                f"{sender_name}，你没有管理员权限，无法清除指定！"
            )
            return

        if not session.designated_ids:
            yield event.plain_result("本轮没有手动指定的目标。")
            return

        session.designated_ids = []
        yield event.plain_result("已清除本轮手动指定的目标，下次 /td_go 将按 Roll 点选择。")

    @filter.command("td_done", alias={"td完成", "tddone"})
    async def cmd_done(self, event: AstrMessageEvent):
        """完成当前事件，进入下一轮"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始！")
            return

        if not session.round_in_progress:
            yield event.plain_result("当前没有进行中的事件，请先发送 /td_go 开始新一轮！")
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        # 只有被选中的玩家才能确认完成
        if user_id not in session.current_target_ids:
            yield event.plain_result(f"{user_name} 不是本轮被选中的玩家，无法确认完成！")
            return

        # 防止同一玩家重复确认
        if user_id in session.completed_target_ids:
            yield event.plain_result(f"{user_name} 已经确认过完成本轮事件了！")
            return

        # 记录该玩家已完成
        session.completed_target_ids.append(user_id)

        # 仍有其他被选中玩家未完成 → 等待，不进入下一轮
        remaining_ids = [
            uid for uid in session.current_target_ids
            if uid not in session.completed_target_ids
        ]
        if remaining_ids:
            remaining_names = "、".join(
                session.players[uid].user_name
                for uid in remaining_ids if uid in session.players
            )
            yield event.plain_result(
                f"{user_name} 已完成事件！\n\n"
                f"本轮共有 {len(session.current_target_ids)} 名被选中的玩家，"
                f"还有 {len(remaining_ids)} 人未完成：\n{remaining_names}\n\n"
                f"请他们完成后发送 /td_done 确认。"
            )
            return

        # 全部被选中玩家均确认完成 → 统一结算本轮
        self._finish_round(session)

        yield event.plain_result(
            f"{user_name} 完成了事件！\n\n"
            f"所有被选中的玩家均已完成，本轮结束！\n"
            f"请发送 /td_go 开始下一轮！"
        )

    @filter.command("td_skip", alias={"td跳过", "tdskip"})
    async def cmd_skip(self, event: AstrMessageEvent):
        """跳过当前事件（需要被选中的玩家确认）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始！")
            return

        if not session.round_in_progress:
            yield event.plain_result("当前没有进行中的事件！")
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if user_id not in session.current_target_ids:
            yield event.plain_result(f"{user_name} 不是本轮被选中的玩家，无法跳过！")
            return

        event_type = session.current_event_type
        event_text = session.current_event_text
        type_label = "真心话" if event_type == "truth" else "大冒险"

        # 跳过视为结算本轮：统一走 _finish_round（递增轮次 + 重置状态 + 启动冷却）
        self._finish_round(session)

        yield event.plain_result(
            f"{user_name} 跳过了本轮事件！\n"
            f"跳过的{type_label}：{event_text}\n\n"
            f"本轮结束！请发送 /td_go 开始下一轮！"
        )

    @filter.command("td_kick", alias={"td踢人", "tdkick"})
    async def cmd_kick(self, event: AstrMessageEvent):
        """管理员踢出玩家（解决玩家 AFK 导致游戏卡死的问题）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始！")
            return

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        # 管理员判定（群主/群管理 或 配置中的 admin_ids，由 _is_admin 统一处理）
        is_admin = self._is_admin(event)

        # 从消息中提取被踢玩家
        message = event.get_message_str()
        # 格式: /td_kick @某人 或 /td_kick 玩家名
        # 尝试从 At 消息中提取
        at_targets = [comp for comp in event.message_obj.message if isinstance(comp, At)]
        if at_targets:
            target_id = str(at_targets[0].qq)
            if target_id not in session.players:
                yield event.plain_result("该玩家不在游戏中！")
                return
            target_name = session.players[target_id].user_name
        else:
            # 尝试从纯文本中提取玩家名
            parts = message.strip().split(None, 1)
            if len(parts) < 2:
                yield event.plain_result("请指定要踢出的玩家：/td_kick @玩家 或 /td_kick 玩家名")
                return
            target_name = parts[1].strip()
            # 按名字查找
            target_id = None
            for uid, p in session.players.items():
                if p.user_name == target_name:
                    target_id = uid
                    break
            if not target_id:
                yield event.plain_result(f"未找到玩家：{target_name}")
                return

        # 权限检查：管理员可踢任意玩家；非管理员仅可踢自己
        if not is_admin and target_id != sender_id:
            yield event.plain_result(
                f"{sender_name}，你没有管理员权限，只能踢出自己！"
            )
            return

        # 踢出玩家
        was_target = target_id in session.current_target_ids
        session.remove_player(target_id)
        if was_target:
            # 踢掉的是当前轮被选中的玩家 → 统一走 _finish_round 结算（递增轮次+重置+冷却）
            self._finish_round(session)

        # 检查人数是否不足，不足则自动结束游戏
        min_players = self.config.get("min_players", 4)
        if session.get_player_count() < min_players:
            # 计入正在进行但尚未结算的轮次，让 "总共进行了 N 轮" 更贴近实际
            total_rounds = session.current_round + (1 if session.round_in_progress else 0)
            session.is_started = False
            session.players.clear()
            session.player_order.clear()
            yield event.plain_result(
                f"{target_name} 被踢出游戏！\n"
                f"玩家数量不足 {min_players} 人，游戏自动结束！\n"
                f"总共进行了 {total_rounds} 轮。感谢大家的参与！"
            )
        elif was_target:
            yield event.plain_result(
                f"{target_name} 被踢出游戏！\n"
                f"当前轮次已结算，请发送 /td_go 开始新一轮。\n"
                f"当前玩家数：{session.get_player_count()}"
            )
        else:
            yield event.plain_result(
                f"{target_name} 被踢出游戏！\n"
                f"当前玩家数：{session.get_player_count()}"
            )

    @filter.command("td_stop", alias={"td结束", "tdstop"})
    async def cmd_stop(self, event: AstrMessageEvent):
        """结束游戏"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始！")
            return

        total_rounds = session.current_round + (1 if session.round_in_progress else 0)
        session.is_started = False
        session.reset_round()
        session.players.clear()
        session.player_order.clear()

        yield event.plain_result(
            f"游戏结束！\n"
            f"总共进行了 {total_rounds} 轮。\n"
            f"感谢大家的参与！"
        )

    @filter.command("td_help", alias={"td帮助", "tdhelp"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助信息"""
        yield event.plain_result(
            "真心话大冒险 - 游戏帮助\n\n"
            "游戏流程：\n"
            "1. /td_join  - 加入游戏\n"
            "2. /td_start - 开始游戏（至少4人）\n"
            "3. /td_roll  - 所有玩家 Roll 点\n"
            "4. /td_go    - 机器人处理事件（随机指定玩家）\n"
            "5. /td_done  - 完成事件，进入下一轮\n"
            "6. /td_skip  - 跳过当前事件\n\n"
            "其他指令：\n"
            "/td_list       - 查看玩家列表\n"
            "/td_result     - 查看 Roll 点结果\n"
            "/td_leave      - 退出游戏\n"
            "/td_kick       - 踢出 AFK 玩家（管理员）\n"
            "/td_指定       - 手动指定本轮目标（管理员，优先级高于 Roll 点）\n"
            "/td_指定清除   - 清除本轮手动指定的目标\n"
            "/td_stop       - 结束游戏\n"
            "/td_help       - 显示此帮助\n\n"
            "动态人数规则：\n"
            "4人→抽2人 | 6人→抽3人 | 8人→抽4人\n"
            "每增加2人，事件目标人数+1\n\n"
            "所有指令均以 td_ 开头，避免与其他插件冲突"
        )

    # ── 生命周期 ──────────────────────────────────────────

    async def initialize(self):
        """插件初始化"""
        self._load_sessions()
        self._save_task = asyncio.create_task(self._periodic_save())
        logger.info("[真心话大冒险] 插件已加载！")

    async def terminate(self):
        """插件卸载清理"""
        if hasattr(self, '_save_task') and self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        self._save_sessions()
        self.games.clear()
        logger.info("[真心话大冒险] 插件已卸载！")