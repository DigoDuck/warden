import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

export function Home() {
  const navigate = useNavigate();
  const [taskId, setTaskId] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = taskId.trim();
    if (trimmed) {
      navigate(`/tarefas/${trimmed}`);
    }
  }

  return (
    <main>
      <h1>Warden</h1>
      {/* Ainda não existe GET /tasks (listagem); dizer isso aqui em vez de fingir uma
          tabela vazia, que pareceria um bug em vez de uma ausência conhecida. */}
      <p>
        Ainda não existe um endpoint de listagem de tarefas. Submeta uma tarefa nova ou cole
        o id de uma tarefa existente para ver o detalhe.
      </p>
      <nav>
        <Link to="/tarefas/nova">Submeter tarefa</Link> · <Link to="/configuracoes">Configurações</Link>
      </nav>
      <form onSubmit={handleSubmit}>
        <label htmlFor="task-id">Ver tarefa por id</label>
        <input id="task-id" value={taskId} onChange={(event) => setTaskId(event.target.value)} />
        <button type="submit">Ver</button>
      </form>
    </main>
  );
}
