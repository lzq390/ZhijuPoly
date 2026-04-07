import { useRef, useState } from "react";

export function useKetcher(initialSmiles = "") {
  const [smiles, setSmiles] = useState(initialSmiles);
  const [isReady, setIsReady] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  return {
    smiles,
    setSmiles,
    isReady,
    setIsReady,
    iframeRef
  };
}
