import { Hand } from "lucide-react";

/** The only place that turns a policy effect (warden/policy/engine.py's Effect) into
 * color, icon and text (DESIGN.md's domain-semantics table). */
export function EffectBadge({ effect }: { effect: string }) {
  // TODO(skeleton): icon + aria-label only, no visible text. Filled in next commit.
  return <Hand size={16} aria-label={effect} />;
}
