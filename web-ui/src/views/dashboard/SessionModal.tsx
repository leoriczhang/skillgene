import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  UserBadge,
  Pill,
  Empty,
  ErrorText,
  ListViewport,
  PaginationControls,
  usePagedItems,
} from "@/components/common";
import { cn } from "@/lib/utils";
import { fmtTime } from "@/lib/format";
import { api, type SessionDetail, type SessionProcess } from "@/api/client";

export type SessTab = "detail" | "process";

function StatusBadge({ status }: { status?: string }) {
  if (status === "consumed") return <Pill tone="green">已消费</Pill>;
  if (status === "queued") return <Pill tone="amber">排队中</Pill>;
  return <Pill tone="gray">{status || "-"}</Pill>;
}

export default function SessionModal({
  sid,
  initialTab,
  open,
  onClose,
}: {
  sid: string | null;
  initialTab: SessTab;
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<SessTab>(initialTab);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [process, setProcess] = useState<SessionProcess | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTab(initialTab);
  }, [initialTab, sid]);

  useEffect(() => {
    if (!open || !sid) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    const p =
      tab === "detail"
        ? api<SessionDetail>(`/conversations/${encodeURIComponent(sid)}`).then((d) => {
            if (!cancelled) setDetail(d);
          })
        : api<SessionProcess>(`/conversations/${encodeURIComponent(sid)}/process`).then(
            (d) => {
              if (!cancelled) setProcess(d);
            }
          );
    p.catch((e) => {
      if (!cancelled) setError(e.message);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [open, sid, tab]);

  const title = detail?.meta?.title || "会话详情";

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[88vh] w-full !max-w-[860px] overflow-auto">
        <DialogHeader>
          <DialogTitle>{tab === "detail" ? title : "会话详情"}</DialogTitle>
        </DialogHeader>

        {/* tabs */}
        <div className="flex flex-wrap gap-1.5">
          {(["detail", "process"] as SessTab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cn(
                "rounded-md border px-3.5 py-1 text-xs font-semibold transition-colors",
                t === tab
                  ? "border-sidebar-primary bg-sidebar-primary text-white"
                  : "border-border bg-transparent hover:bg-muted"
              )}
            >
              {t === "detail" ? "会话内容" : "进化过程"}
            </button>
          ))}
        </div>

        {loading ? (
          <Empty>加载中…</Empty>
        ) : error ? (
          <ErrorText>加载失败：{error}</ErrorText>
        ) : tab === "detail" ? (
          <DetailBody d={detail} />
        ) : (
          <ProcessBody p={process} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function DetailBody({ d }: { d: SessionDetail | null }) {
  const m = d?.meta || {};
  const turns = d?.turns || [];
  const turnsPager = usePagedItems(turns);
  if (!d) return null;
  return (
    <div className="space-y-3 text-sm">
      <div>
        <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
          提交人 / 状态 / 轮数
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <UserBadge name={m.user_alias} />
          <span className="text-muted-foreground">·</span>
          <StatusBadge status={m.status} />
          <span className="text-muted-foreground">·</span>
          <span>{m.num_turns != null ? m.num_turns : "-"} 轮</span>
        </div>
      </div>
      {!d.turns_available || !turns.length ? (
        <Empty>
          此会话已被消费且早于内容归档上线，仅存元数据（无正文可回看）。此后新消费的会话都会保留正文。
        </Empty>
      ) : (
        <>
          {d.turns_source === "archive" && (
            <div className="text-xs text-muted-foreground">正文来自消费后归档快照</div>
          )}
          <ListViewport maxHeight="560px">
            {turnsPager.items.map((t, i) => (
              <div key={`${t.turn_num ?? "turn"}-${turnsPager.start + i}`} className="mb-3">
                <div className="mb-1.5 text-[11px] text-muted-foreground">
                  第 {t.turn_num != null ? t.turn_num : "?"} 轮
                </div>
                {t.prompt_text && (
                  <div className="bubble user">
                    <div className="mb-1 text-[11px] font-semibold text-muted-foreground">
                      👤 用户
                    </div>
                    {t.prompt_text}
                  </div>
                )}
                {t.response_text && (
                  <div className="bubble asst">
                    <div className="mb-1 text-[11px] font-semibold text-muted-foreground">
                      🤖 助手
                    </div>
                    {t.response_text}
                  </div>
                )}
              </div>
            ))}
          </ListViewport>
          <PaginationControls {...turnsPager} onPageChange={turnsPager.setPage} />
        </>
      )}
    </div>
  );
}

function ProcessBody({ p }: { p: SessionProcess | null }) {
  const cycles = p?.cycles || [];
  const cyclesPager = usePagedItems(cycles);
  if (!cycles.length) {
    return (
      <Empty>
        该会话尚未进入任何已完成的进化周期（可能仍在排队，或所在周期未产生记录）。
      </Empty>
    );
  }
  return (
    <div className="space-y-3 text-sm">
      <ListViewport maxHeight="560px">
        {cyclesPager.items.map((c, i) => {
          const j = c.judge || {};
          const evos = c.evolutions || [];
          return (
            <div key={`${c.timestamp || "cycle"}-${cyclesPager.start + i}`} className="rounded-lg border border-border p-4">
            <div className="mb-2.5 text-xs text-muted-foreground">
              🕑 {fmtTime(c.timestamp)} &nbsp;·&nbsp; 本周期 {c.sessions ?? "?"} 会话 /{" "}
              {c.skill_groups ?? "?"} 技能组 / 上传 {c.uploaded_skills ?? 0} / 候选{" "}
              {c.candidates_queued ?? 0}
            </div>
            <div className="mb-3">
              <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
                会话评审
              </div>
              <div>
                {j.overall_score != null ? (
                  <>
                    会话评审总分 <b>{j.overall_score}</b>
                    {j.rationale && (
                      <span className="text-muted-foreground"> — {j.rationale}</span>
                    )}
                  </>
                ) : (
                  <span className="text-muted-foreground">本周期无该会话的评审明细</span>
                )}
              </div>
            </div>
            <div>
              <div className="mb-1.5 text-xs font-semibold text-muted-foreground">
                本会话相关的技能进化
              </div>
              {evos.length ? (
                <table className="w-full border-collapse">
                  <thead>
                    <tr>
                      {["技能", "动作", "已上传", "原因"].map((h) => (
                        <th
                          key={h}
                          className="border-b border-line px-3 py-2 text-left text-xs font-semibold text-muted-foreground"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {evos.map((e, k) => (
                      <tr key={k}>
                        <td className="border-b border-line px-3 py-2 align-top">
                          {e.skill_name || "-"}
                        </td>
                        <td className="border-b border-line px-3 py-2 align-top">
                          {e.action || "-"}
                        </td>
                        <td className="border-b border-line px-3 py-2 align-top">
                          {e.uploaded ? "✅" : "—"}
                        </td>
                        <td className="border-b border-line px-3 py-2 align-top text-xs text-muted-foreground">
                          {e.reason || ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="text-xs text-muted-foreground">
                  本会话未直接触发技能变更（可能仅参与聚合评估）。
                </div>
              )}
            </div>
            </div>
          );
        })}
      </ListViewport>
      <PaginationControls {...cyclesPager} onPageChange={cyclesPager.setPage} />
    </div>
  );
}

export { StatusBadge };
