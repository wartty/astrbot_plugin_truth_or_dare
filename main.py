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
        self.last_cooldown_end_time: float = 0.0      # 上一轮结束时间戳（用于冷却）
        self.current_round_start_time: float = 0.0    # 当前轮开始时间戳（用于超时自动跳过）
        self.current_target_ids: List[str] = []           # 本轮被选中的玩家 user_id
        self.current_target_count: int = 0                # 本轮实际指定人数
        # 新指定机制相关
        self.designator_id: Optional[str] = None          # 获得指定权的玩家 user_id（由6种算法随机选出）
        self.designated_target_id: Optional[str] = None   # 被指定的目标玩家 user_id
        self.designated_event_type: Optional[str] = None  # 被指定玩家的事件类型 (truth/dare/None=随机)
        self.selection_algorithm: Optional[int] = None    # 本轮使用的选择算法索引 0-5
        # 每个目标玩家独立的事件：user_id -> (event_type, event_text)
        self.target_events: Dict[str, tuple] = {}
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
        self.current_target_ids = []
        self.current_target_count = 0
        # 清除新指定机制相关字段
        self.designator_id = None
        self.designated_target_id = None
        self.designated_event_type = None
        self.selection_algorithm = None
        self.target_events = {}
        # 清除本轮已完成确认记录
        self.completed_target_ids = []
        # 清除所有玩家的 Roll 记录
        for p in self.players.values():
            p.last_roll = None


# ─── 插件主类 ───────────────────────────────────────────────

