import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Toaster } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { Activity, ClipboardCheck, Filter, History, LayoutDashboard, BookOpenText, Users, SlidersHorizontal, LogOut, RefreshCw } from "lucide-react";
import { api, type AuthStatus, type UserProfile } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import DashboardView from "@/views/DashboardView";
import SkillsView from "@/views/SkillsView";
import UsersView from "@/views/UsersView";
import ModelSettingsView from "@/views/ModelSettingsView";
import CandidateReviewView from "@/views/CandidateReviewView";
import HealthView from "@/views/HealthView";
import AuditView from "@/views/AuditView";
import SessionFilterView from "@/views/SessionFilterView";

type ViewKey = "dashboard" | "candidates" | "audit" | "filter" | "health" | "skills" | "users" | "model";

const NAV: { key: ViewKey; label: string; icon: typeof LayoutDashboard }[] = [
  { key: "dashboard", label: "进化看板", icon: LayoutDashboard },
  { key: "candidates", label: "候选评审", icon: ClipboardCheck },
  { key: "audit", label: "进化审计", icon: History },
  { key: "filter", label: "过滤审计", icon: Filter },
  { key: "health", label: "系统健康", icon: Activity },
  { key: "skills", label: "技能管理", icon: BookOpenText },
  { key: "users", label: "用户管理", icon: Users },
  { key: "model", label: "模型配置", icon: SlidersHorizontal },
];

export default function App() {
  const [view, setView] = useState<ViewKey>("dashboard");
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [userMenuOpen, setUserMenuOpen] = useState(false);

  const refreshAuth = useCallback(async () => {
    setCheckingAuth(true);
    try {
      const status = await api<AuthStatus>("/api/auth/status");
      setAuth(status);
    } catch (e: any) {
      setAuth({ authenticated: false, needs_setup: false });
      toastErr("登录状态检查失败", e.message);
    } finally {
      setCheckingAuth(false);
    }
  }, []);

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  async function logout() {
    try {
      await api("/api/auth/logout", { method: "POST" });
      setAuth({ authenticated: false, needs_setup: false });
      setUserMenuOpen(false);
      toastOk("已退出登录");
    } catch (e: any) {
      toastErr("退出失败", e.message);
    }
  }

  if (checkingAuth && !auth) {
    return (
      <div className="grid min-h-screen place-items-center bg-background text-sm text-muted-foreground">
        正在检查登录状态…
        <Toaster position="bottom-right" />
      </div>
    );
  }

  if (!auth?.authenticated) {
    return (
      <>
        <LoginGate
          needsSetup={!!auth?.needs_setup}
          onAuthed={(next) => setAuth(next)}
        />
        <Toaster position="bottom-right" />
      </>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* ---- Sidebar (StaffDeck SD1 layout) ---- */}
      <aside className="flex h-screen w-54 shrink-0 flex-col border-r border-line bg-surface">
        <div className="flex h-[58px] items-center gap-2.5 border-b border-line px-[18px]">
          <div className="grid size-[30px] place-items-center rounded-lg bg-sidebar-primary text-[13px] font-extrabold tracking-tighter text-white">
            SG
          </div>
          <div className="text-[15px] font-bold tracking-tight">SkillGene</div>
        </div>
        <nav className="flex flex-col gap-0.5 px-2.5 py-3">
          {NAV.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setView(key)}
              className={cn(
                "flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[13.5px] font-semibold transition-colors",
                view === key
                  ? "bg-sidebar-accent text-foreground"
                  : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-foreground"
              )}
            >
              <Icon className="size-4 opacity-80" />
              {label}
            </button>
          ))}
        </nav>
        <div className="mt-auto border-t border-line px-[18px] py-3.5 text-[11px] leading-relaxed text-muted-soft">
          团队技能进化平台
          <br />
          统一控制台 · v1
        </div>
      </aside>

      {/* ---- Content ---- */}
      <main className="h-screen flex-1 overflow-auto bg-background">
        <UserMenu
          user={auth.user}
          open={userMenuOpen}
          onToggle={() => setUserMenuOpen((v) => !v)}
          onRefresh={refreshAuth}
          onLogout={logout}
        />
        <div className={cn(view !== "dashboard" && "hidden")}>
          <DashboardView active={view === "dashboard"} />
        </div>
        <div className={cn(view !== "candidates" && "hidden")}>
          <CandidateReviewView active={view === "candidates"} />
        </div>
        <div className={cn(view !== "audit" && "hidden")}>
          <AuditView active={view === "audit"} />
        </div>
        <div className={cn(view !== "filter" && "hidden")}>
          <SessionFilterView active={view === "filter"} />
        </div>
        <div className={cn(view !== "health" && "hidden")}>
          <HealthView active={view === "health"} user={auth.user} />
        </div>
        <div className={cn(view !== "skills" && "hidden")}>
          <SkillsView active={view === "skills"} user={auth.user} />
        </div>
        <div className={cn(view !== "users" && "hidden")}>
          <UsersView active={view === "users"} />
        </div>
        <div className={cn(view !== "model" && "hidden")}>
          <ModelSettingsView active={view === "model"} user={auth.user} />
        </div>
      </main>

      <Toaster position="bottom-right" />
    </div>
  );
}

