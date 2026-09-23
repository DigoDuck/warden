import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "../api/client";
import type { TaskListOut } from "../api/types";

// TODO(skeleton): no loading/empty/error states, no status filter, no link per row (not
// keyboard reachable). Filled in next commit.
export function Tasks() {
  const query = useQuery({
    queryKey: ["tasks"],
    queryFn: () => apiFetch<TaskListOut>("/tasks"),
  });

  return (
    <main>
      <h1>Tarefas</h1>
      <ul>
        {(query.data?.tasks ?? []).map((task) => (
          <li key={task.id}>{task.spec}</li>
        ))}
      </ul>
    </main>
  );
}