@filter.command_group("td")
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

    def _select_by_algorithm(self, players: List[Player], algorithm: int) -> Optional[Player]:
        """
        根据指定算法从玩家列表中选择一名玩家。

        算法索引 0-5：
        0: 点数最大
        1: 点数最小
        2: 单数点数最大
        3: 单数点数最小
        4: 双数点数最大
        5: 双数点数最小

        返回选中的玩家，如果没有符合条件的玩家则返回 None
        """
        if not players:
            return None

        # 过滤出有效 roll 的玩家
        valid_players = [p for p in players if p.last_roll is not None]
        if not valid_players:
            return None

        if algorithm == 0:  # 点数最大
            max_roll = max(p.last_roll for p in valid_players)
            candidates = [p for p in valid_players if p.last_roll == max_roll]
        elif algorithm == 1:  # 点数最小
            min_roll = min(p.last_roll for p in valid_players)
            candidates = [p for p in valid_players if p.last_roll == min_roll]
        elif algorithm == 2:  # 单数点数最大
            odd_players = [p for p in valid_players if p.last_roll % 2 == 1]
            if not odd_players:
                # 无单数玩家，回退为点数最大
                max_roll = max(p.last_roll for p in valid_players)
                candidates = [p for p in valid_players if p.last_roll == max_roll]
            else:
                max_roll = max(p.last_roll for p in odd_players)
                candidates = [p for p in odd_players if p.last_roll == max_roll]
        elif algorithm == 3:  # 单数点数最小
            odd_players = [p for p in valid_players if p.last_roll % 2 == 1]
            if not odd_players:
                # 无单数玩家，回退为点数最小
                min_roll = min(p.last_roll for p in valid_players)
                candidates = [p for p in valid_players if p.last_roll == min_roll]
            else:
                min_roll = min(p.last_roll for p in odd_players)
                candidates = [p for p in odd_players if p.last_roll == min_roll]
        elif algorithm == 4:  # 双数点数最大
            even_players = [p for p in valid_players if p.last_roll % 2 == 0]
            if not even_players:
                # 无双数玩家，回退为点数最大
                max_roll = max(p.last_roll for p in valid_players)
                candidates = [p for p in valid_players if p.last_roll == max_roll]
            else:
                max_roll = max(p.last_roll for p in even_players)
                candidates = [p for p in even_players if p.last_roll == max_roll]
        elif algorithm == 5:  # 双数点数最小
            even_players = [p for p in valid_players if p.last_roll % 2 == 0]
            if not even_players:
                # 无双数玩家，回退为点数最小
                min_roll = min(p.last_roll for p in valid_players)
                candidates = [p for p in valid_players if p.last_roll == min_roll]
            else:
                min_roll = min(p.last_roll for p in even_players)
                candidates = [p for p in even_players if p.last_roll == min_roll]
        else:
            return None

        if candidates:
            return random.choice(candidates)
        return None

    def _select_multiple_by_algorithm(self, players: List[Player], algorithm: int, count: int, exclude_ids: List[str] = None) -> List[Player]:
        """
        根据指定算法从玩家列表中选择多名玩家（用于系统补足剩余名额）。

        返回选中的玩家列表
        """
        if not players or count <= 0:
            return []

        exclude_ids = exclude_ids or []
        # 过滤出有效 roll 且未被排除的玩家
        valid_players = [p for p in players if p.last_roll is not None and p.user_id not in exclude_ids]
        if not valid_players:
            return []

        selected = []
        remaining_count = count
        remaining_players = valid_players.copy()

        while remaining_count > 0 and remaining_players:
            player = self._select_by_algorithm(remaining_players, algorithm)
            if player is None:
                break
            selected.append(player)
            remaining_players.remove(player)
            remaining_count -= 1

        return selected


    async def _process_designation(self, session: GameSession, event: AstrMessageEvent):
        """处理指定完成后的逻辑：补足剩余名额、分配事件、开始轮次"""
        group_id = session.group_id
        all_players = list(session.players.values())

        # 动态计算目标人数
        target_count = self._calc_target_count(len(all_players))
        actual_count = min(target_count, len(all_players))

        # 已经有 1 个被指定的目标
        designated_player = session.players[session.designated_target_id]

        # 为被指定的玩家分配事件（如果指定了类型则用指定类型，否则随机）
        if session.designated_event_type == "truth":
            # 强制真心话：从真心话题库随机
            questions = self._parse_truth_questions()
            event_text = random.choice(questions) if questions else "请说出一件关于你的真心话！"
            event_type = "truth"
        elif session.designated_event_type == "dare":
            # 强制大冒险：从大冒险题库随机
            tasks = self._parse_dare_tasks()
            event_text = random.choice(tasks) if tasks else "请表演一个才艺！"
            event_type = "dare"
        else:
            # 未指定：随机
            event_type, event_text = self._pick_event()
        session.target_events[session.designated_target_id] = (event_type, event_text)

        # 如果还需要更多目标，用相同算法补足
        remaining_count = actual_count - 1
        additional_targets = []
        if remaining_count > 0:
            exclude_ids = [session.designated_target_id]
            additional_targets = self._select_multiple_by_algorithm(
                all_players, session.selection_algorithm, remaining_count, exclude_ids
            )

            # 为每个额外目标随机分配事件
            for player in additional_targets:
                et, et_text = self._pick_event()
                session.target_events[player.user_id] = (et, et_text)

        # 合并所有目标
        all_targets = [designated_player] + additional_targets
        session.current_target_ids = [p.user_id for p in all_targets]
        session.current_target_count = len(all_targets)

        # 标记轮次开始
        session.round_in_progress = True
        session.current_round_start_time = time.time()

        # 清除逐人确认状态
        session.completed_target_ids = []

        # 构建回复消息
        target_names = "、".join(p.user_name for p in all_targets)
        at_chain = [At(qq=self._to_at_id(p.user_id)) for p in all_targets]

        result = (
            f"第 {session.current_round + 1} 轮\n\n"
            f"Roll 点结果：\n"
        )
        for p in sorted(all_players, key=lambda x: x.last_roll if x.last_roll is not None else float("inf")):
            roll_text = str(p.last_roll) if p.last_roll is not None else "未Roll"
            result += f"  {p.user_name}：{roll_text}\n"

        # 不透露算法，仅显示指定者
        result += f"指定权获得者指定：{designated_player.user_name}\n"
        if additional_targets:
            extra_names = "、".join(p.user_name for p in additional_targets)
            result += f"系统补足：{extra_names}\n"
        result += f"\n被选中的玩家（{len(all_targets)}人）：{target_names}\n\n"

        # 列出每个玩家的事件
        for p in all_targets:
            et, et_text = session.target_events[p.user_id]
            type_name = "真心话" if et == "truth" else "大冒险"
            result += f"@{p.user_name}：{type_name} - {et_text}\n"

        result += f"\n请上述玩家完成事件后，发送 /td done 确认完成！\n"
        result += f"发送 /td skip 可以跳过本轮（需要被选中的玩家本人确认）"

        algorithm_names = [
            "点数最大", "点数最小", "单数点数最大",
            "单数点数最小", "双数点数最大", "双数点数最小"
        ]
        algo_name = algorithm_names[session.selection_algorithm] if session.selection_algorithm is not None else "未知"
        logger.info(
            f"[真心话大冒险] 群 {group_id} 第 {session.current_round + 1} 轮："
            f"算法={algo_name}，指定={designated_player.user_name}，"
            f"补足={[p.user_name for p in additional_targets]}，"
            f"事件={[f'{p.user_name}={session.target_events[p.user_id][0]}' for p in all_targets]}"
        )

        yield event.chain_result(at_chain + [Plain(result)])



    async def _select_and_notify_designator(self, session: GameSession, event: AstrMessageEvent, all_players: list):
        """选出指定权获得者并发送提示消息（含 @ 指定权获得者）"""
        group_id = session.group_id
        algorithm_names = [
            "点数最大", "点数最小", "单数点数最大",
            "单数点数最小", "双数点数最大", "双数点数最小"
        ]

        # 如果已有指定权获得者，直接复用，不重新选择
        if session.designator_id:
            designator = session.players.get(session.designator_id)
            if designator is None:
                # 指定权获得者已不在游戏中，清空并重新选择
                session.designator_id = None
                session.selection_algorithm = None
                designator = None
            else:
                algorithm = session.selection_algorithm if session.selection_algorithm is not None else random.randint(0, 5)
        else:
            # 随机选择算法 (0-5)
            algorithm = random.randint(0, 5)
            # 用选中的算法选出指定权获得者
            designator = self._select_by_algorithm(all_players, algorithm)

        if designator is None:
            yield event.plain_result("无法选出指定权获得者，请确保所有玩家都已 Roll 点！")
            return

        # 保存算法和指定权获得者
        session.selection_algorithm = algorithm
        session.designator_id = designator.user_id

        logger.info(
            f"[真心话大冒险] 群 {group_id} 第 {session.current_round + 1} 轮："
            f"算法={algorithm_names[algorithm]}，指定权获得者={designator.user_name}"
        )

        # 发送提示消息：含 @ 指定权获得者
        target_count = self._calc_target_count(len(all_players))
        actual_count = min(target_count, len(all_players))
        
        roll_result_text = "\n".join(
            f"  {p.user_name}：{p.last_roll}" 
            for p in sorted(all_players, key=lambda x: x.last_roll if x.last_roll is not None else float("inf"))
        )
        
        # 使用 chain_result 发送 @ 提及
        at_chain = [At(qq=self._to_at_id(designator.user_id))]
        msg = (
            f"第 {session.current_round + 1} 轮\n\n"
            f"Roll 点结果：\n{roll_result_text}\n\n"
            f"本轮目标人数：{actual_count} 人\n"
            f"已随机选出指定权获得者，请 @{designator.user_name} 使用 /td 指定 @玩家 指定 1 名玩家进行事件！"
        )
        
        yield event.chain_result(at_chain + [Plain(msg)])
        return

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
                    # 新指定机制字段
                    "designator_id": session.designator_id,
                    "designated_target_id": session.designated_target_id,
                    "designated_event_type": session.designated_event_type,
                    "selection_algorithm": session.selection_algorithm,
                    "current_target_ids": session.current_target_ids,
                    "round_in_progress": session.round_in_progress,
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
                # 恢复新指定机制字段
                session.designator_id = sdata.get("designator_id")
                session.designated_target_id = sdata.get("designated_target_id")
                session.designated_event_type = sdata.get("designated_event_type")
                session.selection_algorithm = sdata.get("selection_algorithm")
                session.current_target_ids = sdata.get("current_target_ids", [])
                session.round_in_progress = sdata.get("round_in_progress", False)
                # 重启后不恢复进行中的轮次状态，需要重新 Roll
                session.current_target_count = 0
                session.target_events = {}
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

    @filter.command("join", alias={"加入"})
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

    @filter.command("leave", alias={"退出"})
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

    @filter.command("list", alias={"列表"})
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

    @filter.command("start", alias={"开始"})
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
            f"请所有玩家发送 /td roll 来 Roll 点！\n"
            f"本轮目标人数：{target_count} 人\n"
            f"所有人都 Roll 完了后，将随机选出指定权获得者，由其指定 1 名玩家，\n"
            f"系统按相同算法补足剩余名额，每人独立获得真心话/大冒险事件！"
        )

    @filter.command("roll")
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
            yield event.plain_result("游戏还没开始，请先发送 /td start 开始游戏！")
            return

        if session.round_in_progress:
            yield event.plain_result(
                "当前轮次正在进行中，请完成或跳过本轮（/td done 或 /td skip）后再开始下一轮！"
            )
            return

        if user_id not in session.players:
            yield event.plain_result("你不在游戏中，请先发送 /td join 加入！")
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

        # 检查是否所有人都 Roll 完了，如果是则自动触发下一阶段
        all_players = list(session.players.values())
        if all(p.last_roll is not None for p in all_players):
            # 先发送提示（不 @ 任何人）
            yield event.plain_result("所有人已完成 Roll 点，正在随机选出指定权获得者...")
            # 然后选出指定权获得者并发送完整提示（含 @）
            async for result in self._select_and_notify_designator(session, event, all_players):
                yield result

    @filter.command("result", alias={"结果"})
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
            + f"\n\n发送 /td go 让机器人处理事件！"
        )

    @filter.command("go", alias={"下一轮"})
    async def cmd_go(self, event: AstrMessageEvent):
        """
        处理真心话大冒险事件 - 新指定机制。

        流程：
        1. 检查所有玩家都已 Roll 点
        2. 从 6 种算法随机选一种，选出 "指定权获得者"
        3. 等待指定权获得者使用 /td 指定 指定 1 名玩家
        4. 指定完成后，用同一算法补足剩余名额
        5. 为每个目标玩家独立随机真心话/大冒险
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始，请先发送 /td start 开始游戏！")
            return

        # 本轮已在进行：必须先由被选中玩家结算（/td done 或 /td skip），才能开新一轮
        if session.round_in_progress:
            in_progress_names = "、".join(
                session.players[uid].user_name
                for uid in session.current_target_ids
                if uid in session.players
            )
            yield event.plain_result(
                f"本轮正在进行中！请 {in_progress_names} 完成事件后发送 /td done 确认，"
                f"或由被选中的玩家发送 /td skip 跳过！"
            )
            return

        # 检查冷却
        remaining = self._check_cooldown(session)
        if remaining is not None:
            yield event.plain_result(f"冷却中，请等待 {remaining:.0f} 秒后再开始下一轮！")
            return

        # 检查 Roll 状态：所有玩家都必须 Roll
        all_players = list(session.players.values())
        unrolled = [p for p in all_players if p.last_roll is None]
        if unrolled:
            names = "、".join(p.user_name for p in unrolled)
            yield event.plain_result(
                f"以下玩家还没 Roll 点：\n{names}\n\n"
                f"请发送 /td roll 完成 Roll 点后再继续！"
            )
            return

        # 动态计算目标人数：4人→2人，每+2人→+1人
        target_count = self._calc_target_count(len(all_players))
        actual_count = min(target_count, len(all_players))

        # 统一使用 helper 发送提示（含 @ 指定权获得者）
        async for result in self._select_and_notify_designator(session, event, all_players):
            yield result

    @filter.command("指定")
    async def cmd_designate(self, event: AstrMessageEvent):
        """
        指定权获得者指定 1 名玩家进行事件。
        只有被随机选中的指定权获得者可以使用此指令。
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用！")
            return

        session = self._get_session(group_id)

        if not session.is_started:
            yield event.plain_result("游戏还没开始，请先发送 /td start 开始游戏！")
            return

        # 检查是否有指定权获得者且还未指定目标
        if not session.designator_id or session.designated_target_id:
            yield event.plain_result("当前不在指定阶段，无法使用此指令！")
            return

        # 权限检查：只有指定权获得者可以使用
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        if user_id != session.designator_id:
            yield event.plain_result(f"{user_name}，你不是本轮的指定权获得者，无法使用指定指令！")
            return

        # 解析指定目标：仅支持 @ 提及 1 个玩家
        target_id = None
        target_name = None
        event_type_arg = None  # 可选：真心话/大冒险

        # 从 At 组件提取
        at_targets = [comp for comp in event.message_obj.message if isinstance(comp, At)]
        if at_targets:
            uid = str(at_targets[0].qq)
            if uid in session.players:
                target_id = uid
                target_name = session.players[uid].user_name
            else:
                yield event.plain_result("该玩家不在游戏中！")
                return
        else:
            # 尝试从纯文本中提取玩家名
            message = event.get_message_str()
            parts = message.strip().split()
            if len(parts) >= 2:
                name = parts[1].strip()
                for uid, p in session.players.items():
                    if p.user_name == name:
                        target_id = uid
                        target_name = p.user_name
                        break
            if len(parts) >= 3:
                event_type_arg = parts[2].strip()

        # 解析可选事件类型
        if event_type_arg:
            if event_type_arg in ("真心话", "truth", "真话"):
                event_type_arg = "truth"
            elif event_type_arg in ("大冒险", "dare", "冒险"):
                event_type_arg = "dare"
            else:
                yield event.plain_result(
                    "无效的事件类型，请使用：\n"
                    "/td 指定 @玩家 真心话\n"
                    "/td 指定 @玩家 大冒险\n"
                    "或不指定类型（随机）"
                )
                return

        if not target_id:
            yield event.plain_result(
                "请指定要指定的玩家（仅限 1 人）：\n"
                "/td 指定 @玩家\n"
                "/td 指定 玩家名\n"
                "/td 指定 @玩家 真心话\n"
                "/td 指定 @玩家 大冒险"
            )
            return

        # 不能指定自己
        if target_id == user_id:
            yield event.plain_result("不能指定自己！")
            return

        # 保存被指定的目标
        session.designated_target_id = target_id

        # 如果指定了事件类型，保存到 session
        session.designated_event_type = event_type_arg

        type_note = ""
        if event_type_arg == "truth":
            type_note = "（真心话）"
        elif event_type_arg == "dare":
            type_note = "（大冒险）"

        yield event.plain_result(
            f"{user_name} 指定了 {target_name} 进行本轮事件{type_note}！\n"
            f"系统正在按相同算法补足剩余名额..."
        )

        # 自动处理：补足剩余名额并分配事件
        async for result in self._process_designation(session, event):
            yield result

    @filter.command("指定清除")
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

        if not session.designated_target_id:
            yield event.plain_result("本轮没有手动指定的目标。")
            return

        # 清除本轮手动指定状态，保留指定权获得者，使其可重新指定
        session.designated_target_id = None
        session.designated_event_type = None
        yield event.plain_result("已清除本轮手动指定的目标，可重新使用 /td 指定 @玩家 指定其他玩家。")

    @filter.command("done", alias={"完成"})
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
            yield event.plain_result("当前没有进行中的事件，请先发送 /td go 开始新一轮！")
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
            # 显示该玩家的事件类型
            et, et_text = session.target_events.get(user_id, (None, ""))
            type_label = "真心话" if et == "truth" else "大冒险"
            yield event.plain_result(
                f"{user_name} 完成了{type_label}事件！\n\n"
                f"本轮共有 {len(session.current_target_ids)} 名被选中的玩家，"
                f"还有 {len(remaining_ids)} 人未完成：\n{remaining_names}\n\n"
                f"请他们完成后发送 /td done 确认。"
            )
            return

        # 全部被选中玩家均确认完成 → 统一结算本轮
        self._finish_round(session)

        yield event.plain_result(
            f"{user_name} 完成了事件！\n\n"
            f"所有被选中的玩家均已完成，本轮结束！\n"
            f"请发送 /td go 开始下一轮！"
        )

    @filter.command("skip", alias={"跳过"})
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

        # 显示该玩家的事件
        et, et_text = session.target_events.get(user_id, (None, ""))
        type_label = "真心话" if et == "truth" else "大冒险"

        # 跳过视为结算本轮：统一走 _finish_round
        self._finish_round(session)

        yield event.plain_result(
            f"{user_name} 跳过了本轮事件！\n"
            f"跳过的{type_label}：{et_text}\n\n"
            f"本轮结束！请发送 /td go 开始下一轮！"
        )

    @filter.command("kick", alias={"踢人"})
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
        # 格式: /td kick @某人 或 /td kick 玩家名
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
                yield event.plain_result("请指定要踢出的玩家：/td kick @玩家 或 /td kick 玩家名")
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
        was_designator = target_id == session.designator_id
        was_designated = target_id == session.designated_target_id
        session.remove_player(target_id)
        if was_target:
            # 踢掉的是当前轮被选中的玩家 → 统一走 _finish_round 结算（递增轮次+重置+冷却）
            self._finish_round(session)
        elif was_designator or was_designated:
            # 踢掉的是指定权获得者或被指定者，需要重置指定状态
            session.designator_id = None
            session.designated_target_id = None
            session.selection_algorithm = None
            session.target_events = {}
            session.round_in_progress = False
            session.current_target_ids = []

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
                f"当前轮次已结算，请发送 /td go 开始新一轮。\n"
                f"当前玩家数：{session.get_player_count()}"
            )
        else:
            yield event.plain_result(
                f"{target_name} 被踢出游戏！\n"
                f"当前玩家数：{session.get_player_count()}"
            )

    @filter.command("stop", alias={"结束"})
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

    @filter.command("help", alias={"帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助信息"""
        yield event.plain_result(
            "真心话大冒险 - 游戏帮助\n\n"
            "游戏流程：\n"
            "1. /td join  - 加入游戏\n"
            "2. /td start - 开始游戏（至少4人）\n"
            "3. /td roll  - 所有玩家 Roll 点（所有人完成后自动进入下一阶段）\n"
            "4. /td go    - 查看当前轮状态 / 触发指定流程\n"
            "5. /td 指定  - 指定权获得者指定 1 名玩家进行事件\n"
            "6. 系统按相同算法补足剩余名额，每人独立获得真心话/大冒险\n"
            "7. /td done  - 完成事件，进入下一轮\n"
            "8. /td skip  - 跳过当前事件\n\n"
            "其他指令：\n"
            "/td list       - 查看玩家列表\n"
            "/td result     - 查看 Roll 点结果\n"
            "/td leave      - 退出游戏\n"
            "/td kick       - 踢出 AFK 玩家（管理员）\n"
            "/td 指定清除   - 清除本轮指定状态（管理员）\n"
            "/td stop       - 结束游戏\n"
            "/td help       - 显示此帮助\n\n"
            "指定机制：\n"
            "- 所有人都 Roll 完了后，从 6 种算法随机选一种选出指定权获得者\n"
            "- 算法：点数最大/最小、单数点数最大/最小、双数点数最大/最小\n"
            "- 指定权获得者用 /td 指定 @玩家 指定 1 人\n"
            "- 可选指定事件类型：/td 指定 @玩家 真心话 / /td 指定 @玩家 大冒险\n"
            "- 系统用相同算法补足剩余名额\n"
            "- 每人独立随机真心话或大冒险（未指定时）\n\n"
            "动态人数规则：\n"
            "4人→抽2人 | 6人→抽3人 | 8人→抽4人\n"
            "每增加2人，事件目标人数+1\n\n"
            "所有指令均以 td 开头，避免与其他插件冲突"
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