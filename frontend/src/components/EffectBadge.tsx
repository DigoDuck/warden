import { CheckCircle, Hand, ShieldX, type LucideIcon } from "lucide-react";
import { Badge, type BadgeTone } from "./Badge";

interface EffectEntry {
  tone: BadgeTone;
  icon: LucideIcon;
  label: string;
}

// warden/policy/engine.py's Effect, DESIGN.md's domain-semantics table: the heart of "o
// modelo propõe, o control plane decide".
const EFFECT: Record<string, EffectEntry> = {
  allow: { tone: "ok", icon: CheckCircle, label: "Permitido" },
  deny: { tone: "danger", icon: ShieldX, label: "Negado" },
  require_approval: { tone: "warn", icon: Hand, label: "Aguarda aprovação" },
};

/** The only place that turns a policy effect into color, icon and text. An unrecognised
 * value renders its own text instead of throwing, same reasoning as StatusBadge. */
export function EffectBadge({ effect }: { effect: string }) {
  const entry = EFFECT[effect] ?? { tone: "muted" as const, icon: Hand, label: effect };
  return <Badge tone={entry.tone} icon={entry.icon} label={entry.label} />;
}
