# 真心话大冒险插件 - 逻辑漏洞审查报告

审查对象：`main.py`（v1.0.0，commit `d3f647c`）
审查方式：静态代码走查（未运行，方法名基于 AstrBot 4.x 公开 API 推断）

---

## 🔴 P1（崩溃级）：`/td_go` 在「管理员已指定目标 + 仍有玩家未 Roll」时崩溃

**位置：** `cmd_go`，约 `main.py:460-528`

**触发场景：**
1. 管理员执行 `/td_指定 @玩家X`，写入 `session.designated_ids`（此时 `designated_ids` 非空）。
2. `cmd_go` 第 449-450 行：`if not session.designated_ids:` 为假，**跳过了「所有人必须 Roll」的检查**。
3. 某玩家 `/td_go`。此时存在 `last_roll is None` 的未 Roll 玩家，故第 462 行 `all(...)` 为 False → `min_roll = None` → 第 466 行 `lowest_players = []`。
4. `targets` 由指定的目标提供（非空），流程继续执行。
5. **第 523 行**：
   ```python
   sorted(all_players, key=lambda x: x.last_roll)
   ```
   `all_players` 中夹杂 `last_roll = None` 与 `int`，Python 3 对 None 与 int 排序抛出
   `TypeError: '<' not supported between instances of 'NoneType' and 'int'`，命令直接崩溃。

**影响：** 一旦管理员指定过目标且有人未 Roll，`/td_go` 必崩。

**修复建议：** 在 `cmd_go` 排序前过滤未 Roll 玩家，或对 `last_roll` 使用安全排序键：
```python
sorted(all_players, key=lambda p: p.last_roll if p.last_roll is not None else sys.maxsize)
```

---

## 🟠 P1（滥用级）：任一目标玩家 `/td_done` 即视为整轮完成

**位置：** `cmd_done`，`main.py:679-691`

**触发场景：** 本轮抽中 2 人（如 A、B）。A 完成事件后 `/td_done`，`reset_round()` 直接清空整轮（`current_target_ids` 也被清空）。B 即使完全没完成事件，也会随整轮一起被结算、进入下一轮。

**影响：**
- 被同时点名的玩家中，只要一人 `td_done` 就覆盖全员，其余目标玩家可借此逃避事件。
- 与 README「请 X、Y 完成事件后发送 /td_done 确认」描述的交互预期不一致。

**修复建议：** 应为每个目标玩家单独标记完成状态，全部确认后才结算进入下一轮（参见下方 P2 的第 1 节）。

---

## 🟠 P1（滥用级）：任意成员可无限刷 `/td_go`，反复重选目标并刷高轮次

**位置：** `cmd_go`，`main.py:508`（`session.current_round += 1`）与目标随机选择逻辑。

**触发场景：** 每执行一次 `/td_go`，都会再次随机抽选新的目标玩家、`round_in_progress` 被覆盖、`current_round += 1`。任何人（不限于被选中者）都可以反复 `/td_go`。

**影响：**
- 被点名者可无限次被换（配合 P2 空刷），他人可借机反复刷轮数或骚扰不同玩家。
- `current_target_ids`、事件内容等可被刷新覆盖，绕过游戏的轮流节奏。

**修复建议：**
- `/td_go` 在 `round_in_progress == True` 时拒绝执行（本轮已进行中，先 `/td_done`/`/td_skip`）。
- `current_round` 的递增放到「一轮结算完成」时，而不是每次 `/td_go` 时。

---

## 🟡 P2（一致性问题）：管理员指定目标后，其余玩家不必 Roll 也能进行

**位置：** `cmd_go`，`main.py:449-458`

**触发场景：** 只要有 `designated_ids`，就会跳过「全员 Roll」的校验。管理员指定 2 人后，其余几十人完全不 `/td_roll` 也能 `/td_go`，且随后的 Roll 结果列表里会显示一批「未Roll」。

**影响：** 破坏「所有人 Roll 决定命运」的机制；也是上述 P1 崩溃的根源之一。

**修复建议：** 指定目标时，也应要求所有**未被指定的玩家**完成 Roll，未 Roll 则阻止 `/td_go`。

---

## 🟡 P2（跨轮残留）：轮次是被「踢人/指定/跳过」交错重置的，易产生不一致

**位置：** `cmd_kick`（`main.py:789-808`）、`reset_round`。

- 踢出的是当前目标玩家时，会 `reset_round()` 但**不清空 `designated_ids`**（`reset_round` 第 90 行清空的是 `designated_ids`，但 `cmd_kick` 里 `was_target` 分支未单独处理）。实际 `reset_round` 会清 `designated_ids`，故未见残留。
- 真正的问题是：`cmd_kick` 在 `reset_round` 之后，若人数不足走「自动结束游戏」分支，会再次 `reset_round()` + 清空玩家——此时 `designated_ids` 已被清，行为正确。
- 结论：`designated_ids` 生命周期随「本轮」正确清理，此点相对安全；但 `cmd_done`/`cmd_skip`/`cmd_kick` 三处重置与 `cmd_go` 的衔接缺少统一状态机，仍存在上述 P1/P2 交错风险。

---

## 🔵 P3（兼容性）：管理员角色判断依赖返回值精确等于英文枚举

