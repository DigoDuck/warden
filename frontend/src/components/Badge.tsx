import type { ComponentType } from "react";

export type BadgeTone = "muted" | "info" | "ok" | "warn" | "danger" | "caution";

// Literal strings, not `text-${tone}`: Tailwind's build-time scanner looks for whole class
// names as text in the source, so an assembled string would never generate the utility.
const TONE_CLASSES: Record<BadgeTone, string> = {
  muted: "text-fg-muted",
  info: "text-info",
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
  caution: "text-caution",
};

interface IconProps {
  size?: number;
  "aria-hidden"?: boolean | "true" | "false";
}

interface BadgeProps {
  tone: BadgeTone;
  icon: ComponentType<IconProps>;
  label: string;
}

/** DESIGN.md "Cor nunca sozinha": every status or policy effect this renders carries
 * color, an icon and text together, never color alone. This is the one place that
 * composes the three; `StatusBadge` and `EffectBadge` only supply the (tone, icon, label)
 * triple for their own domain value. */
export function Badge({ tone, icon: Icon, label }: BadgeProps) {
  return (
    <span className={`inline-flex items-center gap-1 text-sm font-medium ${TONE_CLASSES[tone]}`}>
      <Icon size={16} aria-hidden="true" />
      {label}
    </span>
  );
}
