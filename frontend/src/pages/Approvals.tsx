import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "../api/client";
import type { ApprovalOut } from "../api/types";

// TODO(skeleton): no task link, no Aprovar/Rejeitar actions, no note requirement, no 409
// handling. Filled in next commit.
export function Approvals() {
  const query = useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => apiFetch<ApprovalOut[]>("/approvals?status=pending"),
  });

  return (
    <main>
      <h1>Aprovações</h1>
      <ul>
        {(query.data ?? []).map((approval) => (
          <li key={approval.id}>{approval.tool}</li>
        ))}
      </ul>
    </main>
  );
}
