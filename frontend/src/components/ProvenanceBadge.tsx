import { Bot, ShieldCheck, type LucideIcon } from "lucide-react";
import { Badge, type BadgeTone } from "./Badge";

export type Provenance = "gerado" | "verificado";

interface ProvenanceEntry {
  tone: BadgeTone;
  icon: LucideIcon;
  label: string;
}

// briefing Parte D, "O que veio do modelo é marcado": the badge answers "who produced this",
// never "is the news good". Model output (the coder's summary, the reviewer's verdict) is
// "gerado"; only what the control plane computed deterministically (evidence) is "verificado".
// A failing check is still "verificado"; an approving verdict is still "gerado".
const PROVENANCE: Record<Provenance, ProvenanceEntry> = {
  gerado: { tone: "muted", icon: Bot, label: "Gerado" },
  verificado: { tone: "info", icon: ShieldCheck, label: "Verificado" },
};

/** Marks where a piece of content came from. `detail` narrows the source in the same label,
 * e.g. the reviewer's verdict is "Gerado · revisor independente": still model output, but not
 * the coder's own. */
export function ProvenanceBadge({ kind, detail }: { kind: Provenance; detail?: string }) {
  const entry = PROVENANCE[kind];
  const label = detail ? `${entry.label} · ${detail}` : entry.label;
  return <Badge tone={entry.tone} icon={entry.icon} label={label} />;
}
