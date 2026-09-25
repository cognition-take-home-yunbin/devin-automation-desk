import type {
  NativeSession,
  Overview,
  Report,
  ScanRequestResult,
  TaskDeleteResult,
  TaskDetail,
  TaskSummary,
} from "./types";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

export const api = {
  overview: () => get<Overview>("/api/overview"),
  tasks: () => get<TaskSummary[]>("/api/tasks"),
  task: (id: number) => get<TaskDetail>(`/api/tasks/${id}`),
  reports: () => get<Report[]>("/api/reports"),
  nativeSessions: () => get<NativeSession[]>("/api/native-sessions"),
  startScenario: (scenario: string) =>
    fetch("/api/simulation/scenarios", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario }),
    }).then(async (res) => {
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }),
  scanNow: () =>
    fetch("/api/scan", { method: "POST" }).then(async (res) => {
      if (!res.ok) throw new Error(await res.text());
      return (await res.json()) as ScanRequestResult;
    }),
  deleteTask: (id: number) =>
    fetch(`/api/tasks/${id}`, { method: "DELETE" }).then(async (res) => {
      if (!res.ok) throw new Error(await res.text());
      return (await res.json()) as TaskDeleteResult;
    }),
};
