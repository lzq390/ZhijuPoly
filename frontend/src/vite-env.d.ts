/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STRUCTURE_EDITOR_ENGINE?: "react" | "iframe";
  readonly VITE_DEV_GPU_SESSION_CONTROL?: string;
  readonly VITE_AGENT_WORKSPACE_URL?: string;
  readonly VITE_SUBSCRIPTION_ENABLED?: string;
  readonly VITE_REGISTRATION_ENABLED?: string;
  readonly VITE_SERVER_MONITORING_ENABLED?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
