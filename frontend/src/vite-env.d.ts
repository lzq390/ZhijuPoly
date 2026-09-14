/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STRUCTURE_EDITOR_ENGINE?: "react" | "iframe";
  readonly VITE_DEV_GPU_SESSION_CONTROL?: string;
  readonly VITE_AGENT_WORKSPACE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
