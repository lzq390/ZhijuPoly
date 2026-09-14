import { retryModuleImport } from "../../retryModuleImport";

/** Keep SDK transport separate from the editor session and its visibility gates. */
export const loadKetcherRuntime = retryModuleImport(() => import("./KetcherReactRuntime"));