**位置：** `cmd_designate`（`main.py:565-566`）、`cmd_designate_clear`、`cmd_kick`。

```python
is_admin = sender_role in ("owner", "admin") if sender_role else False
```

AstrBot/OneBot 不同接入端对角色字段的返回可能不同（如中文「群主」「管理员」、或 `"OWNER"`），若返回非中文/大小写不符，管理员判定会失效（权限被误拒，属「安全降级」，不会越权）。

**修复建议：** 归一化后再比较，如 `sender_role = str(sender_role).lower()`，并考虑兼容 `owner/admin/member` 之外的取值。

---

## 🔵 P3：持久化写入无锁、竞态窗口

**位置：** `_save_sessions`（`main.py:187-210`）、`_periodic_save`（每 30s 全量写）+ `terminate` 时的写。

- 多个并发事件对 `self.games` 的内存修改与 30s 全量 dump 可能交错，极端情况写入半更新数据；中断时只写内存最新快照，未做原子写（临时文件+rename），断电可能损坏 JSON。

---

## ✅ 未发现问题（简单核对）

- `roll_min > roll_max` 未校验：会抛 ValueError，但属配置误用，非核心逻辑缺陷。
- `truth_weight` 越界（如 150）：`random.randint(1,100) <= 150` 恒真，只会导致永远出真心话，不会崩溃。
- `get_player_list_text`、`cmd_roll_result` 等对空/未 Roll 状态的字符串化均有判空，安全。
- `initialize`/`terminate` 对后台保存任务的创建与取消配对正确。

---

## 优先修复建议（按重要性）

1. **P1 崩溃**：`cmd_go` 排序前对 `last_roll` 做非 None 兜底（最高优先，当前版本必崩的一处）。
2. **P1 滥用**：限制 `cmd_go` 在 `round_in_progress` 时不可重复触发；`current_round` 移到结算时递增。
3. **P1 一致**：`cmd_done` 改为逐目标玩家确认，全部完成后才进入下一轮。
4. **P2 机制**：指定目标后仍要求未指定玩家 Roll。

---

# ✅ 修复记录

以下问题已在本轮修改（`main.py`）中修复，并新增了对应回归自检（已运行通过，Python 3.14 `py_compile` 与离线逻辑脚本）。

## 1. 🔴 P1 崩溃 — `/td_go` 排序崩溃（已修复）

- **改动：** `cmd_go` 的 Roll 结果排序改用了 None 安全键：
  ```python
  sorted(all_players, key=lambda x: x.last_roll if x.last_roll is not None else float("inf"))
  ```
  未 Roll 玩家排在最后并显示为「未Roll」，不再抛 `TypeError`。

## 2. 🟠 P1 滥用 — 反复 `/td_go` 刷目标/刷轮次（已修复）

- **改动：** `cmd_go` 启动即检查 `session.round_in_progress`，若本轮进行中直接拒绝并提示先 `/td_done` 或 `/td_skip`。
- **改动：** `current_round` 的递增从 `cmd_go` 移除，统一交由新增的 `_finish_round()`（`cmd_done` 全员完成、`cmd_skip`、`cmd_kick` 踢中被选中者）在**结算时**执行；`cmd_go` 展示的轮次改为 `current_round + 1`，日志同步修正。

## 3. 🟠 P1 一致 — 任一目标 `/td_done` 即整轮过（已修复）

- **改动：** `GameSession` 新增 `completed_target_ids`，`reset_round` 一并清理（含 `_load_sessions` 恢复时重置）。
- **改动：** `cmd_done` 改为**逐目标玩家确认**：仅被选中者可确认、每人一次；尚有其他目标未完成时只记录并提示等待，**全部确认完成后才**走 `_finish_round` 进入下一轮。

## 4. 🟡 P2 机制 — 指定目标后其余玩家免 Roll（已修复）

- **改动：** `cmd_go` 的 Roll 检查从「仅当无指定时才检查」改为「所有**未被指定**的玩家都必须 Roll」，被指定者豁免。

## 5. 🔵 P3 兼容 — 管理员角色判断（已修复）

- **改动：** 新增统一辅助方法 `_is_admin(event)`：捕获异常、`str().strip().lower()` 归一化，兼容 `owner/admin/administrator/creator` 及中文「群主/管理员/管理」；`cmd_designate`、`cmd_designate_clear`、`cmd_kick` 三处全部改用该判断。

## 6. 🔵 P3 持久化竞态（未修复 — 属建议项）

- 仍未做原子写/加锁；优先级偏低，未在本轮改动范围内，保留待后续处理。

---

## 回归自检结果（Python 3.14）

| 检查项 | 结果 |
|--------|------|
| `py_compile main.py` | 通过 |
| 排序含 None 不崩、None 排末 | 通过 |
| `reset_round` 清理 `completed_target_ids` | 通过 |
| 2 目标玩家逐个 `td_done` 时 `round_in_progress` 保持、`current_round` 不变直到最后 | 通过 |
| 全部目标完成后 `current_round` 正确 +1 | 通过 |
| 重复 `td_done` 被识别为「已确认」 | 通过 |
| `_finish_round` 统一结算（round 重置 + cooldown 记录） | 通过 |
| 指定目标时未指定玩家仍须 Roll | 通过 |