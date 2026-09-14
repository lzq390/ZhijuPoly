import { useMemo } from "react";
import { Editor } from "ketcher-react";
import { StandaloneStructServiceProvider } from "ketcher-standalone";
import type { NativeKetcher } from "../../structure/nativeSession";
import type { RuntimeProps } from "./ReactStructureEditor";
import "ketcher-react/dist/index.css";
import "../../styles/ketcher-native.css";

/** This module is the sole native SDK/CSS import, behind the engine alias. */
export default function KetcherReactRuntime({ session, onInit }: RuntimeProps) {
  const provider = useMemo(() => {
    const instance = new StandaloneStructServiceProvider();
    const create = instance.createStructService.bind(instance);
    instance.createStructService = options => {
      session.check();
      return session.ownService(create(options));
    };
    return instance;
  }, [session]);
  return <Editor structServiceProvider={provider} staticResourcesUrl={`${import.meta.env.BASE_URL}ketcher-assets/3.8.0`}
    onInit={ketcher => onInit(ketcher as unknown as NativeKetcher)}
    errorHandler={error => {
      if (!session.initialized) session.fail(`结构编辑器初始化失败：${String(error)}`);
    }} />;
}
