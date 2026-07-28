/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEV_GPU_SESSION_CONTROL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
