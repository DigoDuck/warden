import type { StreamedEvent } from "../hooks/useTaskEventStream";
import { EffectBadge } from "./EffectBadge";
import { StatusBadge } from "./StatusBadge";

type Payload = Record<string, unknown>;

function asString(value: unknown): string {
  return typeof value === "string" ? value : String(value ?? "");
}

/** One event, rendered the way a reviewer reads it: `policy.decided` gets the effect
 * (color + icon + text, via EffectBadge — "cor nunca sozinha") with its matched rules
 * inline in Fira Code, because seeing *which rule* denied a call is the whole point of
 * this timeline. Every other known event type gets a short readable sentence; an
 * unrecognised one falls back to its raw JSON instead of silently disappearing. */
function TimelineEntry({ event }: { event: StreamedEvent }) {
  const payload = event.payload as Payload;

  switch (event.type) {
    case "policy.decided": {
      const rules = Array.isArray(payload.matched_rules) ? (payload.matched_rules as string[]) : [];
      return (
        <div className="flex flex-wrap items-center gap-2">
          <EffectBadge effect={asString(payload.effect)} />
          <code className="font-mono text-xs text-fg-muted">{asString(payload.tool)}</code>
          {rules.length > 0 && <code className="font-mono text-xs">{rules.join(", ")}</code>}
        </div>
      );
    }
    case "tool.requested":
      return (
        <p>
          Tool solicitada: <code className="font-mono text-xs">{asString(payload.tool)}</code>
        </p>
      );
    case "tool.executed":
      return (
        <p>
          Tool executada: <code className="font-mono text-xs">{asString(payload.tool)}</code>{" "}
          {payload.ok === false ? "(falhou)" : "(concluída)"}
        </p>
      );
    case "approval.requested":
      return (
        <p>
          Aguardando aprovação para{" "}
          <code className="font-mono text-xs">{asString(payload.tool)}</code>
        </p>
      );
    case "approval.granted":
      return <p>Aprovação concedida.</p>;
    case "approval.rejected":
      return <p>Aprovação rejeitada{payload.note ? `: ${asString(payload.note)}` : "."}</p>;
    case "cancel.requested":
      return <p>Cancelamento solicitado.</p>;
    case "task.finished":
      return (
        <p className="flex items-center gap-2">
          Tarefa finalizada: <StatusBadge status={asString(payload.status)} />
        </p>
      );
    default:
      return <pre className="font-mono text-xs whitespace-pre-wrap">{JSON.stringify(payload)}</pre>;
  }
}

export function Timeline({ events }: { events: StreamedEvent[] }) {
  if (events.length === 0) {
    return <p>Nenhum evento ainda.</p>;
  }
  return (
    <ol className="flex flex-col gap-3">
      {events.map((event) => (
        <li key={event.seq} className="border-b border-border-subtle pb-3 text-sm">
          <TimelineEntry event={event} />
        </li>
      ))}
    </ol>
  );
}
