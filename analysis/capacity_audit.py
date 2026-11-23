"""工业遗址开放容量试算与可售名额审计（合成样例）。

口径依据 docs/业务规程.md：
- 同一团队在某节点的占用从入场持续到确认离场；缺离场事件时占用上界不确定，
  不得把缺记录解释为成员已离开。
- 相邻节点之间保留 support-2 中的疏散间隔，节点容量在任一连续窗口均不得超限。
- 已确认且已付款的团体预约保持原窗口或进入改约队列；探索估计不能作为可售数量。
- 核准数量 = 同时符合容量约束与已承诺预约后的剩余量。

本脚本只使用标准库；所有输入路径经命令行传入，不依赖当前工作目录。
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 基础工具


def to_min(hhmm: str) -> int:
    """'09:10' -> 当天第 550 分钟。"""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def fmt(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    """两个闭开区间 [a0,a1)、[b0,b1) 的重叠分钟数。"""
    return max(0, min(a1, b1) - max(a0, b0))


# ---------------------------------------------------------------- 数据模型


@dataclass
class Occupancy:
    team: str
    day: int
    node: str
    enter: int
    leave: int | None          # None = 缺离场事件，占用上界未知
    occupancy: int
    weather: str
    group: str
    closure: str
    raw_enter: str
    raw_leave: str

    @property
    def known(self) -> bool:
        return self.leave is not None


@dataclass
class Booking:
    booking: str
    team: str
    day: int
    entry: int
    people: int
    status: str
    paid: bool


@dataclass
class NodeSpec:
    node: str
    max_occupancy: int
    max_dwell: int
    evacuation: int


@dataclass
class Finding:
    """一条可追溯的瓶颈/证据记录。"""
    code: str
    day: int | None
    node: str
    window: str
    detail: str
    source: str
    effect: str


@dataclass
class Audit:
    occupancies: list[Occupancy] = field(default_factory=list)
    bookings: list[Booking] = field(default_factory=list)
    specs: dict[str, NodeSpec] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


# ---------------------------------------------------------------- 读取输入


def load(fixtures: Path) -> Audit:
    audit = Audit()
    with (fixtures / "primary.csv").open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            audit.occupancies.append(
                Occupancy(
                    team=r["team"],
                    day=int(r["day"]),
                    node=r["node"],
                    enter=to_min(r["enter"]),
                    leave=to_min(r["leave"]) if r["leave"] else None,
                    occupancy=int(r["occupancy"]),
                    weather=r["weather"],
                    group=r["group"],
                    closure=r["closure"],
                    raw_enter=r["enter"],
                    raw_leave=r["leave"] or "(缺报)",
                )
            )
    with (fixtures / "support-1.csv").open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            audit.bookings.append(
                Booking(
                    booking=r["booking"],
                    team=r["team"],
                    day=int(r["day"]),
                    entry=to_min(r["entry"]),
                    people=int(r["people"]),
                    status=r["status"],
                    paid=r["paid"].strip().lower() == "yes",
                )
            )
    with (fixtures / "support-2.csv").open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            audit.specs[r["node"]] = NodeSpec(
                node=r["node"],
                max_occupancy=int(r["max_occupancy"]),
                max_dwell=int(r["max_dwell"]),
                evacuation=int(r["evacuation_minutes"]),
            )
    return audit


# ---------------------------------------------------------------- 规则核查


def check_capacity(audit: Audit) -> None:
    """任一连续窗口节点容量不得超限；团队占用以确认离场为界。"""
    for day in sorted({o.day for o in audit.occupancies}):
        for node, spec in audit.specs.items():
            rows = [o for o in audit.occupancies if o.day == day and o.node == node]
            for i, a in enumerate(rows):
                for b in rows[i + 1:]:
                    a1, b1 = a.leave, b.leave
                    if a1 is None or b1 is None:
                        audit.findings.append(Finding(
                            "UNCERTAIN-OVERLAP", day, node,
                            f"{fmt(max(a.enter, b.enter))} 起",
                            f"团队 {a.team}({a.occupancy}人, 离场{a.raw_leave}) 与 "
                            f"{b.team}({b.occupancy}人, 离场{b.raw_leave}) 的重叠上界无法判定，"
                            "不得按已离开处理",
                            "primary.csv", "该时段暂停承诺，见可售表",
                        ))
                        continue
                    ov = overlap(a.enter, a1, b.enter, b1)
                    if ov and a.occupancy + b.occupancy > spec.max_occupancy:
                        audit.findings.append(Finding(
                            "CAPACITY-BREACH", day, node,
                            f"{fmt(max(a.enter, b.enter))}-{fmt(min(a1, b1))}",
                            f"团队 {a.team}+{b.team} 合计 {a.occupancy + b.occupancy} 人 "
                            f"> 容量 {spec.max_occupancy} 人，持续 {ov} 分钟",
                            "primary.csv + support-2.csv",
                            "已付款预约保留权利，不可履行部分进改约队列；同窗口不核准新名额",
                        ))


def check_dwell(audit: Audit) -> None:
    """分段停留不得超过 max_dwell；缺离场时停留时长本身也无法判定。"""
    for o in audit.occupancies:
        spec = audit.specs.get(o.node)
        if spec is None:
            continue
        if o.leave is None:
            audit.findings.append(Finding(
                "MISSING-LEAVE", o.day, o.node,
                f"{o.raw_enter} 起",
                f"团队 {o.team} 缺离场事件：占用上界与是否超过 {spec.max_dwell} 分钟停留上限均无法判定",
                "primary.csv",
                f"按规程占用持续至确认离场；保守口径下该节点在 {o.raw_enter} 之后的承诺窗口暂停",
            ))
            continue
        dwell = o.leave - o.enter
        if dwell > spec.max_dwell:
            audit.findings.append(Finding(
                "DWELL-BREACH", o.day, o.node,
                f"{o.raw_enter}-{o.raw_leave}",
                f"团队 {o.team} 停留 {dwell} 分钟 > 上限 {spec.max_dwell} 分钟",
                "primary.csv + support-2.csv", "该时段政策不可兑现，需改约或限流",
            ))


def check_evacuation(audit: Audit) -> None:
    """相邻节点接续：下一节点入场 - 上一节点确认离场 >= 疏散间隔。"""
    by_team: dict[tuple[str, int], list[Occupancy]] = {}
    for o in audit.occupancies:
        by_team.setdefault((o.team, o.day), []).append(o)
    for (team, day), rows in by_team.items():
        rows = sorted(rows, key=lambda x: x.enter)
        for prev, nxt in zip(rows, rows[1:]):
            spec = audit.specs.get(prev.node)
            if spec is None:
                continue
            if prev.leave is None:
                audit.findings.append(Finding(
                    "UNCERTAIN-EVAC", day, f"{prev.node}->{nxt.node}",
                    f"{prev.raw_enter} 起",
                    f"团队 {team} 在 {prev.node} 缺离场记录，无法核验到 {nxt.node} 的 "
                    f"{spec.evacuation} 分钟疏散间隔（已知 {nxt.node} 入场 {nxt.raw_enter}）",
                    "primary.csv + support-2.csv",
                    "只界定压力、不判定合规；相关窗口暂停承诺",
                ))
                continue
            gap = nxt.enter - prev.leave
            if gap < spec.evacuation:
                audit.findings.append(Finding(
                    "EVAC-BREACH", day, f"{prev.node}->{nxt.node}",
                    f"{prev.raw_leave}-{nxt.raw_enter}",
                    f"团队 {team} 节点间隔 {gap} 分钟 < 疏散要求 {spec.evacuation} 分钟",
                    "primary.csv + support-2.csv", "该接续方案不可核准",
                ))


def check_closures(audit: Audit) -> None:
    """节点临时关闭：途中观众仍占用可达安全空间，关闭后不核准新入场。"""
    for o in audit.occupancies:
        if o.closure.strip().lower() == "yes":
            end = fmt(o.leave) if o.leave is not None else "离场未知"
            audit.findings.append(Finding(
                "NODE-CLOSURE", o.day, o.node,
                f"{o.raw_enter}-{end}",
                f"团队 {o.team} 在场期间 {o.node} 标记临时关闭；其占用持续到确认离场，"
                "不能靠挪动起始时段把占用转到别处",
                "primary.csv",
                f"{o.node} 当日关闭后不核准新入场；未售出名额待重开后按新条件投放",
            ))


def check_bookings(audit: Audit) -> None:
    for b in audit.bookings:
        match = [o for o in audit.occupancies if o.team == b.team and o.day == b.day]
        headcount_ok = any(o.occupancy == b.people for o in match)
        note = "人数与轨迹一致" if headcount_ok else "人数与轨迹不一致，需人工核对"
        audit.findings.append(Finding(
            "BOOKING", b.day, "(预约)",
            fmt(b.entry),
            f"{b.booking} 团队 {b.team} {b.people} 人 status={b.status} paid={'是' if b.paid else '否'}；{note}",
            "support-1.csv" + (" + primary.csv" if match else ""),
            "已付款：保留原窗口或改约；未付款：保留权利但不具备同等级保护，不可履行时列入改约",
        ))


# ---------------------------------------------------------------- 情景与名额


# 探索情景：仅改变停留政策（团队人数、分段停留的联合结构不允许独立重排）。
# pitch = 假定停留上限 + 同节点疏散间隔，即相邻入场的最小节拍。
SCENARIOS = [
    ("S0-现状", "实测停留（含已发生的超载）", None, None),
    ("S1-规程上限", "stair 15 分钟 / refuge 20 分钟（support-2 现行上限）", 15, 20),
    ("S2-收紧10", "stair 10 分钟 / refuge 15 分钟", 10, 15),
    ("S3-极限疏导", "stair 8 分钟 / refuge 12 分钟（探索，需人力与安全复核）", 8, 12),
]

HORIZON_MIN = 180  # 探索口径：每个开放日上午 3 小时窗口（无规程依据，仅用于同条件比较）


def scenario_table(audit: Audit) -> list[dict]:
    rows = []
    for name, desc, stair_d, refuge_d in SCENARIOS:
        row = {"scenario": name, "desc": desc}
        for node, dwell in (("stair", stair_d), ("refuge", refuge_d)):
            spec = audit.specs[node]
            if dwell is None:
                row[f"{node}_pitch"] = "—（实测无统一节拍）"
                row[f"{node}_entries_per_3h"] = "不可计算"
                row[f"{node}_rate_per_hour"] = "—"
            else:
                pitch = dwell + spec.evacuation
                row[f"{node}_pitch"] = f"{pitch} 分钟"
                row[f"{node}_entries_per_3h"] = HORIZON_MIN // pitch
                row[f"{node}_rate_per_hour"] = round(60 / pitch, 2)
        rows.append(row)
    return rows


def free_slots(horizon: tuple[int, int], blocked: list[tuple[int, int]],
               pitch: int) -> list[int]:
    """在 horizon 内枚举完整节拍窗口，窗口与任一 blocked 区间相交即不可售。"""
    h0, h1 = horizon
    slots, t = [], h0
    while t + pitch <= h1:
        if all(overlap(t, t + pitch, b0, b1) == 0 for b0, b1 in blocked):
            slots.append(t)
        t += pitch
    return slots


def committable(audit: Audit) -> list[dict]:
    """核准可售名额：只在有完整观测、无未决占用的节点-日窗口内计算。"""
    out = []
    for day in sorted({o.day for o in audit.occupancies}):
        for node, spec in audit.specs.items():
            rows = [o for o in audit.occupancies if o.day == day and o.node == node]
            closed = any(o.closure.strip().lower() == "yes" for o in rows)
            uncertain = any(o.leave is None for o in rows)
            pitch = spec.max_dwell + spec.evacuation
            if not rows:
                out.append(dict(day=day, node=node, status="无观测",
                                window="—", approved_slots=0,
                                reason="该节点当日无任何轨迹，资料不足，全部窗口暂停承诺"))
                continue
            if uncertain:
                o = next(o for o in rows if o.leave is None)
                out.append(dict(day=day, node=node, status="暂停承诺",
                                window=f"{o.raw_enter} 之后", approved_slots=0,
                                reason=f"团队 {o.team} 缺离场记录，占用上界未知；"
                                       "规程要求按仍在场处理，不核准新名额"))
                continue
            if closed:
                o = next(o for o in rows if o.closure.strip().lower() == "yes")
                out.append(dict(day=day, node=node, status="关闭",
                                window=f"{o.raw_enter} 起", approved_slots=0,
                                reason="节点临时关闭，途中观众占用至确认离场；关闭后不投放新名额"))
                continue
            # 完整观测窗口：最早入场到最晚确认离场
            h0, h1 = min(o.enter for o in rows), max(o.leave for o in rows)  # type: ignore[type-var]
            blocked = [(o.enter, o.leave) for o in rows]  # type: ignore[arg-type]
            # 容量超限窗口：已付款预约保留，同节拍不核准
            breach = any(
                a.occupancy + b.occupancy > spec.max_occupancy
                and overlap(a.enter, a.leave, b.enter, b.leave) > 0  # type: ignore[arg-type]
                for i, a in enumerate(rows) for b in rows[i + 1:]
            )
            slots = free_slots((h0, h1), blocked, pitch)
            kept_team = ""
            if breach:
                slots = []
                reason = ("观测窗口内发生容量超限（见瓶颈台账），已付款预约保留/改约，"
                          "窗口内不核准新名额；改约兑现后释放的节拍按 free_after_rebooking 列示")
                kept_team = min(rows, key=lambda o: o.enter).team
            else:
                reason = f"按 {pitch} 分钟节拍（停留 {spec.max_dwell}+疏散 {spec.evacuation}）枚举"
            # 改约兑现后的释放量（探索性参考）：只搬走解决超载所必需的最少团单，
            # 保留最早的已付款预约按原约兑现，避免挤占仍可履行的团队。
            rebooked_blocked = []
            if breach:
                kept = min(rows, key=lambda o: o.enter)
                rebooked_blocked = [(o.enter, o.leave) for o in rows if o is not kept]  # type: ignore[arg-type]
            freed = free_slots((h0, h1), rebooked_blocked, pitch) if breach else []
            out.append(dict(day=day, node=node,
                            status="超载-冻结" if breach else "可核准",
                            window=f"{fmt(h0)}-{fmt(h1)}",
                            approved_slots=len(slots),
                            per_slot_people=spec.max_occupancy,
                            reason=reason,
                            free_after_rebooking=len(freed),
                            rebooking_note=(f"保留 {kept_team} 按原约兑现，仅改约其余团单；"
                                            "需被改约团队接受，且不得挤占仍按原约兑现的团队") if breach else ""))
    return out


def pairing_feasibility(audit: Audit) -> list[dict]:
    """同节拍团队人数配对是否可同时被节点容纳（联合结构，不允许拆人重排）。"""
    out = []
    for day in sorted({o.day for o in audit.occupancies}):
        for node, spec in audit.specs.items():
            rows = [o for o in audit.occupancies if o.day == day and o.node == node]
            for i, a in enumerate(rows):
                for b in rows[i + 1:]:
                    total = a.occupancy + b.occupancy
                    out.append(dict(
                        day=day, node=node, pair=f"{a.team}+{b.team}",
                        people=total, capacity=spec.max_occupancy,
                        feasible=total <= spec.max_occupancy,
                        note="人数为团体实测值，不可独立重排" if total > spec.max_occupancy else "恰在容量内",
                    ))
    return out


# ---------------------------------------------------------------- 槽位追溯


def slot_trace(audit: Audit) -> list[dict]:
    """逐节拍枚举候选入场窗口，记录每个窗口被哪条占用/瓶颈拦截。"""
    out = []
    for day in sorted({o.day for o in audit.occupancies}):
        for node, spec in audit.specs.items():
            rows = [o for o in audit.occupancies if o.day == day and o.node == node]
            if not rows:
                out.append(dict(day=day, node=node, slot_start="—", slot_end="—",
                                decision="暂停承诺", blocker="无观测资料",
                                source="primary.csv 缺该节点当日记录"))
                continue
            uncertain = next((o for o in rows if o.leave is None), None)
            if uncertain is not None:
                out.append(dict(day=day, node=node, slot_start=uncertain.raw_enter,
                                slot_end="未知", decision="暂停承诺",
                                blocker=f"团队 {uncertain.team} 缺离场，占用上界未知",
                                source="primary.csv"))
                continue
            closed = next((o for o in rows if o.closure.strip().lower() == "yes"), None)
            if closed is not None:
                out.append(dict(day=day, node=node, slot_start=closed.raw_enter,
                                slot_end="关闭后全部", decision="不投放",
                                blocker=f"节点关闭，团队 {closed.team} 占用至 {closed.raw_leave}",
                                source="primary.csv"))
                continue
            pitch = spec.max_dwell + spec.evacuation
            h0, h1 = min(o.enter for o in rows), max(o.leave for o in rows)  # type: ignore[type-var]
            breach_pairs = [
                (a, b) for i, a in enumerate(rows) for b in rows[i + 1:]
                if a.occupancy + b.occupancy > spec.max_occupancy
                and overlap(a.enter, a.leave, b.enter, b.leave) > 0  # type: ignore[arg-type]
            ]
            t = h0
            while t + pitch <= h1:
                blockers = [
                    f"团队 {o.team} 占用 {fmt(o.enter)}-{fmt(o.leave)}"  # type: ignore[arg-type]
                    for o in rows if overlap(t, t + pitch, o.enter, o.leave) > 0  # type: ignore[arg-type]
                ]
                if breach_pairs:
                    a, b = breach_pairs[0]
                    blockers.append(f"容量超载 {a.team}+{b.team}={a.occupancy + b.occupancy}>{spec.max_occupancy}")
                out.append(dict(
                    day=day, node=node, slot_start=fmt(t), slot_end=fmt(t + pitch),
                    decision="不可售" if blockers else "可售（探索枚举）",
                    blocker="；".join(blockers) if blockers else "无",
                    source="primary.csv + support-2.csv",
                ))
                t += pitch
    return out


# ---------------------------------------------------------------- 输出


def write_outputs(audit: Audit, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    check_capacity(audit)
    check_dwell(audit)
    check_evacuation(audit)
    check_closures(audit)
    check_bookings(audit)

    ledger = [vars(f) for f in audit.findings]
    with (out_dir / "bottleneck_ledger.json").open("w", encoding="utf-8") as fh:
        json.dump(ledger, fh, ensure_ascii=False, indent=2)
    with (out_dir / "bottleneck_ledger.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["code", "day", "node", "window", "detail", "source", "effect"])
        w.writeheader()
        w.writerows(ledger)

    scenarios = scenario_table(audit)
    with (out_dir / "scenario_comparison.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(scenarios[0].keys()))
        w.writeheader()
        w.writerows(scenarios)

    committed = committable(audit)
    with (out_dir / "committable_slots.csv").open("w", encoding="utf-8", newline="") as fh:
        keys = sorted({k for r in committed for k in r})
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(committed)

    pairs = pairing_feasibility(audit)
    with (out_dir / "pairing_check.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(pairs[0].keys()))
        w.writeheader()
        w.writerows(pairs)

    trace = slot_trace(audit)
    with (out_dir / "slot_trace.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(trace[0].keys()))
        w.writeheader()
        w.writerows(trace)

    print(f"findings={len(ledger)} scenarios={len(scenarios)} "
          f"committable_rows={len(committed)} pairs={len(pairs)} "
          f"slot_trace={len(trace)} -> {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    write_outputs(load(args.fixtures), args.out)


if __name__ == "__main__":
    main()
