import { useQuery } from "@tanstack/react-query";
import { ClipboardCheck, ListChecks, Menu, Plus, Settings } from "lucide-react";
import { type ComponentType, type ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { apiFetch } from "../api/client";
import type { ApprovalOut } from "../api/types";

interface NavItem {
  to: string;
  label: string;
  icon: ComponentType<{ size?: number; "aria-hidden"?: boolean }>;
  end?: boolean;
  badge?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Tarefas", icon: ListChecks, end: true },
  { to: "/tarefas/nova", label: "Nova tarefa", icon: Plus },
  { to: "/aprovacoes", label: "Aprovações", icon: ClipboardCheck, badge: true },
  { to: "/configuracoes", label: "Configurações", icon: Settings },
];

function NavItems({ pendingCount }: { pendingCount: number }): ReactNode {
  return (
    <ul className="flex flex-col gap-1 lg:mt-2">
      {NAV_ITEMS.map(({ to, label, icon: Icon, end, badge }) => (
        <li key={to}>
          <NavLink
            to={to}
            end={end}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-md border-l-2 px-3 py-2 text-sm ${
                isActive
                  ? "border-accent bg-surface-raised text-fg"
                  : "border-transparent text-fg-muted"
              }`
            }
          >
            <Icon size={20} aria-hidden={true} />
            {label}
            {badge && pendingCount > 0 && (
              <span
                className="ml-auto rounded-full bg-warn px-2 py-0.5 text-xs font-semibold text-bg"
                aria-label={`${pendingCount} aprovações pendentes`}
              >
                {pendingCount}
              </span>
            )}
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

// TODO(skeleton): renders the shell but never moves focus to the new page's h1 on route
// change. Filled in next commit.
export function AppShell() {
  const approvalsQuery = useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => apiFetch<ApprovalOut[]>("/approvals?status=pending"),
    refetchInterval: 5000,
  });
  const pendingCount = approvalsQuery.data?.length ?? 0;

  return (
    <div className="min-h-screen lg:flex">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-10 focus:rounded-md focus:bg-surface focus:px-4 focus:py-2"
      >
        Pular para o conteúdo
      </a>

      <details className="border-b border-border-subtle bg-surface lg:hidden">
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3">
          <span className="font-semibold">Warden</span>
          <Menu size={20} aria-hidden={true} />
        </summary>
        <nav aria-label="Principal" className="px-2 pb-2">
          <NavItems pendingCount={pendingCount} />
        </nav>
      </details>

      <nav
        aria-label="Principal"
        className="hidden lg:flex lg:w-60 lg:flex-col lg:border-r lg:border-border-subtle lg:bg-surface lg:p-4"
      >
        <span className="mb-6 px-3 text-lg font-semibold">Warden</span>
        <NavItems pendingCount={pendingCount} />
      </nav>

      <main id="main-content" className="mx-auto w-full max-w-[1280px] flex-1 p-6 lg:p-8">
        <Outlet />
      </main>
    </div>
  );
}
