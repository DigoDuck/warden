import { useQuery } from "@tanstack/react-query";
import { type ChangeEvent, type ReactNode, useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import type { MetricsSummaryOut } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";

const SKELETON_ROWS = 6;
const DAY_MS = 24 * 60 * 60 * 1000;

// "all" sends no `from`. Periods are by when a task FINISHED (routes_metrics.py), so
// "últimos 7 dias" means tasks that ended in the last seven days, not tasks created then.
const PERIODS = [
  { value: "7d", label: "Últimos 7 dias", days: 7 },
  { value: "30d", label: "Últimos 30 dias", days: 30 },
  { value: "all", label: "Todo o período", days: null },
] as const;

type Period = (typeof PERIODS)[number]["value"];

const NO_DATA = "—";

function number(value: number, fractionDigits: number): string {
  return value.toLocaleString("pt-BR", {
    minimumFractionDigits: 0,
    maximumFractionDigits: fractionDigits,
  });
}

function percent(value: number | null): string {
  return value === null ? NO_DATA : `${number(value * 100, 1)}%`;
}

function count(value: number | null, fractionDigits = 1): string {
  return value === null ? NO_DATA : number(value, fractionDigits);
}

// Money arrives as a string (Decimal): parsed only to be shown, never summed here.
function usd(value: string | null): string {
  if (value === null) return NO_DATA;
  return `US$ ${Number(value).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

function duration(seconds: number | null): string {
  if (seconds === null) return NO_DATA;
  if (seconds < 60) return `${number(seconds, 1)} s`;
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)} min ${total % 60} s`;
}

interface Indicator {
  label: string;
  value: string;
  how: string;
}

function indicators(s: MetricsSummaryOut): Indicator[] {
  return [
    { label: "Taxa de sucesso", value: percent(s.success_rate), how: "concluídas ÷ tarefas encerradas" },
    { label: "Custo por tarefa", value: usd(s.cost_per_task), how: "custo total ÷ tarefas encerradas" },
    {
      label: "Custo por sucesso",
      value: usd(s.cost_per_success),
      how: "custo total (falhas incluídas) ÷ concluídas",
    },
    { label: "Custo total", value: usd(s.cost_total), how: "todas as chamadas ao modelo, inclusive revisor" },
    { label: "Tokens de entrada por tarefa", value: count(s.tokens_in_per_task, 0), how: "média" },
    { label: "Tokens de saída por tarefa", value: count(s.tokens_out_per_task, 0), how: "média" },
    { label: "Latência p50", value: duration(s.latency_p50_s), how: "início até fim da tarefa, posto mais próximo" },
    { label: "Latência p95", value: duration(s.latency_p95_s), how: "início até fim da tarefa, posto mais próximo" },
    { label: "Iterações por tarefa", value: count(s.avg_iterations), how: "média de iteration.started" },
    { label: "Chamadas de tool por tarefa", value: count(s.tool_calls_per_task), how: "média, sem o finish" },
    {
      label: "Taxa de falha de tool",
      value: percent(s.tool_failure_rate),
      how: "chamadas permitidas que erraram ÷ permitidas",
    },
    { label: "Intervenções por tarefa", value: count(s.interventions_per_task, 2), how: "aprovações pedidas, média" },
    {
      label: "Taxa de negação",
      value: percent(s.deny_rate),
      how: "decisões de policy negadas ÷ todas as decisões",
    },
  ];
}

const TABLE_HEAD = "h-10 border-b border-border-subtle bg-surface-raised text-fg-muted";

function Skeleton(): ReactNode {
  return (
    <div className="mt-4" aria-live="polite">
      <p>Carregando métricas…</p>
      <div className="flex flex-col gap-1" aria-hidden="true">
        {Array.from({ length: SKELETON_ROWS }, (_, i) => (
          <div key={i} className="h-10 rounded-md bg-surface" />
        ))}
      </div>
    </div>
  );
}

function Summary({ data }: { data: MetricsSummaryOut }): ReactNode {
  return (
    <>
      <p className="mt-4 text-fg-muted">
        <span className="tabular-nums">{data.tasks}</span> tarefas encerradas no período.
        Tarefas ainda em andamento não entram na conta.
      </p>

      {/* DESIGN.md: never a horizontal page scroll; a wide table scrolls in its own box. */}
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Indicadores do período</caption>
          <thead>
            <tr className={TABLE_HEAD}>
              <th scope="col">Indicador</th>
              <th scope="col" className="text-right">
                Valor
              </th>
              <th scope="col">Como é calculado</th>
            </tr>
          </thead>
          <tbody>
            {indicators(data).map((row) => (
              <tr key={row.label} className="h-10 border-b border-border-subtle hover:bg-surface-raised">
                <th scope="row" className="font-normal">
                  {row.label}
                </th>
                <td className="text-right font-semibold tabular-nums">{row.value}</td>
                <td className="text-fg-muted">{row.how}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="mt-8">Tarefas por status</h2>
      <div className="mt-2 max-w-md overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Tarefas encerradas por status</caption>
          <thead>
            <tr className={TABLE_HEAD}>
              <th scope="col">Status</th>
              <th scope="col" className="text-right">
                Tarefas
              </th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(data.by_status).map(([status, n]) => (
              <tr key={status} className="h-10 border-b border-border-subtle hover:bg-surface-raised">
                <td>
                  <StatusBadge status={status} />
                </td>
                <td className="text-right tabular-nums">{n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function Metrics() {
  const [period, setPeriod] = useState<Period>("30d");

  const query = useQuery({
    queryKey: ["metrics", "summary", period],
    queryFn: () => {
      const days = PERIODS.find((p) => p.value === period)?.days;
      // Computed at request time: a refetch of "últimos 7 dias" slides the window forward.
      const qs = days ? `?from=${encodeURIComponent(new Date(Date.now() - days * DAY_MS).toISOString())}` : "";
      return apiFetch<MetricsSummaryOut>(`/metrics/summary${qs}`);
    },
  });

  return (
    <div>
      <h1>Métricas</h1>

      <div className="mt-4 flex items-center gap-3">
        <label htmlFor="period" className="text-sm text-fg-muted">
          Período
        </label>
        <select
          id="period"
          value={period}
          onChange={(e: ChangeEvent<HTMLSelectElement>) => setPeriod(e.target.value as Period)}
        >
          {PERIODS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
      </div>

      {query.isLoading && <Skeleton />}

      {query.isError && (
        <div className="mt-4" role="alert">
          <p>
            Falha ao carregar as métricas.{" "}
            {query.error instanceof ApiError ? `(${query.error.status})` : ""}
          </p>
          <button type="button" className="border-border-control" onClick={() => query.refetch()}>
            Tentar de novo
          </button>
        </div>
      )}

      {query.data && query.data.tasks === 0 && (
        <p className="mt-4">
          Nenhuma tarefa concluída neste período. Escolha um período maior ou aguarde uma tarefa
          terminar.
        </p>
      )}

      {query.data && query.data.tasks > 0 && <Summary data={query.data} />}
    </div>
  );
}
