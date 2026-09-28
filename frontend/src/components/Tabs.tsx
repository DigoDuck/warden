import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";

export interface TabDef {
  id: string;
  label: string;
  panel: ReactNode;
}

/** WAI-ARIA APG "tabs" pattern: roving tabindex (only the selected tab is a Tab stop),
 * ArrowLeft/ArrowRight move focus and select (wrapping at the ends), Home/End jump to the
 * first/last tab. Used once, for Task Detail's Spec/Execução/Custo — kept as its own
 * component rather than inlined so the keyboard behaviour has its own test. */
export function Tabs({
  label,
  tabs,
  defaultTabId,
}: {
  label: string;
  tabs: TabDef[];
  /** Initial selected tab, by id. Defaults to the first tab (`tabs[0]`) when omitted, same
   * as before this prop existed. Lets a caller open on, say, a live-updating tab without
   * having to reorder the tab list itself just to change which one starts selected. */
  defaultTabId?: string;
}) {
  const [activeId, setActiveId] = useState(defaultTabId ?? tabs[0]?.id);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  function select(id: string, focus: boolean) {
    setActiveId(id);
    if (focus) {
      tabRefs.current[id]?.focus();
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number;
    switch (event.key) {
      case "ArrowRight":
        nextIndex = (index + 1) % tabs.length;
        break;
      case "ArrowLeft":
        nextIndex = (index - 1 + tabs.length) % tabs.length;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = tabs.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    const next = tabs[nextIndex];
    if (next) {
      select(next.id, true);
    }
  }

  return (
    <div>
      <div role="tablist" aria-label={label} className="flex gap-1 border-b border-border-subtle">
        {tabs.map((tab, index) => {
          const isSelected = tab.id === activeId;
          return (
            <button
              key={tab.id}
              ref={(el) => {
                tabRefs.current[tab.id] = el;
              }}
              type="button"
              role="tab"
              id={`tab-${tab.id}`}
              aria-selected={isSelected}
              aria-controls={`panel-${tab.id}`}
              tabIndex={isSelected ? 0 : -1}
              className={`border-b-2 px-3 py-2 text-sm ${
                isSelected ? "border-accent text-fg" : "border-transparent text-fg-muted"
              }`}
              onClick={() => select(tab.id, false)}
              onKeyDown={(event) => handleKeyDown(event, index)}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {tabs
        .filter((tab) => tab.id === activeId)
        .map((tab) => (
          // Only the active panel is mounted: the alternative (mount all, `hidden` on the
          // rest) would keep an idle SSE-driven Execução tab subscribed in the background
          // for no reason while Spec or Custo is showing.
          <div
            key={tab.id}
            role="tabpanel"
            id={`panel-${tab.id}`}
            aria-labelledby={`tab-${tab.id}`}
            className="pt-4"
          >
            {tab.panel}
          </div>
        ))}
    </div>
  );
}
