export const OPENSCIENCE_BRIDGE_NAMESPACE = "openscience.zhijupoly" as const;
export const OPENSCIENCE_BRIDGE_VERSION = 1 as const;

export type OpenScienceProjectSummary = {
  directory: string;
  name: string;
  displayPath: string;
  updatedAt: number;
  favorite: boolean;
};

export type OpenScienceProjectsSnapshot = {
  namespace: typeof OPENSCIENCE_BRIDGE_NAMESPACE;
  version: typeof OPENSCIENCE_BRIDGE_VERSION;
  type: "projects.snapshot";
  projects: OpenScienceProjectSummary[];
  activeDirectory: string | null;
};

type BridgeWindow = {
  postMessage(message: unknown, targetOrigin: string): void;
};

type BridgeMessageEvent = {
  data: unknown;
  origin: string;
  source: unknown;
};

type CreateOpenScienceProjectBridgeOptions = {
  workspaceUrl: string;
  getFrameWindow: () => BridgeWindow | null;
  onSnapshot: (snapshot: OpenScienceProjectsSnapshot) => void;
};

export function resolveAgentWorkspaceOrigin(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return undefined;
    }
    if (url.username || url.password) {
      return undefined;
    }
    return url.origin;
  } catch {
    return undefined;
  }
}

export function parseOpenScienceProjectsSnapshot(value: unknown): OpenScienceProjectsSnapshot | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  if (value.namespace !== OPENSCIENCE_BRIDGE_NAMESPACE) {
    return undefined;
  }
  if (value.version !== OPENSCIENCE_BRIDGE_VERSION || value.type !== "projects.snapshot") {
    return undefined;
  }
  if (!Array.isArray(value.projects)) {
    return undefined;
  }

  const projects: OpenScienceProjectSummary[] = [];
  for (const project of value.projects) {
    const parsed = parseProjectSummary(project);
    if (!parsed) {
      return undefined;
    }
    projects.push(parsed);
  }

  if (
    value.activeDirectory !== null &&
    (typeof value.activeDirectory !== "string" || !value.activeDirectory.trim())
  ) {
    return undefined;
  }

  return {
    namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
    version: OPENSCIENCE_BRIDGE_VERSION,
    type: "projects.snapshot",
    projects,
    activeDirectory: value.activeDirectory
  };
}

export function createOpenScienceProjectBridge(options: CreateOpenScienceProjectBridgeOptions) {
  const workspaceOrigin = resolveAgentWorkspaceOrigin(options.workspaceUrl);

  function post(message: unknown): boolean {
    const frameWindow = options.getFrameWindow();
    if (!workspaceOrigin || !frameWindow) {
      return false;
    }
    frameWindow.postMessage(message, workspaceOrigin);
    return true;
  }

  return {
    handleMessage(event: BridgeMessageEvent) {
      const frameWindow = options.getFrameWindow();
      if (!workspaceOrigin || !frameWindow) {
        return;
      }
      if (event.source !== frameWindow || event.origin !== workspaceOrigin) {
        return;
      }

      const snapshot = parseOpenScienceProjectsSnapshot(event.data);
      if (snapshot) {
        options.onSnapshot(snapshot);
      }
    },
    requestProjects() {
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "projects.request"
      });
    },
    browseProjects() {
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "projects.browse"
      });
    },
    newProject() {
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "project.new"
      });
    },
    setProjectFavorite(directory: string, favorite: boolean) {
      if (!directory.trim()) {
        return false;
      }
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "project.favorite.set",
        directory,
        favorite
      });
    },
    archiveProject(directory: string) {
      if (!directory.trim()) {
        return false;
      }
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "project.archive",
        directory
      });
    },
    openProject(directory: string) {
      if (!directory.trim()) {
        return false;
      }
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "project.open",
        directory
      });
    }
  };
}

function parseProjectSummary(value: unknown): OpenScienceProjectSummary | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  if (typeof value.directory !== "string" || !value.directory.trim()) {
    return undefined;
  }
  if (typeof value.name !== "string" || !value.name.trim()) {
    return undefined;
  }
  if (typeof value.displayPath !== "string") {
    return undefined;
  }
  if (typeof value.updatedAt !== "number" || !Number.isFinite(value.updatedAt)) {
    return undefined;
  }
  if (typeof value.favorite !== "boolean") {
    return undefined;
  }

  return {
    directory: value.directory,
    name: value.name,
    displayPath: value.displayPath,
    updatedAt: value.updatedAt,
    favorite: value.favorite
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
