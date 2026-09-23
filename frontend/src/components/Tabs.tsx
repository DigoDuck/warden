import { useState, type ReactNode } from "react";

export interface TabDef {
  id: string;
  label: string;
  panel: ReactNode;
}

// TODO(skeleton): plain buttons, no tablist/tab/tabpanel roles, no arrow-key navigation,
// no roving tabindex. Filled in next commit.
export function Tabs({ label, tabs }: { label: string; tabs: TabDef[] }) {
  const [activeId, setActiveId] = useState(tabs[0]?.id);
  const active = tabs.find((tab) => tab.id === activeId);

  return (
    <div aria-label={label}>
      <div>
        {tabs.map((tab) => (
          <button key={tab.id} onClick={() => setActiveId(tab.id)}>
            {tab.label}
          </button>
        ))}
      </div>
      <div>{active?.panel}</div>
    </div>
  );
}
