import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { BrowserRouter, Route, Routes, useNavigate } from "react-router-dom";
import { onUnauthorized } from "./api/client";
import { Home } from "./pages/Home";
import { Settings } from "./pages/Settings";
import { SubmitTask } from "./pages/SubmitTask";
import { TaskDetail } from "./pages/TaskDetail";

const queryClient = new QueryClient();

/** Wires apiFetch's 401 handler to the router; must render inside BrowserRouter to use
 * useNavigate. Renders nothing itself. */
function UnauthorizedRedirect() {
  const navigate = useNavigate();
  useEffect(() => {
    onUnauthorized(() => navigate("/configuracoes"));
  }, [navigate]);
  return null;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <UnauthorizedRedirect />
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/tarefas/nova" element={<SubmitTask />} />
          <Route path="/tarefas/:id" element={<TaskDetail />} />
          <Route path="/configuracoes" element={<Settings />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
