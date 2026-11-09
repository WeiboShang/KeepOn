/// <reference types="vite/client" />
declare global { interface Window { keepOn?: {
  openExternal(url: string): Promise<void>; notify(title: string, body: string): Promise<void>
  showApproval(): Promise<void>; hideApproval(): Promise<void>
  acceptanceRpc(method: string, params: Record<string, unknown>): Promise<{result?: unknown; error?: {code: number; message: string; data?: unknown}}>
} } }
export {}