function LoginGate({
  needsSetup,
  onAuthed,
}: {
  needsSetup: boolean;
  onAuthed: (status: AuthStatus) => void;
}) {
  const [username, setUsername] = useState(needsSetup ? "admin" : "");
  const [displayName, setDisplayName] = useState(needsSetup ? "admin" : "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState(needsSetup ? "admin" : "");
  const [loading, setLoading] = useState(false);

  async function submit() {
    setLoading(true);
    try {
      const path = needsSetup ? "/api/auth/bootstrap" : "/api/auth/login";
      const payload = needsSetup
        ? { username, display_name: displayName || username, email, password }
        : { username, password };
      const status = await api<AuthStatus>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      onAuthed(status);
      toastOk(needsSetup ? "管理员已初始化" : "登录成功", status.user?.display_name || status.user?.id || "");
    } catch (e: any) {
      toastErr(needsSetup ? "初始化失败" : "登录失败", e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-background px-6">
      <div className="w-full max-w-[420px] rounded-4xl border border-border bg-surface p-6 shadow-[var(--shadow-float)]">
        <div className="mb-5">
          <div className="mb-2 grid size-10 place-items-center rounded-xl bg-sidebar-primary text-sm font-extrabold text-white">
            SG
          </div>
          <h1 className="text-[22px] font-bold tracking-tight">
            {needsSetup ? "初始化管理员账号" : "登录 SkillGene 控制台"}
          </h1>
          <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
            {needsSetup
              ? "当前还没有用户。默认管理员账号和密码均为 admin，可直接创建后登录。"
              : "请输入账号密码后继续访问团队技能进化控制台。"}
          </p>
        </div>

        <div className="space-y-3.5">
          <Field label="账号">
            <Input value={username} placeholder="admin" onChange={(e) => setUsername(e.target.value)} />
          </Field>
          {needsSetup && (
            <div className="grid gap-3.5 sm:grid-cols-2">
              <Field label="显示名">
                <Input value={displayName} placeholder="管理员" onChange={(e) => setDisplayName(e.target.value)} />
              </Field>
              <Field label="邮箱">
                <Input value={email} placeholder="name@example.com" onChange={(e) => setEmail(e.target.value)} />
              </Field>
            </div>
          )}
          <Field label="密码">
            <Input
              type="password"
              value={password}
              placeholder={needsSetup ? "默认 admin" : "请输入密码"}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
            />
          </Field>
          <Button className="w-full" disabled={loading} onClick={submit}>
            {needsSetup ? "创建管理员并登录" : "登录"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function UserMenu({
  user,
  open,
  onToggle,
  onRefresh,
  onLogout,
}: {
  user?: UserProfile | null;
  open: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  onLogout: () => void;
}) {
  const name = user?.display_name || user?.id || "unknown";
  const initials = name.slice(0, 1).toUpperCase();
  return (
    <div className="fixed top-4 right-5 z-50">
      <button
        onClick={onToggle}
        className="flex items-center gap-2 rounded-full border border-border bg-surface px-2 py-1.5 shadow-[var(--shadow-soft)] hover:bg-muted"
      >
        <span className="grid size-7 place-items-center rounded-full bg-sidebar-primary text-xs font-bold text-white">
          {initials}
        </span>
        <span className="max-w-[160px] truncate text-sm font-semibold">{name}</span>
        <span className="text-[11px] text-muted-foreground">{user?.role === "admin" ? "管理员" : "用户"}</span>
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-[240px] overflow-hidden rounded-xl border border-border bg-surface shadow-[var(--shadow-float)]">
          <div className="border-b border-line px-4 py-3">
            <div className="text-sm font-bold">{name}</div>
            <div className="mt-1 text-xs text-muted-foreground">{user?.email || user?.id || ""}</div>
          </div>
          <button onClick={onRefresh} className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm hover:bg-muted">
            <RefreshCw className="size-4" />
            刷新登录信息
          </button>
          <button onClick={onLogout} className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-destructive hover:bg-muted">
            <LogOut className="size-4" />
            退出登录
          </button>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}
