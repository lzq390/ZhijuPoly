import { loadKetcherRuntime } from "./loadKetcherRuntime";

/** Loading code does not acquire a workspace lease or create an SDK/Worker. */
export async function preloadStructureEditor(): Promise<void> {
  await loadKetcherRuntime();
}
