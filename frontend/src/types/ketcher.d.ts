export {};

declare global {
  interface Window {
    ketcher?: {
      getSmiles: () => Promise<string>;
      getMolfile?: () => Promise<string>;
      getKet?: () => Promise<string>;
      generateImage?: (
        data: string,
        options: {
          outputFormat: "png";
          backgroundColor: string;
          "image-resolution": number;
        }
      ) => Promise<Blob>;
      setMolecule?: (molecule: string) => Promise<void>;
      clear?: () => Promise<void>;
    };
    $3Dmol?: {
      createViewer: (
        element: HTMLElement,
        config?: Record<string, unknown>,
        sharedConfig?: Record<string, unknown>
      ) => {
        addModel: (data: string, format: string) => void;
        setStyle: (selector: Record<string, unknown>, style: Record<string, unknown>) => void;
        addStyle?: (selector: Record<string, unknown>, style: Record<string, unknown>) => void;
        addArrow?: (spec: {
          start: { x: number; y: number; z: number };
          end: { x: number; y: number; z: number };
          radius?: number;
          radiusRatio?: number;
          mid?: number;
          color?: string;
        }) => void;
        removeAllShapes?: () => void;
        zoomTo: () => void;
        render: () => void;
        clear: () => void;
        setBackgroundColor: (color: string) => void;
      };
    };
  }
}
